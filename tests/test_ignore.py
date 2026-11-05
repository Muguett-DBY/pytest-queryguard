from __future__ import annotations

from pathlib import Path

import pytest

from pytest_queryguard.findings import (
    KIND_MISSING_INDEX,
    KIND_RELATIONSHIP,
    KIND_REPEAT,
    Finding,
)
from pytest_queryguard.ignores import IgnoreRule, apply_rules, parse_rule, parse_rules

# pytester runs sessions in-process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone

MODELS = (Path(__file__).parent / "models.py").read_text()

LAZY = """
{marker}
def test_loop():
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name
"""
HEADER = """
import pytest
from qgmodels import Order, make_sync_session
"""
LOOP = HEADER + LAZY.format(marker="")


def _finding(
    kind: str = KIND_RELATIONSHIP,
    label: str = "Order.customer",
    file: str | None = "app/api/orders.py",
    function: str | None = "list_orders",
    stack: tuple[tuple[str, int, str], ...] = (),
) -> Finding:
    return Finding(
        kind=kind, label=label, file=file, function=function, message="m", stack=stack
    )


# --- pattern parsing and matching ---


def test_parse_kind_and_label() -> None:
    assert parse_rule("relationship-load Order.customer") == IgnoreRule(
        "relationship-load", "Order.customer"
    )


def test_parse_file_and_function_from_the_end() -> None:
    rule = parse_rule("relationship-load Order.* file=app/*.py function=list_*")
    assert rule == IgnoreRule("relationship-load", "Order.*", "app/*.py", "list_*")


def test_label_may_hold_spaces_and_options_may_come_in_any_order() -> None:
    rule = parse_rule(
        "repeated-statement SELECT * FROM t WHERE a = ? function=f file=x.py"
    )
    assert rule == IgnoreRule(
        "repeated-statement", "SELECT * FROM t WHERE a = ?", "x.py", "f"
    )


@pytest.mark.parametrize("line", ["", "relationship-load", "   "])
def test_parse_needs_kind_and_label(line: str) -> None:
    with pytest.raises(ValueError, match="expected 'KIND LABEL"):
        parse_rule(line)


def test_parse_rejects_a_kind_that_matches_nothing() -> None:
    with pytest.raises(ValueError, match="matches no finding kind"):
        parse_rule("relationshipload Order.customer")


def test_the_unknown_kind_error_lists_every_kind_comma_separated() -> None:
    with pytest.raises(ValueError) as caught:
        parse_rule("relationshipload Order.customer")
    assert str(caught.value).endswith(
        "(kinds: relationship-load, repeated-statement, column-load, "
        "write-loop, unused-eager-load, cartesian-load, missing-index)"
    )


def test_parse_accepts_kind_globs() -> None:
    assert parse_rule("* Order.customer").kind == "*"
    assert parse_rule("relationship-* Order.customer").kind == "relationship-*"


def test_parse_rules_skips_blank_lines_and_comments() -> None:
    rules = parse_rules(["", "# why", "missing-index public.orders", "  "])
    assert rules == (IgnoreRule("missing-index", "public.orders"),)


def test_str_round_trips() -> None:
    line = "relationship-load Order.* file=app/*.py function=list_*"
    assert str(parse_rule(line)) == line
    assert (
        str(parse_rule("missing-index public.orders")) == "missing-index public.orders"
    )


def test_rule_matches_kind_and_label() -> None:
    rule = IgnoreRule(KIND_RELATIONSHIP, "Order.*")
    assert rule.matches(_finding())
    assert not rule.matches(_finding(label="Customer.orders"))
    assert not rule.matches(_finding(kind=KIND_REPEAT))


def test_matching_is_case_sensitive() -> None:
    assert not IgnoreRule(KIND_RELATIONSHIP, "order.customer").matches(_finding())


def test_rule_with_file_and_function() -> None:
    rule = IgnoreRule("*", "*", "app/api/*.py", "list_orders")
    assert rule.matches(_finding())
    assert not rule.matches(_finding(file="app/jobs/x.py"))
    assert not rule.matches(_finding(function="other"))


def test_file_or_function_pattern_never_matches_an_unknown_location() -> None:
    assert not IgnoreRule("*", "*", file="*").matches(_finding(file=None))
    assert not IgnoreRule("*", "*", function="*").matches(_finding(function=None))
    assert IgnoreRule("*", "*").matches(_finding(file=None, function=None))


