from __future__ import annotations

from pathlib import Path

import pytest

from pytest_queryguard.detectors import Finding, RelationshipFinding, RepeatFinding
from pytest_queryguard.events import CallSite, ScanEvent, ScanIssue, StatementEvent
from pytest_queryguard.messages import (
    format_budget,
    format_duration,
    format_findings,
    format_index_findings,
    format_index_notes,
)

CWD = Path("/work/project")


def test_relationship_message_has_fix_hint() -> None:
    site = CallSite(
        filename="/work/project/app/api/orders.py", lineno=42, function="list_orders"
    )
    finding = RelationshipFinding("Order.customer", 50, site, None)
    assert format_findings([finding], cwd=CWD).splitlines() == [
        "queryguard: N+1 detected",
        "",
        "  Order.customer was lazy-loaded 50 times",
        "  first at app/api/orders.py:42 in list_orders",
        "  kind: relationship-load  label: Order.customer",
        "  fix: load it with the parent query, e.g.",
        "       select(Order).options(selectinload(Order.customer))",
    ]


def test_relationship_message_shows_sample_sql_truncated() -> None:
    site = CallSite(
        filename="/work/project/app/api/orders.py", lineno=42, function="list_orders"
    )
    sql = "SELECT customer.id\nFROM customer\nWHERE customer.id = ?"
    finding = RelationshipFinding("Order.customer", 50, site, sql)
    lines = format_findings([finding], cwd=CWD).splitlines()
    assert lines[3:6] == [
        "  first at app/api/orders.py:42 in list_orders",
        "  kind: relationship-load  label: Order.customer",
        "  sql: SELECT customer.id FROM customer WHERE customer.id = ?",
    ]
    long_sql = "SELECT " + "x" * 300
    long = RelationshipFinding("Order.customer", 50, site, long_sql)
    assert f"  sql: {long_sql[:200]}..." in format_findings([long]).splitlines()


def test_repeat_message() -> None:
    site = CallSite(
        filename="/work/project/app/services/orders.py",
        lineno=18,
        function="fetch_customers",
    )
    sql = "SELECT customer.id, customer.name FROM customer WHERE customer.id = ?"
    assert format_findings([RepeatFinding(sql, 50, site)], cwd=CWD).splitlines() == [
        "queryguard: N+1 detected",
        "",
        "  the same statement ran 50 times",
        "  first at app/services/orders.py:18 in fetch_customers",
        f"  kind: repeated-statement  label: {sql}",
        "  fix: fetch all rows in one query (WHERE id IN (...)) instead of in a loop",
    ]


@pytest.mark.parametrize(
    ("limit", "first_line"),
    [
        (0, "queryguard: expected at most 0 queries, got 1"),
        (1, "queryguard: expected at most 1 query, got 1"),
        (2, "queryguard: expected at most 2 queries, got 1"),
    ],
)
def test_budget_message_noun_agrees_with_the_limit(limit: int, first_line: str) -> None:
    site = CallSite(filename="/work/project/app/a.py", lineno=7, function="run")
    statements = [StatementEvent("SELECT 1", site, None)]
    assert format_budget(limit, statements, cwd=CWD).splitlines()[0] == first_line


def test_budget_message_lists_statements() -> None:
    site = CallSite(filename="/work/project/app/a.py", lineno=7, function="run")
    long_sql = "SELECT " + "x" * 300
    statements = [
        StatementEvent("SELECT 1", site, None),
        StatementEvent(long_sql, None, None),
    ]
    lines = format_budget(1, statements, cwd=CWD).splitlines()
    assert lines[0] == "queryguard: expected at most 1 query, got 2"
    assert "  1. SELECT 1  (app/a.py:7 in run)" in lines
    assert f"  2. {long_sql[:200]}..." in lines


def test_repeat_message_truncates_long_sql() -> None:
    long_sql = "SELECT " + "x" * 300
    lines = format_findings([RepeatFinding(long_sql, 9, None)]).splitlines()
    assert f"  kind: repeated-statement  label: {long_sql[:200]}*" in lines
    assert all(len(line) < 250 for line in lines)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT " + ", ".join(f"col{i}" for i in range(60)) + " FROM t WHERE id = ?",
        "SELECT * FROM t WHERE id = ANY(ARRAY[?, ?])",
        "SELECT * FROM [dbo].[t] WHERE id = ?",
        "SELECT x FROM t WHERE id IN (__[POSTCOMPILE_id_1])",
    ],
)
def test_a_long_or_bracketed_statement_label_still_works_as_a_pattern(
    sql: str,
) -> None:
    from pytest_queryguard.ignores import parse_rule
    from pytest_queryguard.messages import make_finding

    finding = make_finding(RepeatFinding(sql, 6, None), cwd=CWD)
    text = _kind_line_of(finding.message).removeprefix("  kind: ")
    kind, _, label = text.partition("  label: ")
    assert parse_rule(f"{kind} {label}").matches(finding), text


