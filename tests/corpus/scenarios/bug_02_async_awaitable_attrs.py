"""BUG relationship-load: ``await customer.awaitable_attrs.orders`` in a loop.

The async spelling of the lazy-load loop: each ``awaitable_attrs`` access runs
its own SELECT, one per customer.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
setup = shop.setup


async def run(ctx: Ctx) -> None:
    async with AsyncSession(ctx.async_engine()) as session:
        customers = (await session.scalars(select(shop.Customer))).all()
        counts = [len(await c.awaitable_attrs.orders) for c in customers]
    assert counts == [3] * 8
