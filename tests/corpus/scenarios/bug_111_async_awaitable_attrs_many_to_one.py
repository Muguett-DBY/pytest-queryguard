"""BUG relationship-load: ``await order.awaitable_attrs.customer`` for each order.

An async report lists recent orders with the name of their customer and
awaits the lazy many-to-one of each order: a SELECT per distinct customer,
which ``joinedload(Order.customer)`` would fold into the first query.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
setup = shop.setup


async def run(ctx: Ctx) -> None:
    async with AsyncSession(ctx.async_engine()) as session:
        orders = (
            await session.scalars(select(shop.Order).order_by(shop.Order.id).limit(12))
        ).all()
        rows = []
        for order in orders:
            customer = await order.awaitable_attrs.customer
            rows.append((order.id, customer.name))
    assert len(rows) == 12
