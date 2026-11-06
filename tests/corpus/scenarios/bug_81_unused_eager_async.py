"""BUG unused-eager-load: an async handler eager-loads tags it never returns.

``selectinload(Product.tags)`` on an ``AsyncSession`` query whose response
lists names and prices only.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..domains import shop
from ..kit import BUG, UNUSED, Ctx

VERDICT = BUG
KINDS = frozenset({UNUSED})
setup = shop.setup


async def price_list(session: AsyncSession) -> list[tuple[str, int]]:
    stmt = (
        select(shop.Product)
        .order_by(shop.Product.id)
        .options(selectinload(shop.Product.tags))
    )
    return [(p.name, p.price) for p in await session.scalars(stmt)]


async def run(ctx: Ctx) -> None:
    async with AsyncSession(ctx.async_engine()) as session:
        prices = await price_list(session)
    assert len(prices) == 12
