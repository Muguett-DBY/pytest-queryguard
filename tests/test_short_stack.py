from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pytest_queryguard import CallSite, callsite, record_queries, sarif
from pytest_queryguard.callsite import find_callsite
from pytest_queryguard.findings import KIND_RELATIONSHIP, Finding

# pytester runs sessions in-process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone

MODELS = (Path(__file__).parent / "models.py").read_text()

HELPERS = """from qgmodels import Order, make_sync_session


def names(orders):
    found = []
    for order in orders:
        found.append(order.customer.name)  # first at
    return found


def page(session):
    return names(session.query(Order).all())  # page


def test_loop():
    session = make_sync_session()
    page(session)  # test
"""


def _line(source: str, marker: str) -> int:
    return next(
        i for i, text in enumerate(source.splitlines(), 1) if text.endswith(marker)
    )


# --- recording the callers ---


def _inner() -> CallSite:
    site = find_callsite()
    assert site is not None
    return site


def _outer() -> CallSite:
    return _inner()


@pytest.fixture
def fresh() -> None:
    callsite.new_measurement()


@pytest.mark.usefixtures("fresh")
def test_the_callers_are_recorded_innermost_first() -> None:
    site = _outer()
    assert site.function == "_inner"
    functions = [c.function for c in site.callers]
    assert functions == ["_outer", "test_the_callers_are_recorded_innermost_first"]
    assert all(c.filename.endswith("test_short_stack.py") for c in site.callers)


@pytest.mark.usefixtures("fresh")
def test_the_depth_limits_the_callers() -> None:
    old = callsite.swap_stack_depth(1)
    try:
        site = _outer()
    finally:
        callsite.swap_stack_depth(old)
    assert [c.function for c in site.callers] == ["_outer"]


@pytest.mark.usefixtures("fresh")
def test_depth_zero_records_no_callers() -> None:
    old = callsite.swap_stack_depth(0)
    try:
        site = _outer()
    finally:
        callsite.swap_stack_depth(old)
    assert site.callers == ()


def test_the_callers_are_not_part_of_the_identity() -> None:
    plain = CallSite(filename="a.py", lineno=1, function="f")
    called = CallSite(filename="a.py", lineno=1, function="f", callers=(plain,))
    assert plain == called and hash(plain) == hash(called)
    assert repr(plain) == repr(called)


def _from_a() -> CallSite:
    return _inner()


def _from_b() -> CallSite:
    return _inner()


@pytest.mark.usefixtures("fresh")
def test_the_first_run_of_a_line_in_a_measurement_is_kept() -> None:
    site = _from_a()
    assert _from_b() is site
    assert site.callers[0].function == "_from_a"


@pytest.mark.usefixtures("fresh")
def test_a_nested_measurement_keeps_the_callers_of_earlier_sites() -> None:
    site = _from_a()
    with record_queries():  # a measurement inside the test's own
        later = _from_b()
    assert later == site
    assert site.callers[0].function == "_from_a"
    assert later.callers[0].function == "_from_b"


def _raises_through_pytest() -> CallSite:
    found: list[CallSite] = []

    def boom() -> None:
        found.append(_inner())
        raise ZeroDivisionError

    pytest.raises(ZeroDivisionError, boom)
    return found[0]


@pytest.mark.usefixtures("fresh")
def test_pytest_frames_between_user_frames_are_skipped() -> None:
    old = callsite.swap_stack_depth(5)
    try:
        site = _raises_through_pytest()
    finally:
        callsite.swap_stack_depth(old)
    assert [c.function for c in site.callers] == [
        "boom",
        "_raises_through_pytest",
        "test_pytest_frames_between_user_frames_are_skipped",
    ]


# --- the report ---


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS, test_a=HELPERS)
    return pytester


def test_the_report_shows_the_callers_under_first_at(
    project: pytest.Pytester,
) -> None:
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            f"  first at test_a.py:{_line(HELPERS, '# first at')} in names",
            f"    called from test_a.py:{_line(HELPERS, '# page')} in page",
            f"    called from test_a.py:{_line(HELPERS, '# test')} in test_loop",
            "  parents loaded at test_a.py:* in page",
        ],
        consecutive=True,
    )


def test_the_depth_option_limits_the_report(project: pytest.Pytester) -> None:
    project.makeini("[pytest]\nqueryguard_stack_depth = 1\n")
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "  first at test_a.py:* in names",
            "    called from test_a.py:* in page",
            "  parents loaded at test_a.py:* in page",
        ],
        consecutive=True,
    )


def test_depth_zero_shows_no_callers(project: pytest.Pytester) -> None:
    project.makeini("[pytest]\nqueryguard_stack_depth = 0\n")
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["  first at test_a.py:* in names"])
    result.stdout.no_fnmatch_line("*called from*")


