"""Fault injection: no internal error may change the outcome of a user's test.

Every listener, detector, report writer, the SQLite index check and the pytest
hooks are made to raise, one at a time, while a small suite runs in-process
with every feature on. The contract (docs/stability.md, "Failing open"):

- An ``Exception`` (``RuntimeError``, ``MemoryError``, ``RecursionError`` ...)
  never changes the outcome of a test the plugin does not judge, never errors
  a test, never ends the session with an ``INTERNALERROR`` and is reported
  once: one ``QueryGuardInternalWarning`` in the warnings summary and one
  "queryguard: measurement stopped" section (or, for the end-of-session file
  writers, the red "queryguard: baseline" or "queryguard: SARIF" section).
  A test the plugin judges (an N+1, a missing index, a budget) may then pass:
  measurement stopped.
- A ``BaseException`` that is not an ``Exception`` (``KeyboardInterrupt``,
  ``SystemExit``, ``asyncio.CancelledError``) is a signal, not a bug: the
  plugin never swallows it, and its own state (the collector stack, the eager
  load tracker, the recorded error) is cleaned up so the next session measures
  as usual.
"""

from __future__ import annotations

import asyncio
import importlib
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from pytest_queryguard import (
    QueryGuardInternalWarning,
    baseline,
    collector,
    eager,
    listeners,
    messages,
    plugin,
    unused_columns,
)

MODELS = (Path(__file__).parent / "models.py").read_text()

SUITE = """
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, selectinload

from pytest_queryguard import record_queries
from qgmodels import Customer, Order, make_sync_session


# --- tests whose outcome the plugin decides

def test_nplusone():
    session = make_sync_session()
    for order in session.scalars(select(Order)).all():
        _ = order.customer.name
    session.close()


def test_missing_index():
    session = make_sync_session()
    assert session.query(Order).filter(Order.customer_id == 1).count() == 2
    session.close()


@pytest.mark.max_queries(1)
@pytest.mark.max_db_time(0)
def test_budgets():
    session = make_sync_session()
    session.get(Customer, 1)
    session.get(Customer, 2)
    session.close()


def test_unused_eager_load():
    session = make_sync_session()
    rows = session.scalars(select(Order).options(selectinload(Order.customer))).all()
    assert [order.id for order in rows]  # Order.customer is never read
    session.close()


class WideBase(DeclarativeBase):
    pass


class Wide(WideBase):
    __tablename__ = "wide"

    id: Mapped[int] = mapped_column(primary_key=True)
    c1: Mapped[str] = mapped_column(default="v")
    c2: Mapped[str] = mapped_column(default="v")
    c3: Mapped[str] = mapped_column(default="v")
    c4: Mapped[str] = mapped_column(default="v")
    c5: Mapped[str] = mapped_column(default="v")
    c6: Mapped[str] = mapped_column(default="v")
    c7: Mapped[str] = mapped_column(default="v")


def test_unused_columns():
    engine = create_engine("sqlite://")
    WideBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Wide(id=1))
        session.commit()
        session.expunge_all()
        assert [w.c1 for w in session.scalars(select(Wide)).all()]


# --- tests whose outcome only the user's code decides

def test_plain():
    session = make_sync_session()
    customer = session.get(Customer, 1)
    session.expire(customer, ["name"])
    assert customer.name == "customer 1"  # a column load
    session.close()


def test_transactions():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text("INSERT INTO t (v) VALUES ('a')"))
            nested = conn.begin_nested()
            conn.execute(text("INSERT INTO t (v) VALUES ('b')"))
            nested.rollback()
        trans = conn.begin()
        conn.execute(text("DELETE FROM t"))
        trans.rollback()
        assert conn.execute(text("SELECT count(*) FROM t")).scalar() == 1
    engine.dispose()


def test_record_queries_api():
    session = make_sync_session()
    with record_queries() as log:
        session.get(Customer, 3)
    assert log.count <= 1
    session.close()


def test_user_failure():
    session = make_sync_session()
    session.get(Customer, 1)
    session.close()
    assert False, "the user's own failure"
"""

