"""The index advisor without a database: candidates, existing indexes, wording."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from pytest_queryguard import advisor, hypopg
from pytest_queryguard.advisor import ExistingIndex
from pytest_queryguard.events import (
    Advice,
    CallSite,
    IndexKey,
    ScanEvent,
    ScanIssue,
    Verification,
)
from pytest_queryguard.findings import IndexAdvice
from pytest_queryguard.indexes import find_scans, table_label
from pytest_queryguard.messages import format_index_findings, make_index_finding

PLANS = Path(__file__).parent / "fixtures" / "plans"
VARCHAR = {
    ("public", "orders"): frozenset({"status"}),
    ("public", "customers"): frozenset({"email", "name"}),
}


def issues_of(name: str, version: int = 17) -> list[ScanIssue]:
    document = json.loads((PLANS / f"pg{version}" / f"{name}.json").read_text())
    leading = {("public", "order_items_order_product"): "order_id"}
    return find_scans(
        document["plan"][0]["Plan"], "public", leading if version < 18 else {}, VARCHAR
    )


def advise_fixture(name: str, version: int = 17) -> list[str | None]:
    found: list[str | None] = []
    for issue in issues_of(name, version):
        analysis = advisor.analyse(
            issue, VARCHAR.get((issue.schema or "", issue.table), frozenset())
        )
        if analysis is None or analysis.plain is None:
            found.append(None)
        else:
            found.append(advisor.create_sql(table_label(issue), analysis.plain))
    return found


# name -> the CREATE INDEX for each finding of the plan, or None for no advice
EXPECTED: dict[str, list[str | None]] = {
    "eq_param": ["CREATE INDEX ON orders (customer_id)"],
    "eq_any_array": ["CREATE INDEX ON orders (customer_id)"],
    "eq_in_list": ["CREATE INDEX ON orders (customer_id)"],
    "varchar_cast": ["CREATE INDEX ON orders (status)"],
    "char_cast": ["CREATE INDEX ON orders (kind)"],
    "range_one_sided": ["CREATE INDEX ON orders (total)"],
    "range_two_sided": ["CREATE INDEX ON orders (total)"],
    "eq_and_range": ["CREATE INDEX ON orders (customer_id, total)"],
    "two_equalities": ["CREATE INDEX ON orders (customer_id, status)"],
    "order_by_with_equality": ["CREATE INDEX ON orders (customer_id, created_at DESC)"],
    "group_by_filter": ["CREATE INDEX ON orders (customer_id, status)"],
    "join_orders_customers": ["CREATE INDEX ON orders (customer_id)"],
    "correlated_subplan": ["CREATE INDEX ON orders (customer_id)"],
    "initplan": [
        "CREATE INDEX ON orders (customer_id)",
        "CREATE INDEX ON customers (email)",
    ],
    "quoted_identifiers": ['CREATE INDEX ON "Mixed Case" ("customerId")'],
    "quoted_alias": ['CREATE INDEX ON "Mixed Case" ("customerId")'],
    "trailing_index_column": ["CREATE INDEX ON order_items (product_id)"],
    "is_null": ["CREATE INDEX ON orders (note)"],
    "whole_index_order_limit": ["CREATE INDEX ON orders (customer_id)"],
    "function_lower": [None],
    "cast_date": [None],
    "coalesce": [None],
    "like": [None],
    "not_equal": [],  # no btree index serves <>: nothing is reported
    "array_any": [None],
    "json_arrow": [None],
    "or_two_columns": [None],
}


@pytest.mark.parametrize("version", [14, 15, 16, 17, 18])
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_suggestions_on_real_plans(name: str, version: int) -> None:
    if (name, version) == ("trailing_index_column", 18):
        # PostgreSQL 18 skip-scans the index, so the check reports nothing
        assert advise_fixture(name, version) == []
        return
    if (name, version) == ("group_by_filter", 18):
        # PostgreSQL 18 aggregates this by hashing, with no Sort for an index
        # to avoid
        assert advise_fixture(name, version) == ["CREATE INDEX ON orders (customer_id)"]
        return
    assert advise_fixture(name, version) == EXPECTED[name]


def _issue(
    conditions: Sequence[str],
    *,
    alias: str = "t",
    sort: Sequence[str] = (),
    limit: bool = False,
    schema: str = "public",
    show_schema: bool = False,
    table: str = "t",
) -> ScanIssue:
    return ScanIssue(
        schema,
        table,
        "Seq Scan",
        "filter",
        conditions[0],
        None,
        show_schema=show_schema,
        alias=alias,
        conditions=tuple(conditions),
        sort_keys=tuple(sort),
        limit=limit,
    )


def sql_of(issue: ScanIssue, varchar: frozenset[str] = frozenset()) -> str | None:
    analysis = advisor.analyse(issue, varchar)
    if analysis is None or analysis.plain is None:
        return None
    return advisor.create_sql(table_label(issue), analysis.plain)


def test_equality_columns_come_before_the_range_column() -> None:
    issue = _issue(["((t.d > $1) AND (t.a = $2) AND (t.b = $3))"])
    assert sql_of(issue) == "CREATE INDEX ON t (a, b, d)"


def test_only_the_first_range_column_is_a_key() -> None:
    issue = _issue(["((t.d > $1) AND (t.e < $2) AND (t.a = $3))"])
    assert sql_of(issue) == "CREATE INDEX ON t (a, d)"


def test_a_sort_that_the_index_can_avoid_replaces_the_range() -> None:
    sort = ["t.created DESC"]
    with_limit = _issue(["((t.a = $1) AND (t.d > $2))"], sort=sort, limit=True)
    assert sql_of(with_limit) == "CREATE INDEX ON t (a, created DESC)"
    # without a LIMIT, narrowing by the range matters more than the order
    without = _issue(["((t.a = $1) AND (t.d > $2))"], sort=sort)
    assert sql_of(without) == "CREATE INDEX ON t (a, d)"


def test_sort_keys_keep_direction_and_nulls_order() -> None:
    issue = _issue(
        ["(t.a = $1)"], sort=["t.b DESC NULLS LAST", "t.c", "t.a"], limit=True
    )
    assert sql_of(issue) == "CREATE INDEX ON t (a, b DESC NULLS LAST, c)"


def test_a_sort_on_an_expression_is_left_out() -> None:
    issue = _issue(["(t.a = $1)"], sort=["lower((t.b)::text)"])
    assert sql_of(issue) == "CREATE INDEX ON t (a)"
    other = _issue(["(t.a = $1)"], sort=["u.b"])
    assert sql_of(other) == "CREATE INDEX ON t (a)"


def test_at_most_four_key_columns() -> None:
    issue = _issue(
        ["((t.a = $1) AND (t.b = $2) AND (t.c = $3) AND (t.d = $4) AND (t.e = $5))"]
    )
    assert sql_of(issue) == "CREATE INDEX ON t (a, b, c, d)"


def test_conditions_without_a_plain_column_give_no_advice() -> None:
    for condition in [
        "(lower((t.a)::text) = $1)",
        "((t.a)::date = $1)",
        "(t.a <> $1)",
        "(t.a ~~ $1)",
        "((t.a = $1) OR (t.b = $2))",
        "(t.a IS NOT NULL)",
        "($1 = ANY (t.tags))",
        "(CASE WHEN t.a THEN 1 END = $1)",
    ]:
        assert sql_of(_issue([condition])) is None, condition


def test_a_text_cast_counts_only_for_a_varchar_column() -> None:
    issue = _issue(["((t.a)::text = $1)"])
    assert sql_of(issue) is None
    assert sql_of(issue, frozenset({"a"})) == "CREATE INDEX ON t (a)"


def test_an_expression_next_to_a_plain_column_still_advises_the_column() -> None:
    issue = _issue(["((t.a = $1) AND (lower((t.b)::text) = $2))"])
    assert sql_of(issue) == "CREATE INDEX ON t (a)"


def test_is_null_is_an_equality_for_a_btree() -> None:
    assert sql_of(_issue(["((t.a = $1) AND (t.d IS NULL))"])) == (
        "CREATE INDEX ON t (a, d)"
    )


def test_join_conditions_on_the_other_side_are_equalities() -> None:
    assert sql_of(_issue(["(t.a = u.b)"])) == "CREATE INDEX ON t (a)"
    assert sql_of(_issue(["(u.b = t.a)"])) == "CREATE INDEX ON t (a)"
    assert sql_of(_issue(["(t.a = t.b)"])) is None


def test_the_schema_is_shown_when_it_is_not_the_current_one() -> None:
    issue = _issue(["(t.a = $1)"], schema="audit", show_schema=True)
    assert sql_of(issue) == "CREATE INDEX ON audit.t (a)"


def test_an_unparsed_condition_gives_no_advice() -> None:
    assert sql_of(_issue(["(t.a = $1 +)"])) is None
    assert advisor.analyse(_issue(["(t.a = $1)"]), frozenset()) is not None
    no_conditions = ScanIssue("public", "t", "Seq Scan", "filter", "(t.a = $1)", "a")
    assert advisor.analyse(no_conditions, frozenset()) is None


def test_a_constant_predicate_gives_a_partial_candidate() -> None:
    issue = _issue(["((t.a = $1) AND (t.deleted_at IS NULL))"])
    analysis = advisor.analyse(issue, frozenset())
    assert analysis is not None and analysis.plain is not None
    assert analysis.partial is not None
    assert (
        advisor.create_sql("t", analysis.plain) == "CREATE INDEX ON t (a, deleted_at)"
    )
    assert advisor.create_sql("t", analysis.partial) == (
        "CREATE INDEX ON t (a) WHERE deleted_at IS NULL"
    )


def test_a_literal_equality_is_a_partial_candidate_but_a_parameter_is_not() -> None:
    literal = _issue(["((t.a = $1) AND ((t.s)::text = 'open'::text))"])
    analysis = advisor.analyse(literal, frozenset({"s"}))
    assert analysis is not None and analysis.partial is not None
    assert analysis.partial.where == "(s)::text = 'open'::text"
    parameter = _issue(["((t.a = $1) AND (t.d IS NULL) AND (t.s = $2))"])
    analysis = advisor.analyse(parameter, frozenset())
    assert analysis is not None and analysis.partial is not None
    assert analysis.partial.keys == (IndexKey("a"), IndexKey("s"))
    only_params = _issue(["((t.a = $1) AND (t.s = $2))"])
    analysis = advisor.analyse(only_params, frozenset())
    assert analysis is not None and analysis.partial is None


def test_no_partial_candidate_when_nothing_is_left_to_index() -> None:
    issue = _issue(["(t.d IS NULL)"])
    analysis = advisor.analyse(issue, frozenset())
    assert analysis is not None and analysis.plain is not None
    assert analysis.partial is None


# ------------------------------------------------------------ existing indexes


def _index(
    name: str = "t_a_idx",
    method: str = "btree",
    valid: bool = True,
    columns: tuple[str | None, ...] = ("a",),
    predicate: str | None = None,
) -> ExistingIndex:
    return ExistingIndex(name, method, valid, columns, predicate)


KEYS = (IndexKey("a"),)


def test_a_valid_index_for_the_columns_means_no_new_index() -> None:
    reason = advisor.blocking_index(KEYS, [_index()])
    assert reason is not None
    assert "t_a_idx on (a) exists and covers these columns" in reason
    assert "collation" in reason


def test_a_longer_index_with_the_columns_first_covers_them() -> None:
    assert advisor.blocking_index(KEYS, [_index(columns=("a", "b"))]) is not None


def test_an_index_with_the_columns_in_another_position_does_not_cover_them() -> None:
    assert advisor.blocking_index(KEYS, [_index(columns=("b", "a"))]) is None
    assert advisor.blocking_index(KEYS, [_index(columns=(None,))]) is None


def test_an_invalid_index_is_named_as_the_reason() -> None:
    reason = advisor.blocking_index(KEYS, [_index(valid=False)])
    assert reason is not None and "marked invalid" in reason


def test_a_partial_index_is_named_as_the_reason() -> None:
    reason = advisor.blocking_index(KEYS, [_index(predicate="(id > 5)")])
    assert reason is not None
    assert "partial (WHERE (id > 5))" in reason


def test_the_most_useful_reason_wins() -> None:
    reason = advisor.blocking_index(
        KEYS, [_index("p", predicate="x"), _index("i", valid=False), _index("v")]
    )
    assert reason is not None and "v on (a)" in reason
    reason = advisor.blocking_index(
        KEYS, [_index("p", predicate="x"), _index("i", valid=False)]
    )
    assert reason is not None and "marked invalid" in reason


def test_other_index_methods_and_hash_for_several_columns_do_not_count() -> None:
    assert advisor.blocking_index(KEYS, [_index(method="gin")]) is None
    assert advisor.blocking_index(KEYS, [_index(method="brin")]) is None
    two = (IndexKey("a"), IndexKey("b"))
    assert (
        advisor.blocking_index(two, [_index(method="hash", columns=("a", "b"))]) is None
    )
    assert advisor.blocking_index(KEYS, [_index(method="hash")]) is not None


def test_make_advice_skips_the_statement_when_an_index_blocks_it() -> None:
    issue = _issue(["(t.a = $1)"])
    candidate = advisor.Candidate(KEYS)
    advice = advisor.make_advice(issue, candidate, "t", [_index()])
    assert advice.create_sql is None and advice.alembic is None
    assert advice.skipped is not None and advice.keys == KEYS
    fresh = advisor.make_advice(issue, candidate, "t", [])
    assert fresh.create_sql == "CREATE INDEX ON t (a)"
    # a partial candidate is a different index from the one that exists
    partial = advisor.Candidate(KEYS, "d IS NULL")
    assert advisor.make_advice(issue, partial, "t", [_index()]).create_sql is not None


# ------------------------------------------------------------------- rendering


def test_index_name_follows_the_alembic_convention() -> None:
    assert advisor.index_name("orders", (IndexKey("customer_id"),)) == (
        "ix_orders_customer_id"
    )
    assert advisor.index_name("Mixed Case", (IndexKey('"customerId"'),)) == (
        "ix_Mixed_Case_customerId"
    )


def test_a_long_index_name_is_cut_to_63_bytes_with_a_stable_suffix() -> None:
    keys = tuple(IndexKey(f"column_number_{n}") for n in range(4))
    name = advisor.index_name("a_table_with_a_long_name", keys)
    assert len(name.encode()) <= 63
    assert name == advisor.index_name("a_table_with_a_long_name", keys)
    other = advisor.index_name("a_table_with_a_long_name", keys[:3] + (IndexKey("x"),))
    assert other != name


def test_alembic_snippet_for_a_plain_index_is_one_line() -> None:
    issue = _issue(["(t.a = $1)"], table="orders")
    snippet = advisor.alembic_snippet(issue, advisor.Candidate(KEYS))
    assert snippet == 'op.create_index("ix_orders_a", "orders", ["a"])'


def test_alembic_snippet_with_schema_where_and_order() -> None:
    issue = _issue(["(t.a = $1)"], schema="audit", show_schema=True, table="orders")
    candidate = advisor.Candidate(
        (IndexKey("a"), IndexKey('"b c"', " DESC")), "deleted_at IS NULL"
    )
    snippet = advisor.alembic_snippet(issue, candidate)
    assert snippet == (
        "op.create_index(\n"
        '    "ix_orders_a_b_c",\n'
        '    "orders",\n'
        '    ["a", sa.text("b c DESC")],\n'
        '    schema="audit",\n'
        '    postgresql_where=sa.text("deleted_at IS NULL"),\n'
        ")"
    )
    compile(snippet.replace("sa.", "str(").replace('")', '"))'), "<snippet>", "eval")


def test_render_rebuilds_a_condition_without_the_alias() -> None:
    from pytest_queryguard import predicates

    tree = predicates.parse("(((t.s)::text = 'a''b'::text) AND (t.d IS NULL))")
    assert tree is not None
    assert advisor.render(tree, "t") == ("(((s)::text = 'a''b'::text) AND (d IS NULL))")
    assert advisor._top("(a = b)") == "a = b"
    assert advisor._top("(a) AND (b)") == "(a) AND (b)"
    assert advisor._top("a = b") == "a = b"
    for unrenderable in ["(t.a = $1)", "(t.a = (SubPlan 1))"]:
        parsed = predicates.parse(unrenderable)
        assert parsed is not None
        with pytest.raises(advisor._Unrenderable):
            advisor.render(parsed, "t")


# ----------------------------------------------------------- messages, findings


def _scan(advice: Advice | None, sql: str = "SELECT 1") -> ScanEvent:
    issue = ScanIssue(
        "public",
        "orders",
        "Seq Scan",
        "filter",
        "(orders.customer_id = $1)",
        "customer_id",
        show_schema=False,
        advice=advice,
    )
    site = CallSite(filename="/work/app/orders.py", lineno=12, function="orders_for")
    return ScanEvent(issue, sql, site)


def _block(advice: Advice | None) -> list[str]:
    text = format_index_findings([_scan(advice)], cwd=Path("/work"))
    return text.splitlines()[2:-3]


def _advice(verification: Verification | None = None, **kwargs: Any) -> Advice:
    return Advice(
        keys=(IndexKey("customer_id"),),
        create_sql="CREATE INDEX ON orders (customer_id)",
        alembic='op.create_index("ix_orders_customer_id", "orders", ["customer_id"])',
        verification=verification,
        **kwargs,
    )


def test_message_without_advice_keeps_the_old_fix_line() -> None:
    assert _block(None)[-1] == "  fix: CREATE INDEX ON orders (customer_id)"


def test_message_with_advice_adds_the_alembic_snippet() -> None:
    assert _block(_advice())[5:] == [
        "  fix: CREATE INDEX ON orders (customer_id)",
        (
            '  alembic: op.create_index("ix_orders_customer_id", "orders", '
            '["customer_id"])'
        ),
    ]


def test_message_indents_a_multi_line_alembic_snippet() -> None:
    advice = Advice(
        keys=(IndexKey("a"),),
        create_sql="CREATE INDEX ON t (a)",
        alembic='op.create_index(\n    "x",\n    "t",\n)',
    )
    assert _block(advice)[6:] == [
        "  alembic: op.create_index(",
        '      "x",',
        '      "t",',
        "  )",
    ]


@pytest.mark.parametrize(
    ("verification", "line"),
    [
        (
            Verification(True, 417.0, 157.21, "Bitmap Index Scan"),
            (
                "  hypopg: the planner uses it (Bitmap Index Scan); "
                "estimated cost 417.00 -> 157.21"
            ),
        ),
        (
            Verification(True, 12.5, 12.5, "Index Scan"),
            (
                "  hypopg: the planner uses it (Index Scan); estimated cost "
                "12.50 -> 12.50 (no gain at the current table size)"
            ),
        ),
        (
            Verification(False, 10.0, 10.0),
            (
                "  hypopg: the planner would not use it for this statement, even "
                "with sequential scans off; estimated cost 10.00 -> 10.00 "
                "(no gain at the current table size)"
            ),
        ),
        (
            Verification(True, None, None, "Index Scan"),
            "  hypopg: the planner uses it (Index Scan)",
        ),
        (
            Verification(error="permission denied"),
            "  hypopg: could not verify this index: permission denied",
        ),
    ],
)
def test_message_reports_the_verification(
    verification: Verification, line: str
) -> None:
    assert _block(_advice(verification))[-1] == line


def test_message_for_a_skipped_suggestion_says_why() -> None:
    advice = Advice(keys=(IndexKey("a"),), skipped="index x exists and is invalid")
    assert _block(advice)[-1] == "  fix: no new index: index x exists and is invalid"


def test_finding_carries_the_advice() -> None:
    verification = Verification(True, 417.0, 157.21, "Index Scan", 8192)
    finding = make_index_finding(
        _scan(_advice(verification, where="d IS NULL")), cwd=Path("/work")
    )
    assert finding.advice == IndexAdvice(
        create_index="CREATE INDEX ON orders (customer_id)",
        alembic='op.create_index("ix_orders_customer_id", "orders", ["customer_id"])',
        columns=("customer_id",),
        where="d IS NULL",
        verified=True,
        cost_before=417.0,
        cost_after=157.21,
        size_bytes=8192,
    )
    assert make_index_finding(_scan(None), cwd=Path("/work")).advice is None
    skipped = make_index_finding(
        _scan(Advice(keys=(IndexKey("a"),), skipped="why")), cwd=Path("/work")
    )
    assert skipped.advice is not None
    assert skipped.advice.create_index is None and skipped.advice.skipped == "why"
    assert skipped.advice.verified is None
    errored = make_index_finding(
        _scan(_advice(Verification(error="boom"))), cwd=Path("/work")
    )
    assert errored.advice is not None
    assert errored.advice.verification_error == "boom"


def test_advice_does_not_change_the_identity_of_a_finding() -> None:
    plain = make_index_finding(_scan(None), cwd=Path("/work"))
    advised = make_index_finding(_scan(_advice()), cwd=Path("/work"))
    assert plain.identity == advised.identity


def test_scan_issues_compare_without_the_advisor_fields() -> None:
    one = ScanIssue("p", "t", "Seq Scan", "filter", "(a = $1)", "a")
    two = ScanIssue(
        "p", "t", "Seq Scan", "filter", "(a = $1)", "a", alias="t", advice=Advice()
    )
    assert one == two and hash(one) == hash(two)


# ----------------------------------------------------------------- the notices


def test_notice_when_hypopg_is_not_available_names_the_package() -> None:
    text = hypopg.notice((None, None), "auto", 17)
    assert text is not None
    assert "not available on this server" in text
    assert "apt install postgresql-17-hypopg" in text
    assert "queryguard_hypopg = off" in text


def test_notice_when_hypopg_is_available_but_not_installed() -> None:
    text = hypopg.notice((None, "1.4.3"), "auto", 16)
    assert text is not None
    assert "CREATE EXTENSION hypopg" in text and "queryguard_hypopg = on" in text


def test_no_notice_when_installed_or_switched_off() -> None:
    assert hypopg.notice(("1.4.3", "1.4.3"), "auto", 17) is None
    assert hypopg.notice((None, None), "off", 17) is None


def test_notice_without_a_server_version() -> None:
    text = hypopg.notice((None, None), "auto", None)
    assert text is not None and "apt install hypopg" in text
