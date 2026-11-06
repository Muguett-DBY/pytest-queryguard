"""CLEAN: an ``ignore()`` block around ``asyncio.gather`` of per-id tasks.

An async warm-up job fetches each product in its own task, on purpose (each
task feeds a separate cache entry). The tasks are created inside the block,
so they inherit it.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pytest_queryguard import ignore

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


async def warm_cache(ctx: Ctx, ids: list[int]) -> dict[int, str]:
    async def one(pid: int) -> tuple[int, str]:
        async with AsyncSession(ctx.async_engine()) as session:
            stmt = select(shop.Product.name).where(shop.Product.id == pid)
            return pid, (await session.scalars(stmt)).one()

    with ignore("repeated-statement", reason="one cache entry per product"):
        return dict(await asyncio.gather(*(one(pid) for pid in ids)))


async def run(ctx: Ctx) -> None:
    cache = await warm_cache(ctx, list(range(1, 13)))
    assert len(cache) == 12
