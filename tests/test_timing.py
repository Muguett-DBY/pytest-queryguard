from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from pytest_queryguard import (
    QueryBudgetExceeded,
    QueryLog,
    max_db_time,
    max_queries,
    record_queries,
)

pytestmark = pytest.mark.allow_nplusone

MODELS = (Path(__file__).parent / "models.py").read_text()

SLEEP = 0.03


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _register(dbapi_connection: Any, record: Any) -> None:
        def nap(seconds: float) -> int:
            time.sleep(seconds)
            return 1

        dbapi_connection.create_function("nap", 1, nap)

    yield engine
    engine.dispose()


def _run(engine: Engine, sql: str) -> None:
    with engine.connect() as conn:
        conn.execute(text(sql)).all()


# ------------------------------------------------------------ the recorded log


def test_every_statement_has_a_duration(engine: Engine) -> None:
    with record_queries() as log:
        _run(engine, "select 1")
        _run(engine, f"select nap({SLEEP})")
    durations = [q.duration for q in log]
    assert all(d is not None and d >= 0 for d in durations)
    slow = log[-1].duration
    assert slow is not None and slow >= SLEEP * 0.9


def test_db_time_is_the_sum_of_the_durations(engine: Engine) -> None:
    with record_queries() as log:
        for _ in range(3):
            _run(engine, f"select nap({SLEEP})")
    total = sum(q.duration or 0.0 for q in log)
    assert log.db_time == pytest.approx(total)
    assert log.db_time >= 3 * SLEEP * 0.9


def test_slowest_orders_statements_by_duration(engine: Engine) -> None:
    with record_queries() as log:
        _run(engine, "select 1")
        _run(engine, f"select nap({SLEEP * 3})")
        _run(engine, f"select nap({SLEEP})")
    top = log.slowest(2)
    # Compare with the measured durations, not with the sleeps: a loaded CI
    # runner can stall a short nap for longer than a long one takes.
    expected = sorted(log, key=lambda q: q.duration or 0.0, reverse=True)[:2]
    assert [q.sql for q in top] == [q.sql for q in expected]
    assert [q.duration for q in top] == sorted(
        (q.duration for q in top), key=lambda d: d or 0.0, reverse=True
    )
    assert log.slowest(0) == ()
    assert len(log.slowest()) == 3


def test_slowest_rejects_a_bad_count() -> None:
    with record_queries() as log:
        pass
    for bad in (-1, True, 1.5, "2"):
        with pytest.raises(ValueError, match="non-negative integer"):
            log.slowest(bad)  # type: ignore[arg-type]


def test_a_statement_that_raises_has_no_duration(engine: Engine) -> None:
    with record_queries() as log, pytest.raises(Exception, match="no such table"):
        _run(engine, "select * from missing")
    assert len(log) == 1
    assert log[0].duration is None
    assert log.db_time == 0.0
    assert log.slowest() == ()


def test_a_log_without_statements_has_no_db_time() -> None:
    with record_queries() as log:
        pass
    assert log.db_time == 0.0
    assert log.slowest() == ()


def test_nested_logs_both_count_the_time(engine: Engine) -> None:
    with record_queries() as outer:
        with record_queries() as inner:
            _run(engine, f"select nap({SLEEP})")
        _run(engine, "select 1")
    assert inner.db_time >= SLEEP * 0.9
    assert outer.db_time >= inner.db_time