@pytest.mark.parametrize("value", ["-1", "many"])
def test_a_bad_depth_is_a_usage_error(project: pytest.Pytester, value: str) -> None:
    project.makeini(f"[pytest]\nqueryguard_stack_depth = {value}\n")
    result = project.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*queryguard_stack_depth must be an integer >= 0*"])


def test_sarif_and_json_carry_the_callers(project: pytest.Pytester) -> None:
    result = project.runpytest(
        "--queryguard-sarif=qg.sarif", "--queryguard-json=qg.json"
    )
    result.assert_outcomes(failed=1)
    stack = [
        {"file": "test_a.py", "function": "page", "line": _line(HELPERS, "# page")},
        {
            "file": "test_a.py",
            "function": "test_loop",
            "line": _line(HELPERS, "# test"),
        },
    ]
    tests = json.loads((project.path / "qg.json").read_text())["tests"]
    assert tests["test_a.py::test_loop"]["findings"][0]["stack"] == stack
    (found,) = json.loads((project.path / "qg.sarif").read_text())["runs"][0]["results"]
    (flow,) = found["codeFlows"]
    (thread,) = flow["threadFlows"]
    steps = [
        (
            step["location"]["physicalLocation"]["artifactLocation"]["uri"],
            step["location"]["physicalLocation"]["region"]["startLine"],
            step["location"]["message"]["text"],
        )
        for step in thread["locations"]
    ]
    assert steps == [
        ("test_a.py", _line(HELPERS, "# test"), "test_loop"),
        ("test_a.py", _line(HELPERS, "# page"), "page"),
        ("test_a.py", _line(HELPERS, "# first at"), "names"),
    ]


ASYNC = """import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


async def fetch(session, i):
    return await session.execute(text("select :i"), {"i": i})  # first at


async def page(session):
    for i in range(6):
        await fetch(session, i)  # page


async def main():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with AsyncSession(engine) as session:
        await page(session)  # main
    await engine.dispose()


def test_async():
    asyncio.run(main())  # test
"""


def test_async_callers_are_found_through_the_parent_greenlet(
    pytester: pytest.Pytester,
) -> None:
    pytest.importorskip("aiosqlite")
    pytest.importorskip("greenlet")
    pytester.makepyfile(test_async_a=ASYNC)
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            f"  first at test_async_a.py:{_line(ASYNC, '# first at')} in fetch",
            f"    called from test_async_a.py:{_line(ASYNC, '# page')} in page",
            f"    called from test_async_a.py:{_line(ASYNC, '# main')} in main",
            f"    called from test_async_a.py:{_line(ASYNC, '# test')} in test_async",
        ],
        consecutive=True,
    )


# --- SARIF without the plugin ---


def _finding(**kwargs: Any) -> Finding:
    values: dict[str, Any] = {
        "kind": KIND_RELATIONSHIP,
        "label": "Order.customer",
        "file": "app/orders.py",
        "function": "list_orders",
        "message": "  Order.customer was lazy-loaded 3 times",
        "line": 12,
        "count": 3,
    }
    values.update(kwargs)
    return Finding(**values)


def _result(finding: Finding, prefix: str = "") -> dict[str, Any]:
    store: sarif.Collected = {}
    sarif.add(store, finding, "error")
    (result,) = sarif.build(store, "1.2.3", prefix)[0]["runs"][0]["results"]
    return result  # type: ignore[no-any-return]


def test_no_stack_no_code_flow() -> None:
    assert "codeFlows" not in _result(_finding())


def test_callers_outside_the_repository_are_left_out_of_the_flow() -> None:
    stack = (("/usr/lib/other.py", 3, "outside"), ("app\\views.py", 30, "view"))
    result = _result(_finding(stack=stack), prefix="sub")
    uris = [
        step["location"]["physicalLocation"]["artifactLocation"]["uri"]
        for step in result["codeFlows"][0]["threadFlows"][0]["locations"]
    ]
    assert uris == ["sub/app/views.py", "sub/app/orders.py"]


def test_only_callers_outside_the_repository_give_no_flow() -> None:
    stack = (("/usr/lib/other.py", 3, "outside"),)
    assert "codeFlows" not in _result(_finding(stack=stack))


def test_the_stack_survives_the_wire_and_is_not_identity() -> None:
    stack = (("app/views.py", 30, "view"),)
    store: sarif.Collected = {}
    sarif.add(store, _finding(stack=stack), "error")
    other: sarif.Collected = {}
    sarif.merge_wire(other, json.loads(json.dumps(sarif.to_wire(store))))
    (finding, _) = other[_finding().identity]
    assert finding.stack == stack
    assert _finding(stack=stack) == _finding()
