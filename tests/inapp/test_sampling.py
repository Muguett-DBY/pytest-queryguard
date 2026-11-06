"""sample_rate and min_db_time: measure some units, skip fast N+1 (#279)."""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from flask import Flask
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pytest_queryguard import app as queryguard
from pytest_queryguard.app import (
    QueryGuardASGI,
    QueryGuardWSGI,
    RequestReport,
    Settings,
    _core,
    _listeners,
)

from .conftest import CUSTOMERS, Order


def _lazy_loop(engine: Engine) -> list[str]:
    with Session(engine) as session:
        return [o.customer.name for o in session.scalars(select(Order))]


def _random(monkeypatch: pytest.MonkeyPatch, *values: float) -> None:
    draws = iter(values)
    monkeypatch.setattr(_core.random, "random", lambda: next(draws))


def test_sample_rate_is_validated() -> None:
    for bad in (-0.1, 1.5, float("nan"), True, "0.5", None):
        with pytest.raises(ValueError, match="sample_rate"):
            Settings(sample_rate=bad)  # type: ignore[arg-type]
    Settings(sample_rate=0)
    Settings(sample_rate=0.25)
    Settings(sample_rate=1)


def test_min_db_time_is_validated() -> None:
    for bad in (-1, float("nan"), float("inf"), False, "0.1", None):
        with pytest.raises(ValueError, match="min_db_time"):
            Settings(min_db_time=bad)  # type: ignore[arg-type]
    Settings(min_db_time=0.005)


def test_an_unsampled_unit_measures_and_reports_nothing(watched: Engine) -> None:
    seen: list[RequestReport] = []
    with queryguard.track("job", sample_rate=0, report=seen.append) as report:
        _lazy_loop(watched)
    assert not report.sampled
    assert report.statement_count == 0
    assert report.findings == ()
    assert seen == []


def test_an_unsampled_unit_counts_for_the_enclosing_one(watched: Engine) -> None:
    with queryguard.track("outer", report=[]) as outer, watched.connect() as conn:
        conn.execute(text("SELECT 1"))
        with queryguard.track("inner", sample_rate=0, report=[]) as inner:
            conn.execute(text("SELECT 2"))
        conn.execute(text("SELECT 3"))
    assert not inner.sampled
    assert inner.statement_count == 0
    assert outer.statement_count == 3


def test_sample_rate_picks_units_at_random(
    watched: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _random(monkeypatch, 0.1, 0.6, 0.29)
    seen: list[RequestReport] = []

    @queryguard.track(sample_rate=0.3, report=seen.append)
    def job() -> None:
        _lazy_loop(watched)

    for _ in range(3):
        job()
    assert len(seen) == 2
    assert all(r.sampled and r.statement_count == 1 + CUSTOMERS for r in seen)


def test_start_and_stop_of_an_unsampled_unit(watched: Engine) -> None:
    unit = queryguard.track("task", sample_rate=0, report="warn")
    report = unit.start()
    _lazy_loop(watched)
    assert unit.stop() is report
    assert not report.sampled
    assert report.duration is None


def test_wsgi_requests_are_sampled(
    watched: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _random(monkeypatch, 0.9, 0.2)
    app = Flask(__name__)
    reports: list[RequestReport] = []
    app.wsgi_app = QueryGuardWSGI(  # type: ignore[method-assign]
        app.wsgi_app, sample_rate=0.5, panel=True, report=reports.append
    )

    @app.get("/orders")
    def orders() -> list[str]:
        return _lazy_loop(watched)

    client = app.test_client()
    for _ in range(2):
        assert client.get("/orders").get_json()[0] == "customer 1"
    assert len(reports) == 1
    middleware = app.wsgi_app
    assert isinstance(middleware, QueryGuardWSGI)
    # The panel holds only the measured request.
    assert middleware.reports == tuple(reports)


def test_asgi_requests_are_sampled(watched: Engine) -> None:
    api = FastAPI()

    @api.get("/orders")
    def orders() -> list[str]:
        return _lazy_loop(watched)

    reports: list[RequestReport] = []
    app = QueryGuardASGI(api, sample_rate=0, panel=True, report=reports.append)
    with TestClient(app) as client:
        assert client.get("/orders").status_code == 200
    assert reports == []
    assert app.reports == ()


def test_min_db_time_skips_the_rules_but_not_the_budget(watched: Engine) -> None:
    seen: list[RequestReport] = []
    with queryguard.track(
        min_db_time=3600, default_budget=2, report=seen.append
    ) as report:
        _lazy_loop(watched)
    assert report.db_time > 0
    assert report.findings == ()
    assert report.over_budget
    assert seen == [report]
    assert "N+1" not in report.message

    with queryguard.track(min_db_time=3600, report=seen.append) as quiet:
        _lazy_loop(watched)
    assert quiet.findings == ()
    assert seen == [report]


def test_min_db_time_compares_the_database_time_of_the_whole_unit(
    watched: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each statement takes one second on a fake clock: 6 statements, of which
    # the 5 lazy loads of the finding took 5 seconds.
    clock = itertools.count()
    monkeypatch.setattr(
        _listeners, "time", SimpleNamespace(perf_counter=lambda: next(clock))
    )
    with queryguard.track(min_db_time=6, report=[]) as report:
        _lazy_loop(watched)
    assert report.db_time == 1 + CUSTOMERS
    assert [f.label for f in report.findings] == ["Order.customer"]
    with queryguard.track(min_db_time=7, report=[]) as fast:
        _lazy_loop(watched)
    assert fast.db_time == 1 + CUSTOMERS
    assert fast.findings == ()