@pytest.mark.parametrize(
    "make_log", [record_queries, lambda: max_queries(2), lambda: max_db_time(60)]
)
def test_a_late_completion_does_not_change_a_finished_logs_db_time(
    make_log: Callable[[], AbstractContextManager[QueryLog]],
) -> None:
    started = Event()
    release = Event()
    engine = create_engine("sqlite://", poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def register(dbapi_connection: Any, record: Any) -> None:
        def wait_for_release() -> int:
            started.set()
            if not release.wait(timeout=10):
                raise TimeoutError("the test did not release the query")
            return 1

        dbapi_connection.create_function("wait_for_release", 0, wait_for_release)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool, record_queries() as outer:
            try:
                with make_log() as inner:
                    _run(engine, "select 1")
                    completed_time = inner.db_time
                    assert completed_time > 0
                    pending = pool.submit(_run, engine, "select wait_for_release()")
                    assert started.wait(timeout=10)
                    assert inner.count == 2
                    assert inner[-1].duration is None
                assert inner.db_time == completed_time
            finally:
                release.set()
            pending.result(timeout=10)
            # The outer recording is still live and sees the completion; the
            # inner log must keep the timing snapshot from its block's exit.
            assert outer.db_time > completed_time
            assert inner.db_time == completed_time
            assert inner[-1].duration is None
        assert outer.db_time == pytest.approx(sum(q.duration or 0 for q in outer))
    finally:
        engine.dispose()


def test_the_time_budget_judges_the_total_fixed_at_the_blocks_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A statement started in another thread finishes right after the log is
    # fixed and before the verdict. Its time must not decide the budget.
    started = Event()
    release = Event()
    engine = create_engine("sqlite://", poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def register(dbapi_connection: Any, record: Any) -> None:
        def slow_after_release() -> int:
            started.set()
            if not release.wait(timeout=10):
                raise TimeoutError("the test did not release the query")
            time.sleep(0.2)
            return 1

        dbapi_connection.create_function("slow_after_release", 0, slow_after_release)

    finish = QueryLog._finish

    def finish_then_complete(
        self: QueryLog, earlier: BaseException | None
    ) -> BaseException | None:
        result = finish(self, earlier)
        release.set()
        pending.result(timeout=10)
        return result

    monkeypatch.setattr(QueryLog, "_finish", finish_then_complete)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with max_db_time(0.1) as log:
                pending = pool.submit(_run, engine, "select slow_after_release()")
                assert started.wait(timeout=10)
            assert log.db_time < 0.1
    finally:
        release.set()
        engine.dispose()


def test_executemany_is_timed_once(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("create table t (a integer)"))
        with record_queries() as log:
            conn.execute(text("insert into t values (:a)"), [{"a": 1}, {"a": 2}])
    assert len(log) == 1
    assert log[0].duration is not None


def test_the_time_is_measured_around_the_cursor_call(engine: Engine) -> None:
    # Work in the caller between two statements is not database time.
    with record_queries() as log:
        _run(engine, "select 1")
        time.sleep(SLEEP * 3)
        _run(engine, "select 1")
    assert log.db_time < SLEEP * 3


# ------------------------------------------------------------ max_db_time


def test_max_db_time_passes_under_the_limit(engine: Engine) -> None:
    with max_db_time(5):
        _run(engine, "select 1")


def test_max_db_time_fails_over_the_limit_and_names_the_slowest(
    engine: Engine,
) -> None:
    with pytest.raises(QueryBudgetExceeded) as caught, max_db_time(SLEEP / 2):
        _run(engine, "select 1")
        _run(engine, f"select nap({SLEEP})")
    message = str(caught.value)
    assert "database time" in message
    assert "exceeded the budget of" in message
    assert "(2 statements)" in message
    assert "slowest:" in message
    slowest = message.splitlines()[message.splitlines().index("  slowest:") + 1]
    assert f"select nap({SLEEP})" in slowest
    assert "test_timing.py" in slowest  # the call site


def test_max_db_time_zero_fails_on_any_statement(engine: Engine) -> None:
    with pytest.raises(QueryBudgetExceeded), max_db_time(0):
        _run(engine, "select 1")


def test_max_db_time_with_no_statements_passes() -> None:
    with max_db_time(0):
        pass


@pytest.mark.parametrize("bad", [-1, -0.1, True, None, "1", float("nan"), float("inf")])
def test_max_db_time_rejects_an_invalid_limit(bad: Any) -> None:
    with pytest.raises(ValueError, match="non-negative number"):
        max_db_time(bad)


def test_max_db_time_yields_the_collector_and_does_not_hide_an_error(
    engine: Engine,
) -> None:
    with pytest.raises(RuntimeError, match="boom"), max_db_time(0):
        _run(engine, "select 1")
        raise RuntimeError("boom")


def test_max_queries_failure_shows_the_database_time(engine: Engine) -> None:
    with pytest.raises(QueryBudgetExceeded) as caught, max_queries(1):
        _run(engine, "select 1")
        _run(engine, f"select nap({SLEEP})")
    message = str(caught.value)
    assert "expected at most 1 query, got 2" in message
    assert "  database time: " in message
    assert "  slowest:" in message
    assert "select nap" in message.split("slowest:")[1].splitlines()[1]


# ------------------------------------------------------------ the marker

BODY = """
import time
import pytest
from sqlalchemy import create_engine, event, text


def make_engine():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def register(dbapi_connection, record):
        def nap(seconds):
            time.sleep(seconds)
            return 1

        dbapi_connection.create_function("nap", 1, nap)

    return engine


{marker}
def test_timed():
    engine = make_engine()
    with engine.connect() as conn:
        conn.execute(text("select 1")).all()
        conn.execute(text("select nap(0.05)")).all()
"""


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS)
    return pytester


def _marked(marker: str) -> str:
    return BODY.format(marker=marker)


def test_marker_fails_over_the_limit(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=_marked("@pytest.mark.max_db_time(0.01)"))
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*database time * exceeded the budget of 10.0 ms (2 statements)*",
            "*slowest:*",
            "*select nap(0.05)*test_a.py*",
        ]
    )


