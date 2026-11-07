"""watch(), track(), Settings and reporting of the in-app mode."""

from __future__ import annotations

import dataclasses
import gc
import logging
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.horizontal_shard import ShardedSession
from sqlalchemy.orm import Session, joinedload

from pytest_queryguard import (
    QueryGuardDeprecationWarning,
    QueryGuardWarning,
    record_queries,
)
from pytest_queryguard import app as queryguard
from pytest_queryguard import listeners as pytest_listeners
from pytest_queryguard.app import RequestReport, Settings, _listeners

from .conftest import CUSTOMERS, Customer, Order, seed


def _lazy_loop(engine: Engine) -> list[str]:
    with Session(engine) as session:
        return [o.customer.name for o in session.scalars(select(Order))]


def test_importing_registers_nothing_and_needs_no_pytest() -> None:
    code = (
        "import sys\n"
        "from sqlalchemy import event\n"
        "from sqlalchemy.engine import Engine\n"
        "from sqlalchemy.orm import Session\n"
        "import pytest_queryguard.app as qg\n"
        "from pytest_queryguard.app import _listeners as L\n"
        "assert 'pytest' not in sys.modules, 'pytest was imported'\n"
        "assert not event.contains(Session, 'after_flush', L._on_after_flush)\n"
        "assert not L._watched\n"
        "qg.QueryGuardASGI(lambda *a: None)\n"
        "qg.QueryGuardWSGI(lambda *a: [])\n"
        "assert not event.contains(Session, 'after_flush', L._on_after_flush)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.stdout.strip() == "ok", result.stderr


def test_watch_and_unwatch_are_idempotent(engine: Engine) -> None:
    queryguard.watch(engine)
    queryguard.watch(engine)
    assert queryguard.is_watched(engine)
    assert event.contains(
        engine, "before_cursor_execute", _listeners._on_before_cursor_execute
    )
    assert event.contains(Session, "after_flush", _listeners._on_after_flush)
    queryguard.unwatch(engine)
    queryguard.unwatch(engine)
    assert not queryguard.is_watched(engine)
    assert not event.contains(
        engine, "before_cursor_execute", _listeners._on_before_cursor_execute
    )
    # The Session listeners go with the last watched engine.
    assert not event.contains(Session, "after_flush", _listeners._on_after_flush)


def test_watch_adds_no_do_orm_execute_listener(watched: Engine) -> None:
    """The cursor listener reads the ORM's options, as the plugin's does.

    A ``do_orm_execute`` listener would make SQLAlchemy prepare each ORM
    statement twice, and leak load options on some releases (see
    tests/test_yield_per_leak.py).
    """
    with Session(watched) as session:
        found = [
            fn
            for fn in session.dispatch.do_orm_execute
            if getattr(fn, "__module__", "").startswith("pytest_queryguard.app")
        ]
    assert found == []


def test_the_session_listeners_after_the_last_watched_engine_is_collected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = create_engine(f"sqlite:///{tmp_path / 'gone.sqlite'}")
    queryguard.watch(gone)
    gone.dispose()
    del gone
    gc.collect()
    assert not _listeners._watched
    # Still registered, but they do nothing until a watch() or unwatch() call.
    assert event.contains(Session, "after_flush", _listeners._on_after_flush)
    other = create_engine("sqlite://")
    seed(other)
    with queryguard.track(report=[]), Session(other) as session:
        _lazy_loop(other)
        session.add(Customer(name="new"))
        session.flush()  # the after_flush and after_begin listeners run
        assert _listeners._WRITTEN_ATTR not in session.__dict__
    queryguard.unwatch(other)
    assert not event.contains(Session, "after_begin", _listeners._on_after_begin)
    assert not event.contains(Session, "after_flush", _listeners._on_after_flush)
    other.dispose()


def test_watch_takes_the_sync_engine_of_an_async_engine(
    async_engine: AsyncEngine,
) -> None:
    queryguard.watch(async_engine)
    assert queryguard.is_watched(async_engine.sync_engine)


def test_watch_rejects_other_objects() -> None:
    with pytest.raises(TypeError, match="Engine or AsyncEngine"):
        queryguard.watch(object())


def test_track_reports_an_n_plus_one(watched: Engine) -> None:
    seen: list[RequestReport] = []
    with queryguard.track("list orders", report=seen.append) as report:
        _lazy_loop(watched)
    assert seen == [report]
    assert report.statement_count == 1 + CUSTOMERS
    assert [f.kind for f in report.findings] == ["relationship-load"]
    assert report.findings[0].label == "Order.customer"
    assert report.findings[0].file == "tests/inapp/test_core.py"
    assert report.statements[1].relationship == "Order.customer"
    assert report.duration is not None and report.duration > 0
    assert "Order.customer was lazy-loaded 5 times" in report.message


def test_the_fixed_code_has_no_findings_and_is_not_reported(watched: Engine) -> None:
    seen: list[RequestReport] = []
    query = select(Order).options(joinedload(Order.customer))
    with queryguard.track(report=seen.append) as report, Session(watched) as session:
        [o.customer.name for o in session.scalars(query)]
    assert report.statement_count == 1
    assert report.findings == ()
    assert not report.problems
    assert report.message == ""
    assert seen == []


def test_only_watched_engines_count(watched: Engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    other = create_engine(f"sqlite:///{tmp_path / 'other.sqlite'}")
    seed(other)
    with queryguard.track(report=[]) as report:
        _lazy_loop(other)
        with watched.connect() as conn:
            conn.execute(text("SELECT 1"))
    assert report.statement_count == 1
    # The other engine's lazy loads are not kept either.
    assert report.findings == ()
    other.dispose()


def test_nothing_is_recorded_outside_a_unit(watched: Engine) -> None:
    _lazy_loop(watched)
    with queryguard.track(report=[]) as report:
        pass
    assert report.statement_count == 0


def test_column_loads_and_repeated_statements(watched: Engine) -> None:
    with queryguard.track(report=[]) as report:
        with Session(watched) as session:
            customers = session.scalars(select(Customer)).all()
            [c.bio for c in customers]  # deferred: one SELECT each
        with watched.connect() as conn:
            for _ in range(5):
                conn.execute(text("SELECT count(*) FROM orders")).scalar()
    kinds = sorted(f.kind for f in report.findings)
    assert kinds == ["column-load", "repeated-statement"]


def test_budgets_follow_settings(watched: Engine) -> None:
    with queryguard.track("job", default_budget=2, report=[]) as report:
        _lazy_loop(watched)
    assert report.budget == 2
    assert report.over_budget
    assert "expected at most 2 queries, got 6" in report.message


def test_max_events_caps_what_is_stored(watched: Engine) -> None:
    with queryguard.track(max_events=2, report=[]) as report:
        _lazy_loop(watched)
    assert report.statement_count == 6
    assert len(report.statements) == 2
    assert report.truncated


def test_scope_session_counts_each_session_alone(watched: Engine) -> None:
    def work() -> None:
        for _ in range(2):
            with Session(watched) as session:
                for _ in range(3):
                    session.execute(text("SELECT count(*) FROM customers")).scalar()

    with queryguard.track(report=[]) as per_request:
        work()
    with queryguard.track(scope="session", report=[]) as per_session:
        work()
    assert [f.kind for f in per_request.findings] == ["repeated-statement"]
    assert per_session.findings == ()
    # the deprecated name still works, and warns at the caller's line
    with pytest.warns(QueryGuardDeprecationWarning, match="use scope= instead") as rec:
        old = queryguard.track(repeat_scope="session", report=[])
    assert rec[0].filename == __file__
    with old as per_session:
        work()
    assert per_session.findings == ()


def test_settings_repeat_scope_is_a_deprecated_alias() -> None:
    with pytest.warns(QueryGuardDeprecationWarning) as record:
        settings = Settings(repeat_scope="session")
    assert str(record[0].message) == (
        "repeat_scope= is deprecated and will be removed in pytest-queryguard "
        "2.0; use scope= instead"
    )
    assert record[0].filename == __file__
    assert (settings.scope, settings.repeat_scope) == ("session", None)
    assert Settings().scope == "request"
    assert type(Settings().scope) is str
    # a copy does not warn again or see a conflict
    assert dataclasses.replace(settings, scope="request").scope == "request"
    with pytest.warns(QueryGuardDeprecationWarning):
        assert Settings(scope="session", repeat_scope="session").scope == "session"
    with (
        pytest.warns(QueryGuardDeprecationWarning),
        pytest.raises(ValueError, match="disagree"),
    ):
        Settings(scope="request", repeat_scope="session")


def test_the_report_targets(watched: Engine, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="pytest_queryguard.app")
    with queryguard.track("logged"):
        _lazy_loop(watched)
    assert "queryguard: logged: 6 statements" in caplog.text
    with (
        pytest.warns(QueryGuardWarning, match="Order.customer"),
        queryguard.track(report="warn"),
    ):
        _lazy_loop(watched)


def test_a_failing_callback_is_logged_not_raised(
    watched: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(report: RequestReport) -> None:
        raise RuntimeError("callback bug")

    with queryguard.track(report=[broken, "log"]):
        _lazy_loop(watched)
    assert "the report callback" in caplog.text
    assert "callback bug" in caplog.text
    assert "Order.customer was lazy-loaded" in caplog.text


def test_settings_are_validated() -> None:
    with pytest.raises(ValueError, match="repeat_threshold"):
        Settings(repeat_threshold=1)
    with pytest.raises(ValueError, match="small_repeat_threshold"):
        Settings(small_repeat_threshold=5)
    with pytest.raises(ValueError, match="scope must be one of"):
        Settings(scope="test")
    with pytest.raises(ValueError, match="budget"):
        Settings(budgets={"/x": -1})
    with pytest.raises(ValueError, match="report"):
        Settings(report="print")
    with (
        pytest.raises(TypeError, match="either settings"),
        queryguard.track(settings=Settings(), report="log"),
    ):
        pass


@pytest.mark.parametrize(
    ("method", "route", "path", "budget"),
    [
        ("GET", "/orders", "/orders", 3),
        ("POST", "/orders", "/orders", 10),
        ("GET", "/orders/{order_id}", "/orders/7", 4),
        ("DELETE", "/orders/{order_id}", "/orders/7", 4),
        ("GET", None, "/admin/users", 50),
        ("GET", None, "/health", 1),
        ("GET", None, "/other", None),
    ],
)
def test_budget_keys(
    method: str, route: str | None, path: str, budget: int | None
) -> None:
    settings = Settings(
        budgets={
            "GET /orders": 3,
            "/orders": 10,
            "/orders/{order_id}": 4,
            "/admin/*": 50,
            "get /health": 1,
        }
    )
    assert settings.budget_for(method, route, path) == budget


@pytest.mark.parametrize(
    ("method", "path", "budget"),
    [
        ("GET", "/files/my docs/a", 2),
        ("POST", "/files/my docs/a", None),
        ("GET", "/search results", 5),
        ("GET", "/tab", 7),
        ("GET", "/a  b", 8),
        ("M-SEARCH", "/upnp", 9),
        ("GET", "/upnp", None),
    ],
)
def test_budget_keys_with_whitespace(
    method: str, path: str, budget: int | None
) -> None:
    settings = Settings(
        budgets={
            "GET /files/my docs/*": 2,
            "/search results": 5,
            "GET\t/tab": 7,
            "GET   /a  b": 8,
            "M-SEARCH /upnp": 9,
        }
    )
    assert settings.budget_for(method, None, path) == budget


def test_settings_copy_the_budgets() -> None:
    budgets = {"/a": 1}
    settings = Settings(budgets=budgets)
    budgets["/b"] = 2
    assert settings.budget_for("GET", None, "/b") is None


def test_set_route_outside_a_request_does_nothing() -> None:
    queryguard.set_route("/x")


def test_the_pytest_plugin_still_records_alongside(watched: Engine) -> None:
    with record_queries() as log, queryguard.track(report=[]) as report:
        _lazy_loop(watched)
    assert log.count == report.statement_count == 6


def test_an_internal_error_switches_the_mode_off_but_not_the_app(
    watched: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(sites: object = None) -> None:
        raise RuntimeError("callsite bug")

    monkeypatch.setattr(_listeners, "find_callsite", broken)
    seen: list[RequestReport] = []
    with queryguard.track(default_budget=0, report=seen.append) as report:
        names = _lazy_loop(watched)
    assert len(names) == 2 * CUSTOMERS  # the application got its rows
    assert isinstance(queryguard.internal_error(), RuntimeError)
    assert report.error == "RuntimeError: callsite bug"
    assert report.findings == ()
    # Incomplete counts: no budget verdict either, so nothing is reported.
    assert report.budget is None
    assert seen == []
    assert caplog.text.count("in-app mode switched off") == 1
    # The pytest plugin's own error state is untouched.
    assert pytest_listeners.internal_error() is None
    monkeypatch.undo()
    with queryguard.track(report=[]) as later:
        _lazy_loop(watched)
    assert later.statement_count == 0  # off for the rest of the process


def test_an_error_at_the_end_of_a_unit_is_contained(
    watched: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("detector bug")

    monkeypatch.setattr("pytest_queryguard.app._core.detect", broken)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with queryguard.track(report=[]) as report:
            _lazy_loop(watched)
    assert report.error == "RuntimeError: detector bug"
    assert isinstance(queryguard.internal_error(), RuntimeError)


def test_an_engine_with_execution_options_counts_as_watched(watched: Engine) -> None:
    # execution_options() returns an OptionEngine that shares the listeners
    # (schema_translate_map, isolation levels); its loads must still count.
    option = watched.execution_options(isolation_level="SERIALIZABLE")
    nested = option.execution_options(schema_translate_map={None: None})
    assert queryguard.is_watched(option) and queryguard.is_watched(nested)
    queryguard.watch(option)  # already watched through its parent: no-op
    with queryguard.track(report=[]) as report:
        _lazy_loop(nested)
    assert report.statement_count == 1 + CUSTOMERS
    assert "relationship-load" in {f.kind for f in report.findings}
    assert "Order.customer" in {q.relationship for q in report.statements}


def test_watching_the_parent_after_an_option_engine_counts_once(engine: Engine) -> None:
    option = engine.execution_options(isolation_level="SERIALIZABLE")
    queryguard.watch(option)
    queryguard.watch(engine)
    with queryguard.track(report=[]) as report:
        _lazy_loop(option)
    assert report.statement_count == 1 + CUSTOMERS
    queryguard.unwatch(option)  # watched through its parent now
    assert queryguard.is_watched(option)
    queryguard.unwatch(engine)
    assert not queryguard.is_watched(option)


def test_a_custom_get_bind_is_not_called_again(watched: Engine) -> None:
    chosen: list[object] = []

    def shard_chooser(mapper: object, instance: object, clause: object = None) -> str:
        chosen.append(instance)  # application code: it may count or rotate
        return "a"

    with (
        queryguard.track(report=[]) as report,
        ShardedSession(
            shards={"a": watched},
            shard_chooser=shard_chooser,
            identity_chooser=lambda *args, **kwargs: ["a"],
            execute_chooser=lambda context: ["a"],
        ) as session,
    ):
        names = [o.customer.name for o in session.scalars(select(Order))]
    assert len(names) == CUSTOMERS * 2
    assert chosen == []
    assert "relationship-load" in {f.kind for f in report.findings}


class RoutingSession(Session):
    """A get_bind override, as in Flask-SQLAlchemy or a replica router."""

    def get_bind(self, *args: Any, **kwargs: Any) -> Any:
        return super().get_bind(*args, **kwargs)


@pytest.mark.parametrize("session_class", [Session, RoutingSession])
def test_a_load_on_an_unwatched_engine_is_not_credited_to_a_watched_one(
    watched: Engine, tmp_path: Path, session_class: type[Session]
) -> None:
    other = create_engine(f"sqlite:///{tmp_path / 'other.sqlite'}")
    seed(other)
    try:
        with (
            queryguard.track(report=[]) as report,
            session_class(other) as session,
            watched.connect() as conn,
        ):
            for order in session.scalars(select(Order)):
                _ = order.customer  # a lazy load, run on the unwatched engine
                conn.execute(text("SELECT 1")).scalar()
    finally:
        other.dispose()
    # Only the watched engine's SELECT 1 per order (two orders per customer).
    assert report.statement_count == CUSTOMERS * 2
    assert {q.relationship for q in report.statements} == {None}
    assert "relationship-load" not in {f.kind for f in report.findings}