def test_parse_stack() -> None:
    rule = parse_rule("relationship-load Order.* stack=app/admin/* file=app/*.py")
    assert rule == IgnoreRule(
        "relationship-load", "Order.*", "app/*.py", None, "app/admin/*"
    )
    assert str(rule) == "relationship-load Order.* file=app/*.py stack=app/admin/*"


STACK = (("app/api/views.py", 12, "export_orders"), ("tests/test_x.py", 3, "test_x"))


@pytest.mark.parametrize(
    "pattern",
    [
        "app/api/orders.py",  # the first location
        "app/api/views.py",  # a caller, as file
        "app/api/*",
        "app/api/views.py:export_orders",  # a caller, as file:function
        "*:export_*",
        "tests/*",
    ],
)
def test_stack_matches_any_frame_as_file_or_file_function(pattern: str) -> None:
    assert IgnoreRule("*", "*", stack=pattern).matches(_finding(stack=STACK))


@pytest.mark.parametrize(
    "pattern", ["app/admin/*", "*:list_users", "export_orders", "app/api/views.py:x"]
)
def test_stack_matches_no_other_frame(pattern: str) -> None:
    assert not IgnoreRule("*", "*", stack=pattern).matches(_finding(stack=STACK))


def test_stack_needs_every_other_field_to_match_too() -> None:
    rule = IgnoreRule("*", "*", function="other", stack="app/api/views.py")
    assert not rule.matches(_finding(stack=STACK))


def test_stack_of_an_unknown_location_is_only_its_callers() -> None:
    unknown = _finding(file=None, function=None)
    assert not IgnoreRule("*", "*", stack="*").matches(unknown)
    caller = IgnoreRule("*", "*", stack="app/api/views.py")
    assert caller.matches(_finding(file=None, function=None, stack=STACK))


def test_apply_rules_returns_kept_findings_and_used_rules() -> None:
    keep = _finding(label="Customer.orders")
    drop = _finding()
    used_rule = IgnoreRule(KIND_RELATIONSHIP, "Order.customer")
    unused_rule = IgnoreRule(KIND_MISSING_INDEX, "orders")
    kept, used = apply_rules([keep, drop], [used_rule, unused_rule])
    assert kept == [keep]
    assert used == {used_rule}


# --- queryguard_ignore in a session ---


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS)
    return pytester


def _ini(project: pytest.Pytester, *lines: str) -> None:
    body = "\n".join(f"    {line}" for line in lines)
    project.makeini(f"[pytest]\nqueryguard_ignore =\n{body}\n")


def test_ini_ignore_by_kind_and_label(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=LOOP)
    _ini(project, "relationship-load Order.customer")
    result = project.runpytest()
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_other_findings_still_fail(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=LOOP)
    _ini(project, "relationship-load Customer.orders")
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Order.customer was lazy-loaded*"])


def test_ignoring_one_finding_keeps_the_rest_of_the_report(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a=HEADER
        + """
def test_loop():
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name
    for customer in {order.customer for order in session.query(Order).all()}:
        _ = customer.orders
"""
    )
    _ini(project, "relationship-load Order.customer")
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.no_fnmatch_line("*Order.customer was lazy-loaded*")


def test_ini_ignore_with_file_and_function(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=LOOP)
    _ini(project, "relationship-load Order.customer file=test_a.py function=test_loop")
    project.runpytest().assert_outcomes(passed=1)


def test_ini_ignore_with_another_file_does_not_match(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=LOOP)
    _ini(project, "relationship-load Order.customer file=other/*.py")
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        ["*stale ignores*", "*relationship-load Order.customer file=other/*.py"]
    )


def test_ignore_applies_in_warn_mode(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=LOOP)
    _ini(project, "relationship-load Order.customer")
    project.runpytest("--queryguard-mode=warn").assert_outcomes(passed=1, warnings=0)


