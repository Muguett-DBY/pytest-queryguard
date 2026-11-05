"""track() for units of work outside HTTP: decorators, async, start/stop (#240)."""

from __future__ import annotations

import asyncio
import contextvars
import warnings

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session

from pytest_queryguard import QueryGuardWarning
from pytest_queryguard import app as queryguard
from pytest_queryguard.app import RequestReport

from .conftest import CUSTOMERS, Order


def _lazy_loop(engine: Engine) -> list[str]:
    with Session(engine) as session:
        return [o.customer.name for o in session.scalars(select(Order))]


def test_a_decorated_function_is_one_unit_per_call(watched: Engine) -> None:
    seen: list[RequestReport] = []

    @queryguard.track(report=seen.append)
    def export() -> list[str]:
        return _lazy_loop(watched)

    assert export.__name__ == "export"
    assert len(export()) == CUSTOMERS * 2
    export()
    assert [r.name for r in seen] == [export.__qualname__] * 2
    assert all(r.statement_count == 1 + CUSTOMERS for r in seen)
    assert seen[0].id != seen[1].id


def test_a_decorated_function_can_be_named(watched: Engine) -> None:
    seen: list[RequestReport] = []

    @queryguard.track("nightly export", report=seen.append)
    def export() -> None:
        _lazy_loop(watched)

    export()
    assert seen[0].name == "nightly export"


def test_a_decorated_coroutine_function_is_measured_across_awaits(
    async_engine: AsyncEngine,
) -> None:
    queryguard.watch(async_engine)
    seen: list[RequestReport] = []

    @queryguard.track(report=seen.append)
    async def handle(message: str) -> int:
        async with AsyncSession(async_engine) as session:
            orders = (await session.scalars(select(Order))).all()
            for order in orders:
                await asyncio.sleep(0)  # other tasks run in between
                await order.awaitable_attrs.customer
        return len(orders)

    async def main() -> list[int]:
        return list(await asyncio.gather(handle("a"), handle("b")))

    assert asyncio.run(main()) == [CUSTOMERS * 2] * 2
    # two concurrent units: each counts its own statements only
    assert [r.statement_count for r in seen] == [1 + CUSTOMERS] * 2
    assert all(f.label == "Order.customer" for r in seen for f in r.findings)


def test_async_with(async_engine: AsyncEngine) -> None:
    queryguard.watch(async_engine)
    seen: list[RequestReport] = []

    async def main() -> RequestReport:
        async with (
            queryguard.track("sync accounts", report=seen.append) as report,
            AsyncSession(async_engine) as session,
        ):
            await session.scalars(select(Order))
        return report

    report = asyncio.run(main())
    assert report.statement_count == 1
    assert seen == []  # nothing to report


def test_start_and_stop_from_signals_like_celery(watched: Engine) -> None:
    # task_prerun / task_postrun call the two handlers in the task's context
    seen: list[RequestReport] = []
    units: dict[str, queryguard.track] = {}

    def task_prerun(task_id: str, name: str) -> None:
        unit = queryguard.track(name, report=seen.append)
        unit.start()
        units[task_id] = unit

    def task_postrun(task_id: str) -> None:
        units.pop(task_id).stop()

    task_prerun("t1", "tasks.export")
    _lazy_loop(watched)
    task_postrun("t1")
    assert [(r.name, r.statement_count) for r in seen] == [
        ("tasks.export", 1 + CUSTOMERS)
    ]
    # outside the unit nothing is measured any more
    _lazy_loop(watched)
    assert len(seen) == 1


def test_start_and_stop_in_another_context_still_end_the_unit(
    watched: Engine,
) -> None:
    # a worker that runs each handler in a copied context
    seen: list[RequestReport] = []
    unit = queryguard.track("job", report=seen.append)
    contextvars.copy_context().run(unit.start)
    report = contextvars.copy_context().run(unit.stop)
    assert report.duration is not None
    assert seen == []


def test_misuse_is_an_error() -> None:
    unit = queryguard.track()
    with pytest.raises(RuntimeError, match="has not started"):
        unit.stop()
    unit.start()
    with pytest.raises(RuntimeError, match="already started"):
        unit.start()
    with pytest.raises(RuntimeError, match="cannot decorate"):
        unit(lambda: None)
    unit.stop()
    with pytest.raises(TypeError, match="either settings= or its fields"):
        queryguard.track(settings=queryguard.Settings(), report=[])


def test_a_warning_turned_into_an_error_is_raised_when_the_unit_ends(
    watched: Engine,
) -> None:
    @queryguard.track(report="warn")
    def export() -> None:
        _lazy_loop(watched)

    with warnings.catch_warnings():
        warnings.simplefilter("error", QueryGuardWarning)
        with pytest.raises(QueryGuardWarning, match="Order.customer"):
            export()


def test_the_unit_s_own_exception_wins(watched: Engine) -> None:
    @queryguard.track(report="warn")
    def export() -> None:
        _lazy_loop(watched)
        raise KeyError("job failed")

    with warnings.catch_warnings():
        warnings.simplefilter("error", QueryGuardWarning)
        with pytest.raises(KeyError):
            export()


def test_one_shared_unit_object_in_many_threads(watched: Engine) -> None:
    import threading

    seen: list[RequestReport] = []
    unit = queryguard.track("handler", report=seen.append)
    errors: list[BaseException] = []

    def handler() -> None:
        try:
            with unit:
                _lazy_loop(watched)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=handler) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(seen) == 8
    assert all(r.statement_count == 1 + CUSTOMERS for r in seen)


def test_a_generator_function_is_measured_over_its_iteration(watched: Engine) -> None:
    seen: list[RequestReport] = []

    @queryguard.track(report=seen.append)
    def names():  # type: ignore[no-untyped-def]
        with Session(watched) as session:
            for order in session.scalars(select(Order)):
                yield order.customer.name

    assert len(list(names())) == CUSTOMERS * 2
    (report,) = seen
    assert report.statement_count == 1 + CUSTOMERS


def test_an_async_generator_function_is_measured_over_its_iteration(
    async_engine: AsyncEngine,
) -> None:
    queryguard.watch(async_engine)
    seen: list[RequestReport] = []

    @queryguard.track(report=seen.append)
    async def names():  # type: ignore[no-untyped-def]
        async with AsyncSession(async_engine) as session:
            for order in (await session.scalars(select(Order))).all():
                yield (await order.awaitable_attrs.customer).name

    async def main() -> list[str]:
        return [name async for name in names()]

    assert len(asyncio.run(main())) == CUSTOMERS * 2
    (report,) = seen
    assert report.statement_count == 1 + CUSTOMERS


def test_an_object_with_an_async_call_is_awaited_inside_the_unit(
    async_engine: AsyncEngine,
) -> None:
    queryguard.watch(async_engine)
    seen: list[RequestReport] = []

    class Handler:
        async def __call__(self) -> None:
            async with AsyncSession(async_engine) as session:
                await session.scalars(select(Order))

    handle = queryguard.track("handler", default_budget=0, report=seen.append)(
        Handler()
    )
    asyncio.run(handle())
    (report,) = seen
    assert report.statement_count == 1 and report.over_budget


def test_stop_of_a_failed_unit_does_not_raise_its_warning(watched: Engine) -> None:
    unit = queryguard.track("task", report="warn")
    with warnings.catch_warnings():
        warnings.simplefilter("error", QueryGuardWarning)
        unit.start()
        _lazy_loop(watched)
        report = unit.stop(failed=True)
        assert report.findings
        unit.start()
        _lazy_loop(watched)
        with pytest.raises(QueryGuardWarning):
            unit.stop()
