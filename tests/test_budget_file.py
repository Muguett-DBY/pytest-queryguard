"""The default budget and the budget file (#232, #244)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pytest_queryguard import budgets

MODELS = (Path(__file__).parent / "models.py").read_text()

SUITE = """
import pytest
from qgmodels import Order, make_sync_session

QUERIES = {"few": 1, "many": 3}


@pytest.mark.parametrize("name", ["few", "many"])
def test_queries(name):
    session = make_sync_session()
    for _ in range(QUERIES[name]):
        session.query(Order).filter(Order.id == 1).all()


@pytest.mark.max_queries(100)
def test_marked():
    session = make_sync_session()
    session.query(Order).all()
"""


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS, test_suite=SUITE)
    # repeats are allowed here: only the budgets are under test
    pytester.makeini("[pytest]\nqueryguard_repeat_threshold = 50\n")
    return pytester


def _run(project: pytest.Pytester, *args: str) -> pytest.RunResult:
    return project.runpytest_subprocess("-p", "no:cacheprovider", *args)


def _budgets(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    assert data["format"] == 1
    budgets_: dict[str, Any] = data["budgets"]
    return budgets_


def _counts(project: pytest.Pytester) -> dict[str, int]:
    _run(project, "--queryguard-json=c.json")
    tests = json.loads((project.path / "c.json").read_text())["tests"]
    return {k: v["statements"] for k, v in tests.items()}


def test_update_writes_every_count_and_the_file_then_holds(
    project: pytest.Pytester,
) -> None:
    counts = _counts(project)
    result = _run(project, "--queryguard-budgets=b.json", "--queryguard-update-budgets")
    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(
        ["*queryguard: budgets updated*", "*b.json: 2 entries (2 added, 0 removed*"]
    )
    # the test with its own marker has no entry
    del counts["test_suite.py::test_marked"]
    assert _budgets(project.path / "b.json") == counts
    _run(project, "--queryguard-budgets=b.json").assert_outcomes(passed=3)


def test_a_test_above_its_budget_fails_and_says_where_the_limit_comes_from(
    project: pytest.Pytester,
) -> None:
    counts = _counts(project)
    many = "test_suite.py::test_queries[many]"
    budgets.write(project.path / "b.json", {**counts, many: counts[many] - 2})
    result = _run(project, "--queryguard-budgets=b.json")
    result.assert_outcomes(passed=2, failed=1)
    result.stdout.fnmatch_lines(
        [
            f"*queryguard: expected at most {counts[many] - 2} queries, got {counts[many]}*",
            (
                "*(the limit is the budget of this test in b.json; if the new count "
                "is right, update it with --queryguard-update-budgets)*"
            ),
        ]
    )


def test_a_marker_wins_over_the_file_and_the_default(project: pytest.Pytester) -> None:
    budgets.write(project.path / "b.json", {"test_suite.py::test_marked": 0})
    project.makeini(
        "[pytest]\nqueryguard_repeat_threshold = 50\nqueryguard_max_queries = 0\n"
        "queryguard_budgets = b.json\n"
    )
    result = _run(project)
    # the marker allows 100; both parametrized tests exceed the default of 0
    result.assert_outcomes(passed=1, failed=2)
    result.stdout.fnmatch_lines(
        [
            "*(the limit is the queryguard_max_queries ini option, the default budget of tests without a marker)*"
        ]
    )


def test_the_file_wins_over_the_default(project: pytest.Pytester) -> None:
    counts = _counts(project)
    budgets.write(project.path / "b.json", counts)
    project.makeini(
        "[pytest]\nqueryguard_repeat_threshold = 50\nqueryguard_max_queries = 0\n"
    )
    _run(project, "--queryguard-budgets=b.json").assert_outcomes(passed=3)


def test_a_partial_update_keeps_the_entries_of_tests_that_did_not_run(
    project: pytest.Pytester,
) -> None:
    budgets.write(
        project.path / "b.json",
        {"test_suite.py::test_other": 7, "test_suite.py::test_deleted": 9},
    )
    result = _run(
        project,
        "--queryguard-budgets=b.json",
        "--queryguard-update-budgets",
        "-k",
        "few or many",
    )
    result.stdout.fnmatch_lines(["*did not cover the whole suite*"])
    after = _budgets(project.path / "b.json")
    assert after["test_suite.py::test_other"] == 7
    assert after["test_suite.py::test_deleted"] == 9
    assert "test_suite.py::test_queries[few]" in after


def test_a_full_update_drops_tests_that_no_longer_exist(
    project: pytest.Pytester,
) -> None:
    budgets.write(project.path / "b.json", {"test_suite.py::test_deleted": 9})
    _run(project, "--queryguard-budgets=b.json", "--queryguard-update-budgets")
    assert "test_suite.py::test_deleted" not in _budgets(project.path / "b.json")


def test_a_run_that_stops_early_does_not_update(project: pytest.Pytester) -> None:
    project.makepyfile(test_zfail="def test_fails():\n    assert False\n")
    result = _run(
        project, "-x", "--queryguard-budgets=b.json", "--queryguard-update-budgets"
    )
    result.stdout.fnmatch_lines(
        ["*queryguard: budgets*", "*not updated: the run stopped early*"]
    )
    assert not (project.path / "b.json").exists()


@pytest.mark.parametrize("workers", [2, 3])
def test_an_update_with_xdist_has_the_tests_of_every_worker(
    project: pytest.Pytester, workers: int
) -> None:
    pytest.importorskip("xdist")
    counts = _counts(project)
    _run(
        project,
        "-n",
        str(workers),
        "--queryguard-budgets=b.json",
        "--queryguard-update-budgets",
    ).assert_outcomes(passed=3)
    del counts["test_suite.py::test_marked"]
    assert _budgets(project.path / "b.json") == counts


def test_xdist_with_a_filter_keeps_the_other_entries(
    project: pytest.Pytester,
) -> None:
    pytest.importorskip("xdist")
    budgets.write(project.path / "b.json", {"test_suite.py::test_other": 7})
    _run(
        project,
        "-n",
        "2",
        "-k",
        "few or many",
        "--queryguard-budgets=b.json",
        "--queryguard-update-budgets",
    )
    assert _budgets(project.path / "b.json")["test_suite.py::test_other"] == 7


@pytest.mark.parametrize(
    ("args", "ini", "message"),
    [
        (
            ["--queryguard-budgets=missing.json"],
            "",
            "*does not exist; create it with --queryguard-update-budgets*",
        ),
        (
            ["--queryguard-update-budgets"],
            "",
            "*--queryguard-update-budgets needs a budget file*",
        ),
        (
            [],
            "queryguard_max_queries = -1",
            "*queryguard_max_queries must be an integer >= 0*",
        ),
        (
            [],
            "queryguard_max_queries = lots",
            "*queryguard_max_queries must be an integer >= 0*",
        ),
        (
            [
                "--queryguard-mode=off",
                "--queryguard-update-budgets",
                "--queryguard-budgets=b.json",
            ],
            "",
            "*cannot run with measurement off*",
        ),
    ],
)
def test_usage_errors(
    project: pytest.Pytester, args: list[str], ini: str, message: str
) -> None:
    project.makeini(f"[pytest]\n{ini}\n")
    result = _run(project, *args)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([message])


def test_a_broken_file_is_a_usage_error_but_an_update_replaces_it(
    project: pytest.Pytester,
) -> None:
    (project.path / "b.json").write_text('{"format": 1, "budgets": {"t": -1}}')
    result = _run(project, "--queryguard-budgets=b.json")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*must be a non-negative integer*"])
    result = _run(project, "--queryguard-budgets=b.json", "--queryguard-update-budgets")
    result.stdout.fnmatch_lines(["*the previous file was replaced*"])
    assert len(_budgets(project.path / "b.json")) == 2


def test_describe_counts_the_changes() -> None:
    before = {"a": 1, "b": 5, "c": 3}
    after = {"a": 2, "b": 4, "c": 3, "d": 1}
    assert budgets.describe(before, after) == (
        "4 entries (1 added, 0 removed, 1 raised, 1 lowered)"
    )


def test_a_broken_file_is_not_replaced_by_a_partial_update(
    project: pytest.Pytester,
) -> None:
    (project.path / "b.json").write_text("<<<<<<< HEAD")
    result = _run(
        project,
        "--queryguard-budgets=b.json",
        "--queryguard-update-budgets",
        "-k",
        "few",
    )
    result.stdout.fnmatch_lines(["*not updated: cannot read budget file*"])
    assert (project.path / "b.json").read_text() == "<<<<<<< HEAD"


EXTRA = """
import pytest
from qgmodels import Order, make_sync_session