JUDGED = {
    "test_nplusone",
    "test_missing_index",
    "test_budgets",
    "test_unused_eager_load",
    "test_unused_columns",
}
USER = {
    "test_plain": "passed",
    "test_transactions": "passed",
    "test_record_queries_api": "passed",
    "test_user_failure": "failed",
}

COMMON = (
    "-p",
    "no:randomly",
    "-p",
    "no:cacheprovider",
    "--queryguard-index-check",
    "--queryguard-report",
    "--queryguard-sarif=out.sarif",
    "--queryguard-baseline=qg.json",
    "-o",
    "queryguard_unused_eager_load=fail",
    "-o",
    "queryguard_unused_columns=fail",
    "-o",
    "queryguard_write_loop_threshold=2",
    "-o",
    "queryguard_ignore=repeated-statement *never*",
)
UPDATE = ("--queryguard-update-baseline",)

STOPPED = "queryguard: measurement stopped"


@pytest.fixture(autouse=True)
def _outer_measurement_off(request: pytest.FixtureRequest) -> Iterator[None]:
    """The patches below also reach this test; the outer plugin must not measure it."""
    stash = request.config.stash
    saved = stash[plugin._settings]
    stash[plugin._settings] = plugin._Settings("off", 2, 5)
    root = messages.swap_root(None)
    messages.swap_root(root)
    yield
    messages.swap_root(root)  # in case a broken cleanup did not restore it
    stash[plugin._settings] = saved
    eager._broken = False
    eager._warned = False
    unused_columns._broken = False
    unused_columns._warned = False


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS)
    pytester.makepyfile(test_suite=SUITE)
    (pytester.path / "qg.json").write_text(baseline.dumps([]))
    return pytester


@pytest.fixture
def faults() -> Iterator[pytest.MonkeyPatch]:
    """A MonkeyPatch of its own: undo() must not undo the chdir of pytester."""
    patch = pytest.MonkeyPatch()
    yield patch
    patch.undo()


def _resolve(spec: str) -> tuple[Any, str]:
    """("module:attr" or "module:Class.attr") -> (owner, attribute name)."""
    module_name, _, path = spec.partition(":")
    owner: Any = importlib.import_module(f"pytest_queryguard.{module_name}")
    *parents, attr = path.split(".")
    for parent in parents:
        owner = getattr(owner, parent)
    assert hasattr(owner, attr), spec
    return owner, attr


def _inject(
    monkeypatch: pytest.MonkeyPatch, spec: str, error: Callable[[], BaseException]
) -> list[str]:
    """Make the target raise; return the list that records each call."""
    owner, attr = _resolve(spec)
    calls: list[str] = []

    def broken(*args: object, **kwargs: object) -> Any:
        calls.append(spec)
        raise error()

    monkeypatch.setattr(owner, attr, broken)
    return calls


def _outcomes(result: pytest.RunResult) -> dict[str, tuple[str, str]]:
    """{test name: (outcome, failure text)}: the worst phase of each test wins."""
    found: dict[str, tuple[str, str]] = {}
    for report in result.reprec.getreports("pytest_runtest_logreport"):  # type: ignore[attr-defined]
        name = report.nodeid.rpartition("::")[2]
        if report.when != "call" and report.outcome == "failed":
            found[name] = ("error", report.longreprtext)
        elif report.when == "call" and name not in found:
            found[name] = (report.outcome, report.longreprtext)
    return found


def _assert_user_tests_unaffected(
    result: pytest.RunResult,
) -> dict[str, tuple[str, str]]:
    output = result.stdout.str() + result.stderr.str()
    assert "INTERNALERROR" not in output, output[-3000:]
    assert result.ret in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED), output[
        -3000:
    ]
    outcomes = _outcomes(result)
    assert set(outcomes) == JUDGED | set(USER), output[-3000:]
    for name, expected in USER.items():
        outcome, text = outcomes[name]
        assert outcome == expected, f"{name}: {outcome}\n{text}"
    assert "the user's own failure" in outcomes["test_user_failure"][1]
    for name in JUDGED:
        outcome, text = outcomes[name]
        assert outcome in ("passed", "failed"), f"{name}: {outcome}\n{text}"
        if outcome == "failed":
            assert text.startswith("queryguard:"), f"{name}:\n{text}"
    return outcomes


