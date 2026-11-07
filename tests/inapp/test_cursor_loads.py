"""The in-app mode attributes loads to the SELECT that actually runs them."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import ORMExecuteState, Session, selectinload

from pytest_queryguard import app as queryguard
from pytest_queryguard import record_queries
from pytest_queryguard.app import _core

from .conftest import CUSTOMERS, Customer


def test_watch_preserves_other_orm_execute_listeners(engine: Engine) -> None:
    calls: list[bool] = []

    def user_listener(state: ORMExecuteState) -> None:
        calls.append(state.is_select)

    event.listen(Session, "do_orm_execute", user_listener)
    try:
        with Session(engine) as session:
            before = tuple(session.dispatch.do_orm_execute)
            queryguard.watch(engine)
            assert tuple(session.dispatch.do_orm_execute) == before
            with queryguard.track(report=[]) as report:
                assert len(session.scalars(select(Customer)).all()) == CUSTOMERS
        assert calls == [True]
        assert [q.sql for q in report.statements if "FROM customers" in q.sql]
    finally:
        event.remove(Session, "do_orm_execute", user_listener)


def test_a_select_in_after_begin_does_not_take_the_load(watched: Engine) -> None:
    def probe(session: Session, transaction: Any, connection: Any) -> None:
        connection.exec_driver_sql("SELECT 99")

    event.listen(Session, "after_begin", probe)
    try:
        with (
            queryguard.track(report=[]) as report,
            record_queries() as log,
            Session(watched, expire_on_commit=False) as session,
        ):
            customers = session.scalars(select(Customer)).all()
            session.commit()
            for customer in customers:
                assert len(customer.orders) == 2
                session.commit()
    finally:
        event.remove(Session, "after_begin", probe)

    loads = [q for q in report.statements if "FROM orders" in q.sql]
    assert len(loads) == CUSTOMERS
    assert {q.relationship for q in loads} == {"Customer.orders"}
    # One probe per transaction: the first query's and one after each commit.
    probes = [q for q in report.statements if q.sql == "SELECT 99"]
    assert len(probes) == CUSTOMERS + 1
    assert all(q.relationship is None for q in probes)
    assert [(q.sql, q.relationship) for q in report.statements] == [
        (q.sql, q.relationship) for q in log
    ]


@pytest.mark.parametrize("kind", ["lazy", "selectin", "deferred", "refresh"])
def test_app_and_plugin_label_the_same_loads(watched: Engine, kind: str) -> None:
    with (
        queryguard.track(report=[]) as report,
        record_queries() as log,
        Session(watched) as session,
    ):
        statement = select(Customer).order_by(Customer.id)
        if kind == "selectin":
            statement = statement.options(selectinload(Customer.orders))
        customers = session.scalars(statement).all()
        for customer in customers:
            if kind in {"lazy", "selectin"}:
                assert len(customer.orders) == 2
            elif kind == "deferred":
                assert customer.bio.startswith("bio ")
            else:
                session.refresh(customer)
    assert [(q.sql, q.relationship) for q in report.statements] == [
        (q.sql, q.relationship) for q in log
    ]
    if kind == "lazy":
        loads = [q for q in report.statements if "FROM orders" in q.sql]
        assert len(loads) == CUSTOMERS
        assert {q.relationship for q in loads} == {"Customer.orders"}
    if kind == "selectin":
        assert report.findings == ()
    else:
        (finding,) = report.findings
        assert finding.kind == (
            "relationship-load" if kind == "lazy" else "column-load"
        )
        assert finding.count == CUSTOMERS


@pytest.mark.parametrize("watch_enabled", [False, True])
def test_async_yield_per_selectinload_keeps_working(
    async_engine: AsyncEngine, watch_enabled: bool
) -> None:
    """The async form of tests/test_yield_per_leak.py's in-app cases.

    SQLAlchemy 2.0.20 to 2.0.49 break this query when a ``do_orm_execute``
    listener is registered (issue 13301); on later releases it passes either
    way, so only the CI jobs on those releases can catch such a listener.
    """
    if watch_enabled:
        queryguard.watch(async_engine)

    async def run() -> tuple[int, Any]:
        async with AsyncSession(async_engine) as session:
            with queryguard.track(report=[]) as report:
                statement = (
                    select(Customer)
                    .options(selectinload(Customer.orders))
                    .execution_options(yield_per=2)
                )
                result = await session.stream_scalars(statement)
                total = sum([len(customer.orders) async for customer in result])
        return total, report

    total, report = asyncio.run(run())
    assert total == CUSTOMERS * 2
    assert _core.internal_error() is None
    measured = [q for q in report.statements if "FROM orders" in q.sql]
    assert bool(measured) is watch_enabled
