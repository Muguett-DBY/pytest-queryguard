"""Each unit records its own callers of a call site (#226 in the in-app mode)."""

from __future__ import annotations

import asyncio
import threading

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pytest_queryguard import app as queryguard
from pytest_queryguard.app import RequestReport

from .conftest import Order


def _names(engine: Engine) -> list[str]:
    with Session(engine) as session:
        return [o.customer.name for o in session.scalars(select(Order))]


def view_a(engine: Engine) -> list[str]:
    return _names(engine)


def view_b(engine: Engine) -> list[str]:
    return _names(engine)


def _callers(report: RequestReport) -> list[str]:
    (finding,) = report.findings
    return [function for _, _, function in finding.stack]


def test_each_unit_shows_its_own_callers(watched: Engine) -> None:
    with queryguard.track("a", report=[]) as first:
        view_a(watched)
    with queryguard.track("b", report=[]) as second:
        view_b(watched)
    assert "view_a" in _callers(first)
    assert "view_b" in _callers(second)
    assert "view_a" not in _callers(second)


def test_concurrent_units_keep_their_own_callers(watched: Engine) -> None:
    # Both units are open at once and reach the same lines.
    reports: dict[str, RequestReport] = {}
    inside = threading.Barrier(2)

    def run(view: object, name: str) -> None:
        with queryguard.track(name, report=[]) as report:
            inside.wait(timeout=10)
            view(watched)  # type: ignore[operator]
            inside.wait(timeout=10)
        reports[name] = report

    threads = [
        threading.Thread(target=run, args=(view_a, "a")),
        threading.Thread(target=run, args=(view_b, "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert "view_a" in _callers(reports["a"]) and "view_b" not in _callers(reports["a"])
    assert "view_b" in _callers(reports["b"]) and "view_a" not in _callers(reports["b"])


def test_async_units_keep_their_own_callers(watched: Engine) -> None:
    async def run(view: object, name: str) -> RequestReport:
        async with queryguard.track(name, report=[]) as report:
            await asyncio.sleep(0)
            view(watched)  # type: ignore[operator]
        return report

    async def main() -> list[RequestReport]:
        return list(await asyncio.gather(run(view_a, "a"), run(view_b, "b")))

    first, second = asyncio.run(main())
    assert "view_a" in _callers(first)
    assert "view_b" in _callers(second)