def _assert_reported_once(result: pytest.RunResult, message: str) -> None:
    """One warning and one summary section, or the section of a file writer.

    The unused eager-load check turns only itself off, with its own warning.
    A ``record_queries()`` block that ends after the error adds its documented
    "may be incomplete" warning; that one is not counted here.
    """
    output = result.stdout.str()
    stopped = re.findall(
        r"QueryGuardInternalWarning: pytest-queryguard disabled itself", output
    )
    eager_off = re.findall(
        r"QueryGuardInternalWarning: pytest-queryguard disabled the unused", output
    )
    sections = output.count(STOPPED)
    files = len(re.findall(r"queryguard (baseline|SARIF) could not write", output))
    assert message in output, output[-3000:]
    notes = output.count("queryguard index check")  # a statement it could not explain
    shown = (len(stopped), len(eager_off), sections, files, notes)
    assert shown in (
        (1, 0, 1, 0, 0),
        (0, 1, 0, 0, 0),
        (0, 0, 0, 1, 0),
        (0, 0, 0, 0, 1),
    ), f"(stopped, eager, sections, files, notes) = {shown}\n{output[-3000:]}"


def test_the_suite_without_faults(project: pytest.Pytester) -> None:
    result = project.runpytest(*COMMON)
    outcomes = _assert_user_tests_unaffected(result)
    # Every judged test fails without a fault: the faults below can only hide.
    assert {name for name in JUDGED if outcomes[name][0] == "failed"} == JUDGED
    assert STOPPED not in result.stdout.str()
    assert "QueryGuardInternalWarning" not in result.stdout.str()


# Where each target is looked up at run time, "module:attribute".
LISTENERS = [
    "listeners:find_callsite",
    "listeners:find_flush",
    "listeners:scope_of_session",
    "listeners:scope_of_connection",
    "listeners:_take_pending",
    "listeners:_is_write",
    "listeners:_record_column_load",
    "listeners:_site_of",
    "listeners:_parent_load",
    "collector:QueryCollector.add_statement",
    "collector:QueryCollector.add_relationship_load",
    "catalog:observe",
    "catalog:_resolve",  # the commit, rollback and savepoint listeners
]
INDEX_CHECK = [
    "sqlite_index:check",
    "sqlite_index:explain",
    "sqlite_index:analyse",
    "plugin:select_findings",
    "plugin:make_index_finding",
    "plugin:format_index_findings",
]
DETECTORS = [
    "plugin:detect",
    "plugin:make_findings",
    "plugin:format_findings",
    "plugin:format_budget",
    "plugin:format_time_budget",
    "plugin:apply_rules",
    "plugin:_apply_baseline",
    "baseline:split",
    "sarif:add",
    "eager:_elements",
    "eager:findings",
    "eager:start",
    "eager:stop",
    "unused_columns:_group",
    "unused_columns:findings",
    "unused_columns:start",
    "unused_columns:stop",
]
REPORTS = [
    "plugin:_write_report",
    "plugin:_write_index_notes",
    "plugin:format_stale_ignores",
    "plugin:format_stale_baseline",
    "sarif:build",
    "sarif:write",
    "sarif:workspace_prefix",
]
HOOKS = [
    "plugin:_read_settings",
    "plugin:install",
    "plugin:install_index_check",
    "plugin:swap_root",
    "eager:enable",
    "unused_columns:enable",
    "plugin:_record",
    "plugin:_note_index_check",
    "plugin:_results",
    "plugin:_start_measuring",
    "plugin:push",
    "plugin:pop",
    "plugin:_index_exemption",
    "plugin:_stop_measuring",
    "plugin:_budget",
    "plugin:_time_budget",
]
ALL_TARGETS = LISTENERS + INDEX_CHECK + DETECTORS + REPORTS + HOOKS