@pytest.mark.skip(reason="no postgres here")
def test_skipped():
    pass


@pytest.fixture
def broken():
    raise RuntimeError("setup failed")


def test_setup_error(broken):
    pass


def test_fails_halfway():
    session = make_sync_session()
    session.query(Order).all()
    assert False
"""


def test_tests_without_a_count_keep_their_entries(project: pytest.Pytester) -> None:
    project.makepyfile(test_extra=EXTRA)
    budgets.write(
        project.path / "b.json",
        {
            "test_extra.py::test_skipped": 4,
            "test_extra.py::test_setup_error": 5,
            "test_extra.py::test_fails_halfway": 50,
            "test_extra.py::test_deleted": 6,
        },
    )
    result = _run(project, "--queryguard-budgets=b.json", "--queryguard-update-budgets")
    result.stdout.fnmatch_lines(["*failing tests kept their previous budget*"])
    after = _budgets(project.path / "b.json")
    assert after["test_extra.py::test_skipped"] == 4
    assert after["test_extra.py::test_setup_error"] == 5
    # it stopped early: its low count would become a budget that fails later
    assert after["test_extra.py::test_fails_halfway"] == 50
    assert "test_extra.py::test_deleted" not in after


def test_the_default_budget_does_not_fail_the_update(project: pytest.Pytester) -> None:
    project.makeini(
        "[pytest]\nqueryguard_repeat_threshold = 50\nqueryguard_max_queries = 0\n"
    )
    result = _run(project, "--queryguard-budgets=b.json", "--queryguard-update-budgets")
    result.assert_outcomes(passed=3)
    # and the written numbers then win over the default
    _run(project, "--queryguard-budgets=b.json").assert_outcomes(passed=3)


def test_the_json_says_which_tests_have_a_marker(project: pytest.Pytester) -> None:
    _run(project, "--queryguard-json=c.json")
    tests = json.loads((project.path / "c.json").read_text())["tests"]
    assert tests["test_suite.py::test_marked"]["max_queries_marker"] is True
    assert tests["test_suite.py::test_queries[few]"]["max_queries_marker"] is False
