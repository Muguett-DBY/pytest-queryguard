from __future__ import annotations

import pytest

from pytest_queryguard import plugin

# pytester runs sessions in-process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone

QUERIES_TEST = """
import pytest
from sqlalchemy import create_engine, text

# The report counts statements; repeating one many times is not the point here.
pytestmark = pytest.mark.allow_nplusone
ENGINE = create_engine("sqlite://")


def run(n):
    with ENGINE.connect() as conn:
        for _ in range(n):
            conn.execute(text("select 1"))
"""


def _test_module(counts: list[int]) -> str:
    body = [QUERIES_TEST]
    for i, n in enumerate(counts):
        body.append(f"\ndef test_{i:02d}():\n    run({n})\n")
    return "".join(body)


def _report_lines(result: pytest.RunResult) -> list[str]:
    lines = result.outlines
    start = next(i for i, line in enumerate(lines) if "queryguard report" in line)
    out = []
    for line in lines[start + 1 :]:
        if line.startswith("="):
            break
        out.append(line)
    return out


def test_report_lists_tests_by_query_count(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_a=_test_module([1, 4, 2]))
    result = pytester.runpytest("--queryguard-report")
    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(["=*= queryguard report =*="])
    assert _report_lines(result) == [
        "    4  test_a.py::test_01",
        "    2  test_a.py::test_02",
        "    1  test_a.py::test_00",
    ]


def test_no_report_without_flag(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_a=_test_module([1, 4, 2]))
    result = pytester.runpytest()
    result.assert_outcomes(passed=3)
    result.stdout.no_fnmatch_line("*queryguard report*")


def test_report_keeps_run_order_for_ties(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_a=_test_module([2, 3, 2, 2]))
    result = pytester.runpytest("--queryguard-report")
    assert _report_lines(result) == [
        "    3  test_a.py::test_01",
        "    2  test_a.py::test_00",
        "    2  test_a.py::test_02",
        "    2  test_a.py::test_03",
    ]


def test_report_shows_top_ten(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_a=_test_module(list(range(1, 13))))
    result = pytester.runpytest("--queryguard-report")
    result.assert_outcomes(passed=12)
    lines = _report_lines(result)
    assert len(lines) == 10
    assert lines[0] == "   12  test_a.py::test_11"
    assert lines[-1] == "    3  test_a.py::test_02"


def test_report_counts_tests_without_queries(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_a=_test_module([0]))
    result = pytester.runpytest("--queryguard-report")
    assert _report_lines(result) == ["    0  test_a.py::test_00"]


def test_report_includes_failing_tests(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_a=QUERIES_TEST + "\ndef test_fails():\n    run(3)\n    assert False\n"
    )
    result = pytester.runpytest("--queryguard-report")
    result.assert_outcomes(failed=1)
    assert _report_lines(result) == ["    3  test_a.py::test_fails"]


def test_report_with_no_measured_tests(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_a="def test_nothing():\n    pass\n")
    result = pytester.runpytest("--queryguard-report", "-k", "no_such_test")
    assert _report_lines(result) == ["no tests were measured"]


def test_report_in_off_mode_says_measurement_is_off(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(test_a=_test_module([1, 4]))
    pytester.makeini("[pytest]\nqueryguard_mode = off\n")
    result = pytester.runpytest("--queryguard-report")
    result.assert_outcomes(passed=2)
    assert _report_lines(result) == [
        "queryguard report: measurement is off (queryguard_mode = off)"
    ]


def test_report_error_does_not_break_the_session(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("report broke")

    monkeypatch.setattr(plugin, "_write_report", boom)
    pytester.makepyfile(test_a=_test_module([1]))
    result = pytester.runpytest("--queryguard-report")
    result.assert_outcomes(passed=1, warnings=1)
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(["*QueryGuardInternalWarning*report broke*"])


def test_report_after_internal_error_says_it_is_incomplete(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise ValueError("detect broke")

    monkeypatch.setattr(plugin, "detect", boom)
    pytester.makepyfile(
        test_a="""
        from sqlalchemy import create_engine, text

        ENGINE = create_engine("sqlite://")


        def run(n):
            with ENGINE.connect() as conn:
                for _ in range(n):
                    conn.execute(text("select 1"))


        def test_first():
            run(2)


        def test_second():
            run(3)
        """
    )
    result = pytester.runpytest("--queryguard-report")
    result.assert_outcomes(passed=2, warnings=1)
    assert _report_lines(result) == [
        "queryguard report: incomplete, measurement stopped after an internal error",
        "    2  test_a.py::test_first",
    ]