# swap_root() also runs in pytest_unconfigure, which then warns about it.
@pytest.mark.filterwarnings("ignore:pytest-queryguard could not clean up")
@pytest.mark.parametrize("error", [RuntimeError, MemoryError, RecursionError])
@pytest.mark.parametrize("target", ALL_TARGETS)
def test_an_exception_never_changes_a_user_test(
    project: pytest.Pytester,
    faults: pytest.MonkeyPatch,
    target: str,
    error: type[Exception],
) -> None:
    calls = _inject(faults, target, lambda: error(f"injected into {target}"))
    result = project.runpytest(*COMMON)
    faults.undo()
    assert calls, f"{target} was never called: the suite does not reach it"
    _assert_user_tests_unaffected(result)
    _assert_reported_once(result, f"injected into {target}")
    if target == "plugin:pop":
        # The collector stays on the stack, but every listener is off after
        # the error, so nothing reaches it.
        collector._reset_for_tests()
    assert collector.active() == ()
    assert eager._current is None
    assert unused_columns._current is None


@pytest.mark.parametrize(
    "marker",
    ["max_queries('x')", "max_db_time(-1)", "allow_missing_index(3)"],
)
def test_a_marker_error_still_fails_the_test_and_is_no_internal_error(
    pytester: pytest.Pytester, marker: str
) -> None:
    # pytest.fail raises a BaseException, which the fail-open guards around
    # the marker checks must let through.
    pytester.makepyfile(
        test_a=f"""
        import pytest


        @pytest.mark.{marker}
        def test_x():
            pass
        """
    )
    result = pytester.runpytest("--queryguard-index-check")
    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert marker.partition("(")[0] + " on test_a.py::test_x" in output
    assert STOPPED not in output
    assert "QueryGuardInternalWarning" not in output
    assert listeners.internal_error() is None
    assert collector.active() == ()