def test_missing_callsite_prints_unknown_location() -> None:
    finding = RelationshipFinding("Order.customer", 3, None, None)
    out = format_findings([finding], cwd=CWD)
    assert "  first at <unknown location>" in out.splitlines()


def test_path_outside_cwd_stays_absolute_and_blocks_are_separated() -> None:
    site = CallSite(filename="/elsewhere/lib.py", lineno=1, function="f")
    findings: list[Finding] = [
        RelationshipFinding("A.b", 2, site, None),
        RepeatFinding("SELECT ?", 9, site),
    ]
    out = format_findings(findings, cwd=CWD)
    assert "  first at /elsewhere/lib.py:1 in f" in out
    assert out.count("queryguard: N+1 detected") == 1
    assert "\n\n  the same statement" in out


def _scan(
    issue: ScanIssue, sql: str = "SELECT 1", site: CallSite | None = None
) -> ScanEvent:
    return ScanEvent(issue, sql, site)


def test_index_message_for_a_filter_with_hint() -> None:
    site = CallSite(
        filename="/work/project/app/orders.py", lineno=12, function="orders_for"
    )
    sql = "SELECT orders.id\nFROM orders\nWHERE orders.customer_id = %s::INTEGER"
    issue = ScanIssue(
        "public",
        "orders",
        "Seq Scan",
        "filter",
        "(orders.customer_id = $1)",
        "customer_id",
        show_schema=False,
    )
    assert format_index_findings([_scan(issue, sql, site)], cwd=CWD).splitlines() == [
        "queryguard: missing index",
        "",
        "  orders is read in full: Seq Scan with filter (orders.customer_id = $1)",
        "  first at app/orders.py:12 in orders_for",
        "  kind: missing-index  label: orders",
        "  sql: SELECT orders.id FROM orders WHERE orders.customer_id = %s::INTEGER",
        "  fix: CREATE INDEX ON orders (customer_id)",
        "",
        "  a sequential scan is fine on a small lookup table:",
        "  list it in queryguard_index_ignore_tables",
    ]


def test_index_message_without_hint_in_another_schema() -> None:
    issue = ScanIssue(
        "Sales", "order", "Seq Scan", "filter", "((o.a = $1) AND (o.b = $2))", None
    )
    lines = format_index_findings([_scan(issue)], cwd=CWD).splitlines()
    assert lines[2:7] == [
        (
            '  "Sales"."order" is read in full: Seq Scan with filter '
            "((o.a = $1) AND (o.b = $2))"
        ),
        "  first at <unknown location>",
        '  kind: missing-index  label: "Sales"."order"',
        "  sql: SELECT 1",
        "  fix: add an index that matches this condition",
    ]


def test_index_message_suggests_an_expression_index() -> None:
    issue = ScanIssue(
        "public",
        "orders",
        "Seq Scan",
        "filter",
        "((orders.total)::integer = $1)",
        None,
        show_schema=False,
        expression=True,
    )
    lines = format_index_findings([_scan(issue)], cwd=CWD).splitlines()
    assert lines[6] == (
        "  fix: add an index that matches this condition "
        "(an expression index may be needed)"
    )


def test_index_message_for_a_trailing_index_column() -> None:
    issue = ScanIssue(
        "public",
        "orders",
        "Index Scan using orders_tenant_id_status_idx",
        "index condition",
        "(orders.status = $1)",
        "status",
        show_schema=False,
        index="index orders_tenant_id_status_idx",
    )
    lines = format_index_findings([_scan(issue)], cwd=CWD).splitlines()
    assert lines[2] == (
        "  orders scans the whole index orders_tenant_id_status_idx: the index "
        "condition (orders.status = $1) does not match the start of any index"
    )
    assert "read in full" not in "\n".join(lines)
    assert lines[6] == "  fix: CREATE INDEX ON orders (status)"


def test_index_message_names_the_index_and_quotes_the_hint() -> None:
    issue = ScanIssue(
        "public",
        "Order",
        "Index Scan using Order_pkey",
        "filter",
        '("Order"."customerId" = $1)',
        '"customerId"',
        show_schema=False,
        index="index Order_pkey",
    )
    lines = format_index_findings([_scan(issue)], cwd=CWD).splitlines()
    assert lines[2] == (
        '  "Order" scans the whole index Order_pkey: the filter '
        '("Order"."customerId" = $1) does not match the start of any index'
    )
    assert lines[6] == '  fix: CREATE INDEX ON "Order" ("customerId")'


