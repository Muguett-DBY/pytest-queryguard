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

from .conftest import CUSTOMERS, Customer, Order


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
            with queryguard.track(report=[]):
                assert len(session.scalars(select(Customer)).all()) == CUSTOMERS
        assert calls == [True]
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
    assert all(
        q.relationship is None for q in report.statements if q.sql == "SELECT 99"
    )
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
    if kind == "selectin":
        assert report.findings == ()
    else:
        (finding,) = report.findings
        assert finding.kind == (
            "relationship-load" if kind == "lazy" else "column-load"
        )
        assert finding.count == CUSTOMERS


@pytest.mark.parametrize("watch_enabled", [False, True])
def test_yield_per_selectinload_keeps_working(
    engine: Engine, watch_enabled: bool
) -> None:
    if watch_enabled:
        queryguard.watch(engine)
    with Session(engine) as session, queryguard.track(report=[]):
        statement = (
            select(Customer)
            .options(selectinload(Customer.orders))
            .execution_options(yield_per=2)
        )
        assert sum(len(customer.orders) for customer in session.scalars(statement)) == (
            CUSTOMERS * 2
        )


@pytest.mark.parametrize("watch_enabled", [False, True])
def test_outer_autoflush_false_does_not_disable_the_loader_flush(
    engine: Engine, watch_enabled: bool
) -> None:
    if watch_enabled:
        queryguard.watch(engine)
    with Session(engine) as session, queryguard.track(report=[]):
        session.add(Order(id=99999, customer_id=1))
        statement = (
            select(Customer)
            .where(Customer.id == 1)
            .options(selectinload(Customer.orders))
            .execution_options(autoflush=False)
        )
        (customer,) = session.scalars(statement).all()
        assert sorted(order.id for order in customer.orders) == [10, 11, 99999]


@pytest.mark.parametrize("watch_enabled", [False, True])
def test_async_yield_per_selectinload_keeps_working(
    async_engine: AsyncEngine, watch_enabled: bool
) -> None:
    if watch_enabled:
        queryguard.watch(async_engine)

    async def run() -> int:
        async with AsyncSession(async_engine) as session:
            with queryguard.track(report=[]):
                statement = (
                    select(Customer)
                    .options(selectinload(Customer.orders))
                    .execution_options(yield_per=2)
                )
                result = await session.stream_scalars(statement)
                return sum([len(customer.orders) async for customer in result])

    assert asyncio.run(run()) == CUSTOMERS * 2