def test_repeated_statement_ignore(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
from sqlalchemy import create_engine, text


def test_loop():
    with create_engine("sqlite://").connect() as conn:
        for i in range(6):
            conn.execute(text(f"select {i}"))
"""
    )
    project.runpytest().assert_outcomes(failed=1)
    _ini(project, "repeated-statement select ?")
    project.runpytest().assert_outcomes(passed=1)


def test_stale_ini_pattern_is_reported_in_the_summary(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(test_a="def test_ok():\n    pass\n")
    _ini(project, "relationship-load Gone.relation")
    result = project.runpytest()
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        [
            "*queryguard: stale ignores*",
            "queryguard_ignore patterns that matched no finding in this run:",
            "  relationship-load Gone.relation",
            "Remove them*",
        ]
    )


def test_used_pattern_is_not_stale_but_unused_one_is(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=LOOP)
    _ini(project, "relationship-load Order.customer", "relationship-load Gone.relation")
    result = project.runpytest()
    result.assert_outcomes(passed=1)
    out = result.stdout.str()
    assert "  relationship-load Gone.relation" in out
    assert "  relationship-load Order.customer\n" not in out


def test_no_stale_report_without_ignores(project: pytest.Pytester) -> None:
    project.makepyfile(test_a="def test_ok():\n    pass\n")
    result = project.runpytest()
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_no_stale_report_when_measurement_is_off(project: pytest.Pytester) -> None:
    project.makepyfile(test_a="def test_ok():\n    pass\n")
    _ini(project, "relationship-load Gone.relation")
    result = project.runpytest("--queryguard-mode=off")
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_no_stale_report_on_an_xdist_controller(project: pytest.Pytester) -> None:
    # The patterns match in the workers, so the controller would call all stale.
    project.makeconftest(
        """
        def pytest_configure(config):
            config.pluginmanager.register(object(), "dsession")
        """
    )
    project.makepyfile(test_a="def test_ok():\n    pass\n")
    _ini(project, "relationship-load Gone.relation")
    result = project.runpytest()
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_bad_ini_pattern_is_a_usage_error(project: pytest.Pytester) -> None:
    _ini(project, "relationship-load")
    result = project.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*queryguard_ignore*expected 'KIND LABEL*"])


def test_unknown_kind_in_ini_is_a_usage_error(project: pytest.Pytester) -> None:
    _ini(project, "n+1 Order.customer")
    result = project.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*queryguard_ignore*matches no finding kind*"])


def test_ini_ignore_for_a_missing_index(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pytest_queryguard import plugin
    from pytest_queryguard.collector import QueryCollector
    from pytest_queryguard.events import ScanEvent, ScanIssue

    issue = ScanIssue("public", "orders", "Seq Scan", "filter", "(a = $1)", "a")
    real = QueryCollector

    def with_scan() -> QueryCollector:
        collector = real()
        collector.scans.append(ScanEvent(issue, "SELECT 1", None))
        return collector

    monkeypatch.setattr(plugin, "QueryCollector", with_scan)
    project.makepyfile(test_a="def test_ok():\n    pass\n")
    project.makeini("[pytest]\n")
    result = project.runpytest("--queryguard-index-check")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*public.orders is read in full*"])
    _ini(project, "missing-index public.orders")
    result = project.runpytest("--queryguard-index-check")
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_ini_ignore_for_one_of_two_missing_indexes(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pytest_queryguard import plugin
    from pytest_queryguard.collector import QueryCollector
    from pytest_queryguard.events import ScanEvent, ScanIssue

    real = QueryCollector

    def with_scans() -> QueryCollector:
        collector = real()
        for table in ("orders", "audit"):
            issue = ScanIssue("public", table, "Seq Scan", "filter", "(a = $1)", "a")
            collector.scans.append(ScanEvent(issue, f"SELECT {table}", None))
        return collector

    monkeypatch.setattr(plugin, "QueryCollector", with_scans)
    project.makepyfile(test_a="def test_ok():\n    pass\n")
    _ini(project, "missing-index public.audit")
    result = project.runpytest("--queryguard-index-check")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*public.orders is read in full*"])
    result.stdout.no_fnmatch_line("*public.audit is read in full*")


# --- allow_nplusone with arguments ---


def test_marker_with_a_label_ignores_only_that_finding(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a=HEADER
        + LAZY.format(marker='@pytest.mark.allow_nplusone("Order.customer")')
    )
    result = project.runpytest()
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_marker_with_another_label_still_fails(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a=HEADER + LAZY.format(marker='@pytest.mark.allow_nplusone("Other.rel")')
    )
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*stale ignores*",
            "allow_nplusone patterns that matched no finding in their test:",
            "  test_a.py::test_loop: * Other.rel",
        ]
    )


def test_marker_with_keywords(project: pytest.Pytester) -> None:
    marker = (
        '@pytest.mark.allow_nplusone(kind="relationship-load", function="test_loop")'
    )
    project.makepyfile(test_a=HEADER + LAZY.format(marker=marker))
    project.runpytest().assert_outcomes(passed=1)


def test_marker_with_only_keywords_matches_any_label(
    project: pytest.Pytester,
) -> None:
    marker = '@pytest.mark.allow_nplusone(file="test_a.py")'
    project.makepyfile(test_a=HEADER + LAZY.format(marker=marker))
    project.runpytest().assert_outcomes(passed=1)


def test_bare_marker_still_ignores_everything(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a=HEADER + LAZY.format(marker="@pytest.mark.allow_nplusone")
    )
    result = project.runpytest()
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_marker_patterns_add_up_with_the_ini(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a=HEADER
        + LAZY.format(marker='@pytest.mark.allow_nplusone("Order.customer")')
    )
    _ini(project, "relationship-load Gone.relation")
    result = project.runpytest()
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        ["*stale ignores*", "  relationship-load Gone.relation"]
    )


@pytest.mark.parametrize(
    "marker",
    [
        "@pytest.mark.allow_nplusone(1)",
        '@pytest.mark.allow_nplusone(label="x")',
        '@pytest.mark.allow_nplusone("x", kind=3)',
        '@pytest.mark.allow_nplusone("")',
        "@pytest.mark.allow_nplusone(stack=1)",
    ],
)
def test_bad_marker_arguments_fail_the_test(
    project: pytest.Pytester, marker: str
) -> None:
    project.makepyfile(test_a=HEADER + LAZY.format(marker=marker))
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*allow_nplusone on*takes label patterns*"])


def test_marker_with_an_unknown_kind_fails_the_test(project: pytest.Pytester) -> None:
    marker = '@pytest.mark.allow_nplusone("x", kind="nope")'
    project.makepyfile(test_a=HEADER + LAZY.format(marker=marker))
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*allow_nplusone on*matches no finding kind*"])


# --- stack= ---

SHARED = """from qgmodels import Order


def names(session):
    return [order.customer.name for order in session.query(Order).all()]
"""
ADMIN = """import shared


def export_orders(session):
    return shared.names(session)
"""
CALLERS = """from qgmodels import make_sync_session

import admin_export
import shared
{marker}

def test_admin():
    admin_export.export_orders(make_sync_session())


def test_page():
    shared.names(make_sync_session())
"""


@pytest.fixture
def callers(project: pytest.Pytester) -> pytest.Pytester:
    project.makepyfile(shared=SHARED, admin_export=ADMIN)
    project.makepyfile(test_a=CALLERS.format(marker=""))
    return project


@pytest.mark.parametrize("pattern", ["admin_export.py", "*:export_orders"])
def test_ini_stack_ignores_the_helper_only_from_one_caller(
    callers: pytest.Pytester, pattern: str
) -> None:
    _ini(callers, f"relationship-load Order.customer stack={pattern}")
    result = callers.runpytest()
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["FAILED test_a.py::test_page*"])
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_ini_stack_beyond_the_stack_depth_does_not_match(
    callers: pytest.Pytester,
) -> None:
    callers.makeini(
        "[pytest]\nqueryguard_stack_depth = 0\n"
        "queryguard_ignore = relationship-load Order.customer stack=admin_export.py\n"
    )
    result = callers.runpytest()
    result.assert_outcomes(failed=2)
    result.stdout.fnmatch_lines(
        [
            "*stale ignores*",
            "  relationship-load Order.customer stack=admin_export.py",
        ]
    )


def test_ini_stack_matching_nothing_is_stale(callers: pytest.Pytester) -> None:
    _ini(callers, "relationship-load Order.customer stack=jobs/*")
    result = callers.runpytest()
    result.assert_outcomes(failed=2)
    result.stdout.fnmatch_lines(
        ["*stale ignores*", "  relationship-load Order.customer stack=jobs/*"]
    )


def test_marker_with_stack(callers: pytest.Pytester) -> None:
    marker = '\npytestmark = pytest.mark.allow_nplusone(stack="admin_export.py")\n'
    callers.makepyfile(test_a="import pytest\n" + CALLERS.format(marker=marker))
    result = callers.runpytest()
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        [
            "*stale ignores*",
            "  test_a.py::test_page: * * stack=admin_export.py",
            "FAILED test_a.py::test_page*",
        ]
    )


def test_strict_mode_honours_stack(callers: pytest.Pytester) -> None:
    _ini(callers, "relationship-load Order.customer stack=admin_export.py")
    result = callers.runpytest("--queryguard-strict")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*NPlusOneDetected*"])