def test_index_message_separates_blocks_and_truncates() -> None:
    long_condition = "(t.a = $1)" + " AND (t.b = $2)" * 30
    issues = [
        ScanIssue("public", "a", "Seq Scan", "filter", "(a.x = $1)", "x"),
        ScanIssue("public", "b", "Seq Scan", "filter", long_condition, None),
    ]
    out = format_index_findings([_scan(i, "SELECT " + "y" * 300) for i in issues])
    assert out.count("queryguard: missing index") == 1
    assert out.count("is read in full") == 2
    assert out.count("small lookup table") == 1
    assert all(len(line) < 260 for line in out.splitlines())


def test_index_notes_list_notices_then_the_first_error() -> None:
    notices = {"sqlite statements were not checked": None}
    assert format_index_notes(notices, 0, None) == [
        "sqlite statements were not checked"
    ]
    assert format_index_notes(notices, 3, ("boom", "SELECT\n  1")) == [
        "sqlite statements were not checked",
        "3 statements could not be explained; the first: boom",
        "  SELECT 1",
    ]
    assert format_index_notes([], 1, ("boom", "SELECT 1"))[0] == (
        "1 statement could not be explained; the first: boom"
    )


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0.0000004, "0 us"),
        (0.000037, "37 us"),
        (0.00099, "990 us"),
        (0.001, "1.0 ms"),
        (0.4, "400.0 ms"),
        (0.9994, "999.4 ms"),
        (1.0, "1.00 s"),
        (1.254, "1.25 s"),
    ],
)
def test_format_duration_uses_microseconds_below_a_millisecond(
    seconds: float, text: str
) -> None:
    assert format_duration(seconds) == text


def test_partial_index_skips_are_counted_in_one_line() -> None:
    assert format_index_notes([], 0, None, [("t1",)]) == [
        (
            "1 statement skipped: a partial index on t1 may serve it; "
            "the generic plan cannot tell"
        )
    ]
    assert format_index_notes([], 0, None, [("t1",), ("orders", "t1")]) == [
        (
            "2 statements skipped: a partial index on orders, t1 may serve them; "
            "the generic plan cannot tell"
        )
    ]


def test_a_scan_of_an_existing_index_does_not_say_the_table_is_read_in_full() -> None:
    issue = ScanIssue(
        None,
        "users",
        "SCAN INDEX ix_users_created_at",
        "filter",
        "users.status = ?",
        "status",
        show_schema=False,
        index="index ix_users_created_at",
    )
    lines = format_index_findings([_scan(issue)], cwd=CWD).splitlines()
    assert lines[2] == (
        "  users scans the whole index ix_users_created_at: the filter "
        "users.status = ? does not match the start of any index"
    )
    assert "read in full" not in "\n".join(lines)
    assert lines[6] == "  fix: CREATE INDEX ON users (status)"


def test_a_join_condition_on_a_walked_index_says_join_condition() -> None:
    issue = ScanIssue(
        None,
        "users",
        "SCAN INDEX ix",
        "join condition",
        "users.org_id = orgs.id",
        None,
        show_schema=False,
        index="primary key",
    )
    lines = format_index_findings([_scan(issue)], cwd=CWD).splitlines()
    assert lines[2].startswith(
        "  users scans the whole primary key: the join condition"
    )


def _repeat_message(sql: str) -> str:
    site = CallSite(filename="/work/project/app/users.py", lineno=7, function="load")
    return format_findings([RepeatFinding(sql, 5, site)], cwd=CWD)


def test_a_repeated_statement_without_in_suggests_one_query_with_in() -> None:
    message = _repeat_message("SELECT users.id FROM users WHERE users.id = ?")
    assert "fix: fetch all rows in one query (WHERE id IN (...))" in message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT users.id FROM users WHERE users.id IN (...)",
        "SELECT users.id FROM users WHERE users.id NOT IN (...) ORDER BY users.id",
    ],
)
def test_a_repeated_statement_that_already_uses_in_says_the_calls_repeat(
    sql: str,
) -> None:
    message = _repeat_message(sql)
    assert "already fetches several rows with IN (...)" in message
    assert "call it once with all the ids, or combine the calls" in message
    assert "fetch all rows in one query" not in message


def _kind_line_of(message: str) -> str:
    (line,) = [line for line in message.splitlines() if line.startswith("  kind: ")]
    return line


