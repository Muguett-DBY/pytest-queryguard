"""Per-test statement counts and findings as JSON (``--queryguard-json``, #230)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pytest_queryguard import testjson

MODELS = (Path(__file__).parent / "models.py").read_text()

SUITE = """
import pytest
from qgmodels import Customer, Order, make_sync_session


@pytest.mark.parametrize("i", range(2))
def test_nplusone(i):
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name


def test_clean():
    make_sync_session().query(Order).all()


def test_writes():
    session = make_sync_session()
    session.add(Customer(id=100, name="new"))
    session.commit()


@pytest.mark.allow_nplusone
def test_allowed():
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name


def test_not_measured_has_no_queries():
    pass
"""


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", "select"),
        ("  /* app:x */ select * from t", "select"),
        ("WITH a AS (SELECT 1) SELECT * FROM a", "select"),
        ("(SELECT 1) UNION (SELECT 2)", "select"),
        (
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION SELECT n + 1 FROM r) SELECT n FROM r",
            "select",
        ),
        (
            "WITH m AS (DELETE FROM a RETURNING *) INSERT INTO b SELECT * FROM m",
            "insert",
        ),
        ("with x as (select 1) update t set a = (select * from x)", "update"),
        ("WITH x AS (SELECT ')' AS p) DELETE FROM t", "delete"),
        ("INSERT INTO t VALUES (1)", "insert"),
        ("update t set a = 1", "update"),
        ("-- note\nDELETE FROM t", "delete"),
        ("BEGIN", "transaction"),
        ("SAVEPOINT sa_1", "transaction"),
        ("PRAGMA foreign_keys", "other"),
        ("", "other"),
        ("/* unterminated", "other"),
    ],
)
def test_the_verb_of_a_statement(sql: str, expected: str) -> None:
    assert testjson.verb(sql) == expected


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS, test_suite=SUITE)
    return pytester


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _check_suite(doc: dict[str, Any]) -> None:
    assert doc["format"] == 1
    assert doc["tool"]["name"] == "pytest-queryguard"
    assert doc["complete"] is True
    tests = doc["tests"]
    assert list(tests) == sorted(tests)
    nplus = tests["test_suite.py::test_nplusone[0]"]
    assert nplus["statements"] > 3
    # the session helper creates and seeds the tables inside the test too
    assert sum(nplus["by_verb"].values()) == nplus["statements"]
    assert nplus["by_verb"]["select"] >= 4
    assert nplus["truncated"] is False
    assert [(f["kind"], f["label"]) for f in nplus["findings"]] == [
        ("relationship-load", "Order.customer")
    ]
    assert nplus["findings"][0]["file"] == "test_suite.py"
    assert nplus["findings"][0]["count"] >= 2
    message = nplus["findings"][0]["message"]
    assert message.startswith("Order.customer was lazy-loaded")
    assert "\nfirst at " in message  # without the terminal report's indent
    assert tests["test_suite.py::test_clean"]["findings"] == []
    # one INSERT more than the seeding that every test does
    clean_inserts = tests["test_suite.py::test_clean"]["by_verb"]["insert"]
    assert tests["test_suite.py::test_writes"]["by_verb"]["insert"] == clean_inserts + 1
    # an allowed N+1 is counted but not a finding of the test
    allowed = tests["test_suite.py::test_allowed"]
    assert allowed["statements"] == nplus["statements"]
    assert allowed["findings"] == []
    assert tests["test_suite.py::test_not_measured_has_no_queries"]["statements"] == 0


def test_every_test_has_its_counts_and_findings(project: pytest.Pytester) -> None:
    result = project.runpytest_subprocess(
        "-p", "no:cacheprovider", "--queryguard-json", "out/counts.json"
    )
    result.assert_outcomes(passed=4, failed=2)
    result.stdout.fnmatch_lines(
        ["*queryguard: JSON written*", "*out*counts.json: 6 tests"]
    )
    _check_suite(_load(project.path / "out" / "counts.json"))


@pytest.mark.parametrize("workers", [2, 3])
def test_the_workers_entries_are_merged(project: pytest.Pytester, workers: int) -> None:
    pytest.importorskip("xdist")
    result = project.runpytest_subprocess(
        "-n", str(workers), "-p", "no:cacheprovider", "--queryguard-json=c.json"
    )
    result.assert_outcomes(passed=4, failed=2)
    _check_suite(_load(project.path / "c.json"))


def test_a_run_stopped_early_is_marked_incomplete(project: pytest.Pytester) -> None:
    project.runpytest_subprocess(
        "-p", "no:cacheprovider", "-x", "--queryguard-json=c.json"
    )
    doc = _load(project.path / "c.json")
    assert doc["complete"] is False
    assert len(doc["tests"]) < 6


def test_mode_off_writes_no_counts(project: pytest.Pytester) -> None:
    result = project.runpytest_subprocess(
        "-p", "no:cacheprovider", "--queryguard-mode=off", "--queryguard-json=c.json"
    )
    result.assert_outcomes(passed=6)
    assert _load(project.path / "c.json")["tests"] == {}


def test_an_empty_path_is_a_usage_error(project: pytest.Pytester) -> None:
    result = project.runpytest_subprocess(
        "-p", "no:cacheprovider", "--queryguard-json="
    )
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--queryguard-json needs a path*"])


def test_an_unwritable_path_is_reported_and_the_run_goes_on(
    project: pytest.Pytester,
) -> None:
    (project.path / "file").write_text("")
    result = project.runpytest_subprocess(
        "-p", "no:cacheprovider", "--queryguard-json=file/c.json"
    )
    result.assert_outcomes(passed=4, failed=2)
    result.stdout.fnmatch_lines(
        ["*queryguard: JSON*", "queryguard JSON could not write*"]
    )


def test_a_test_run_again_keeps_its_last_counts() -> None:
    tests: testjson.Tests = {}
    from pytest_queryguard.collector import QueryCollector

    first = QueryCollector()
    first.statement_count = 9
    testjson.record_counts(tests, "t.py::a", first)
    testjson.record_findings(tests, "t.py::a", [])
    tests["t.py::a"]["findings"].append({"kind": "x"})
    again = QueryCollector()
    testjson.record_counts(tests, "t.py::a", again)
    assert tests["t.py::a"]["statements"] == 0
    assert tests["t.py::a"]["findings"] == []


def test_baselined_findings_stay_in_the_file_while_the_baseline_is_updated(
    project: pytest.Pytester,
) -> None:
    project.runpytest_subprocess(
        "-p",
        "no:cacheprovider",
        "--queryguard-baseline=b.json",
        "--queryguard-update-baseline",
        "--queryguard-json=first.json",
    )
    first = _load(project.path / "first.json")["tests"]
    (finding,) = first["test_suite.py::test_nplusone[0]"]["findings"]
    assert finding["label"] == "Order.customer"
    # with the baseline in use, the same finding is listed and marked
    result = project.runpytest_subprocess(
        "-p",
        "no:cacheprovider",
        "--queryguard-baseline=b.json",
        "--queryguard-json=second.json",
    )
    result.assert_outcomes(passed=6)
    second = _load(project.path / "second.json")["tests"]
    (again,) = second["test_suite.py::test_nplusone[0]"]["findings"]
    assert again["baselined"] is True
    assert {**again, "baselined": False} == {**finding, "baselined": False}


def test_db_time_is_null_when_no_statement_was_timed() -> None:
    from pytest_queryguard.collector import QueryCollector
    from pytest_queryguard.events import StatementEvent

    tests: testjson.Tests = {}
    collector = QueryCollector()
    collector.add_statement(StatementEvent("SELECT 1", None, None))
    testjson.record_counts(tests, "t.py::a", collector)
    assert tests["t.py::a"]["db_time"] is None
    testjson.record_counts(tests, "t.py::b", QueryCollector())
    assert tests["t.py::b"]["db_time"] == 0.0