def test_marker_passes_under_the_limit(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=_marked("@pytest.mark.max_db_time(30)"))
    project.runpytest().assert_outcomes(passed=1)


def test_marker_takes_seconds_as_a_keyword(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=_marked("@pytest.mark.max_db_time(seconds=0.01)"))
    project.runpytest().assert_outcomes(failed=1)


def test_marker_fails_even_in_off_mode(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=_marked("@pytest.mark.max_db_time(0.01)"))
    result = project.runpytest("--queryguard-mode=off")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*exceeded the budget of 10.0 ms*"])


@pytest.mark.parametrize(
    "marker",
    [
        "@pytest.mark.max_db_time('x')",
        "@pytest.mark.max_db_time(-1)",
        "@pytest.mark.max_db_time(True)",
        "@pytest.mark.max_db_time(float('nan'))",
    ],
)
def test_marker_rejects_an_invalid_limit(project: pytest.Pytester, marker: str) -> None:
    project.makepyfile(test_a=_marked(marker))
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*max_db_time*test_timed*non-negative number*"])


def test_marker_rejects_extra_arguments(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=_marked("@pytest.mark.max_db_time(1, 2)"))
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*max_db_time*takes a single argument*"])


def test_marker_rejects_the_limit_given_twice(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=_marked("@pytest.mark.max_db_time(1, seconds=1)"))
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*max_db_time*twice*"])


def test_both_budgets_are_reported_in_one_failure(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a=_marked("@pytest.mark.max_queries(1)\n@pytest.mark.max_db_time(0.01)")
    )
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        ["*expected at most 1 query, got 2*", "*database time * exceeded*"]
    )


def test_the_time_counts_statements_beyond_the_event_cap(
    project: pytest.Pytester,
) -> None:
    project.makeini("[pytest]\nqueryguard_max_events = 1\n")
    project.makepyfile(test_a=_marked("@pytest.mark.max_db_time(0.01)"))
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    # the slow statement is the second one, which is not stored
    result.stdout.fnmatch_lines(["*exceeded the budget of 10.0 ms (2 statements)*"])
    result.stdout.fnmatch_lines(["*1 more statement was not stored*"])


def test_marker_is_registered(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=_marked("@pytest.mark.max_db_time(30)"))
    project.runpytest("--strict-markers").assert_outcomes(passed=1)


# ------------------------------------------------------------ robustness


def test_a_failing_timing_listener_is_visible_and_harmless(engine: Engine) -> None:
    from pytest_queryguard import listeners

    class Broken:
        def __setattr__(self, name: str, value: object) -> None:
            raise RuntimeError("no attributes")

    listeners._reset_for_tests()
    try:
        listeners._on_timing_after(None, None, "select 1", (), _Context(), False)
        assert listeners.internal_error() is None  # the context was fine
        listeners._on_timing_after(None, None, "select 1", (), Broken(), False)
        assert listeners.internal_error() is None  # nothing was recorded for it
        context = _Context()
        context._queryguard_timing = (None, (), 0.0)  # a bug: not a StatementEvent
        listeners._on_timing_after(None, None, "select 1", (), context, False)
        failure = listeners.internal_failure()
        assert failure is not None
        assert "statement timing" in failure.source
    finally:
        listeners._reset_for_tests()


class _Context:
    _queryguard_timing: object = None