@pytest.mark.parametrize("error", [RuntimeError, MemoryError])
def test_an_exception_in_the_index_notes_never_changes_a_user_test(
    project: pytest.Pytester, faults: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    # The notes are written only when the check could not explain a statement.
    _inject(faults, "sqlite_index:analyse", lambda: RuntimeError("no plan"))
    calls = _inject(faults, "plugin:format_index_notes", lambda: error("notes broke"))
    result = project.runpytest(*COMMON)
    faults.undo()
    assert calls
    _assert_user_tests_unaffected(result)
    output = result.stdout.str()
    assert output.count("QueryGuardInternalWarning: pytest-queryguard disabled") == 1
    assert output.count(STOPPED) == 1
    assert "notes broke" in output


UPDATE_TARGETS = [
    "baseline:write",
    "plugin:sorted_identities",
    "plugin:_update_baseline",
    "plugin:_write_sarif",
    "plugin:_write_baseline_summary",
    "plugin:_write_sarif_summary",
]


@pytest.mark.parametrize("target", UPDATE_TARGETS + LISTENERS[:2] + DETECTORS[:1])
def test_an_exception_in_an_update_run_never_changes_a_user_test(
    project: pytest.Pytester, faults: pytest.MonkeyPatch, target: str
) -> None:
    calls = _inject(faults, target, lambda: RuntimeError(f"injected {target}"))
    result = project.runpytest(*COMMON, *UPDATE)
    faults.undo()
    if target != "plugin:sorted_identities":  # used only by xdist workers here
        assert calls, f"{target} was never called"
        _assert_reported_once(result, f"injected {target}")
    _assert_user_tests_unaffected(result)
    if target in ("baseline:write", "plugin:_update_baseline"):
        assert baseline.load(project.path / "qg.json") == frozenset()


def test_cleanup_errors_after_the_summary_do_not_break_the_run(
    project: pytest.Pytester, faults: pytest.MonkeyPatch
) -> None:
    calls = _inject(faults, "eager:disable", lambda: RuntimeError("cleanup"))
    # After the summary the warning goes to whoever handles warnings: here
    # the outer test, in a terminal Python's default handler (stderr).
    with pytest.warns(QueryGuardInternalWarning, match="could not clean up"):
        result = project.runpytest(*COMMON)
    faults.undo()
    assert calls
    _assert_user_tests_unaffected(result)
    # The other cleanup steps still ran.
    assert listeners.internal_error() is None
    eager.disable()  # the real one, for the enable() the broken one left
    assert not eager.is_enabled()


# ------------------------------------------------- signals are not swallowed

SIGNAL_TARGETS = [
    "listeners:find_callsite",
    "catalog:observe",
    "sqlite_index:check",
    "plugin:detect",
    "plugin:_record",
    "eager:stop",
    "unused_columns:stop",
    "plugin:_budget",
    "plugin:_time_budget",
    "plugin:_index_exemption",
    "plugin:_stop_measuring",
]


def _clean_state() -> None:
    assert collector.active() == ()
    assert eager._current is None
    assert unused_columns._current is None
    assert listeners.internal_error() is None


def _measures_normally(project: pytest.Pytester) -> None:
    result = project.runpytest(*COMMON)
    outcomes = _assert_user_tests_unaffected(result)
    assert {name for name in JUDGED if outcomes[name][0] == "failed"} == JUDGED


@pytest.mark.parametrize("target", SIGNAL_TARGETS)
@pytest.mark.parametrize(
    "error", [SystemExit, asyncio.CancelledError], ids=["SystemExit", "CancelledError"]
)
def test_a_signal_is_not_swallowed(
    project: pytest.Pytester,
    faults: pytest.MonkeyPatch,
    target: str,
    error: type[BaseException],
) -> None:
    calls = _inject(faults, target, lambda: error("signal"))
    result = project.runpytest(*COMMON)
    faults.undo()
    assert calls
    output = result.stdout.str()
    assert "INTERNALERROR" not in output, output[-3000:]
    assert STOPPED not in output  # the plugin did not take it for its own bug
    failed = [
        name
        for name, (outcome, text) in _outcomes(result).items()
        if outcome == "failed" and error.__name__ in text
    ]
    assert failed, output[-3000:]  # pytest reports it on the test it ended
    _clean_state()
    _measures_normally(project)


@pytest.mark.parametrize("target", SIGNAL_TARGETS)
def test_a_keyboard_interrupt_stops_the_run_and_leaves_no_state(
    project: pytest.Pytester, faults: pytest.MonkeyPatch, target: str
) -> None:
    calls = _inject(faults, target, KeyboardInterrupt)
    result = project.runpytest(*COMMON, *UPDATE, no_reraise_ctrlc=True)
    faults.undo()
    assert calls
    assert result.ret == pytest.ExitCode.INTERRUPTED
    assert STOPPED not in result.stdout.str()
    # An interrupted run never rewrites the baseline with partial findings.
    assert baseline.load(project.path / "qg.json") == frozenset()
    _clean_state()
    _measures_normally(project)


# ------------------------------------------------------------ pytest-xdist

XDIST_CONFTEST = """
import importlib

import pytest

SIDE, SPEC = {side!r}, {spec!r}


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # Not PYTEST_XDIST_WORKER: an outer xdist run passes it on to this process.
    if (SIDE == "worker") != hasattr(config, "workerinput"):
        return
    module, _, attr = SPEC.partition(":")
    owner = importlib.import_module("pytest_queryguard." + module)

    def broken(*args, **kwargs):
        raise RuntimeError("injected into " + SPEC)

    setattr(owner, attr, broken)
"""


@pytest.mark.parametrize(
    ("side", "spec"),
    [
        ("worker", "plugin:sorted_identities"),
        ("worker", "sarif:to_wire"),
        ("controller", "sarif:merge_wire"),
        ("controller", "plugin:_identity"),
    ],
)
def test_an_exception_in_the_xdist_hand_over_never_changes_a_user_test(
    project: pytest.Pytester, side: str, spec: str
) -> None:
    pytest.importorskip("xdist")
    project.makeconftest(XDIST_CONFTEST.format(side=side, spec=spec))
    result = project.runpytest_subprocess(*COMMON, *UPDATE, "-n", "2")
    output = result.stdout.str() + result.stderr.str()
    assert "INTERNALERROR" not in output, output[-3000:]
    assert result.ret == pytest.ExitCode.TESTS_FAILED, output[-3000:]
    # an update run reports no findings; the budgets and the user still fail
    result.assert_outcomes(passed=7, failed=2)
    # The run is known to be incomplete: no baseline with missing entries,
    # and the SARIF file says so.
    result.stdout.fnmatch_lines(
        [
            "*queryguard baseline not updated: the run stopped early*",
            "incomplete: measurement stopped*",
        ]
    )
    assert baseline.load(project.path / "qg.json") == frozenset()
    if side == "controller":
        assert f"injected into {spec}" in output