def _detections() -> list[object]:
    from pytest_queryguard.detectors import ColumnLoadFinding, WriteLoopFinding
    from pytest_queryguard.events import CAUSE_DEFERRED, CAUSE_EXPIRED, CAUSE_REFRESH

    site = CallSite(filename="/work/project/app/a.py", lineno=3, function="run")
    return [
        RelationshipFinding("Order.customer", 5, site, None),
        RepeatFinding("SELECT * FROM t WHERE id = ?", 5, site),
        WriteLoopFinding("INSERT INTO t (a) VALUES (?)", "INSERT", 5, site),
        ColumnLoadFinding("Customer", (), CAUSE_EXPIRED, 5, 5, site, None),
        ColumnLoadFinding("Customer", ("a", "b"), CAUSE_DEFERRED, 5, 5, site, None),
        ColumnLoadFinding("Customer", (), CAUSE_REFRESH, 5, 5, site, None),
    ]


def test_every_finding_names_its_kind_and_label_on_one_line() -> None:
    from pytest_queryguard.messages import make_finding

    for detection in _detections():
        finding = make_finding(detection, cwd=CWD)  # type: ignore[arg-type]
        assert _kind_line_of(finding.message) == (
            f"  kind: {finding.kind}  label: {finding.label}"
        )


def test_the_line_gives_a_working_ignore_pattern() -> None:
    from pytest_queryguard.ignores import parse_rule
    from pytest_queryguard.messages import make_finding

    for detection in _detections():
        finding = make_finding(detection, cwd=CWD)  # type: ignore[arg-type]
        text = _kind_line_of(finding.message).removeprefix("  kind: ")
        kind, _, label = text.partition("  label: ")
        assert parse_rule(f"{kind} {label}").matches(finding), text


def test_an_index_finding_and_an_unused_eager_load_name_theirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue = ScanIssue(
        "Sales", "order", "Seq Scan", "filter", "(o.a = $1)", "a", show_schema=True
    )
    message = format_index_findings([_scan(issue)], cwd=CWD)
    assert '  kind: missing-index  label: "Sales"."order"' in message.splitlines()

    from pytest_queryguard import eager

    class Load:
        label = "Order.customer"
        loader = "joinedload"
        callsite = None

    monkeypatch.setattr(eager, "unused", lambda tracker, sieve=None: [Load()])
    (finding,) = eager.findings(object())  # type: ignore[arg-type]
    assert "  kind: unused-eager-load  label: Order.customer" in finding.message


def test_the_closing_note_names_a_whole_index_scan_when_no_table_was_read() -> None:
    walked = ScanIssue(
        "public",
        "rw_link",
        "Bitmap Index Scan using rw_link_pkey",
        "index condition",
        "(rw_link.hero_id = $1)",
        "hero_id",
        show_schema=False,
        index="index rw_link_pkey",
    )
    seq = ScanIssue("public", "a", "Seq Scan", "filter", "(a.x = $1)", "x")
    only_walked = format_index_findings([_scan(walked)], cwd=CWD)
    assert "  rw_link scans the whole index rw_link_pkey: the index condition" in (
        only_walked
    )
    assert "a whole-index scan is fine on a small lookup table" in only_walked
    assert "sequential scan" not in only_walked
    mixed = format_index_findings([_scan(walked), _scan(seq)], cwd=CWD)
    assert "a sequential scan is fine on a small lookup table" in mixed


def test_relationship_message_names_the_parent_site_and_chains_the_loader() -> None:
    site = CallSite(filename="/work/project/app/views.py", lineno=30, function="render")
    parents = CallSite(
        filename="/work/project/app/orders.py", lineno=21, function="_base_query"
    )
    finding = RelationshipFinding(
        "Item.product",
        4,
        site,
        None,
        True,
        parent_site=parents,
        parent_root="Customer",
        parent_path=(("Customer.orders", False), ("Order.items", False)),
    )
    assert format_findings([finding], cwd=CWD).splitlines() == [
        "queryguard: N+1 detected",
        "",
        "  Item.product was lazy-loaded 4 times",
        "  first at app/views.py:30 in render",
        "  parents loaded at app/orders.py:21 in _base_query",
        "  kind: relationship-load  label: Item.product",
        "  fix: load it with the parent query, e.g.",
        (
            "       select(Customer).options(selectinload(Customer.orders)"
            ".selectinload(Order.items).joinedload(Item.product))"
        ),
    ]


def test_relationship_message_counts_other_parent_sites() -> None:
    parents = CallSite(filename="/work/project/a.py", lineno=3, function="f")
    finding = RelationshipFinding(
        "Order.customer", 4, None, None, parent_site=parents, other_parent_sites=2
    )
    lines = format_findings([finding], cwd=CWD).splitlines()
    assert "  parents loaded at a.py:3 in f and 2 other places" in lines
    # No known chain: the hint is the relationship's own class, as before.
    assert lines[-1] == "       select(Order).options(selectinload(Order.customer))"
