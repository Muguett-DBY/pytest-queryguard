"""BUG repeated-statement: ``asyncio.gather`` fans out one lookup per id.

An ``AsyncSession`` cannot run statements concurrently, so each task opens its
own; the fan-out still sends one ``SELECT`` per id, only in parallel. One
``IN`` query (or ``selectinload``) replaces the ten.
"""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

from ..domains import shop
from ..kit import BUG, REPEAT, Ctx

VERDICT = BUG
KINDS = frozenset({REPEAT})
KNOWN = {
    "session-scope": "by design: with queryguard_scope = session each Session is counted on its own, and each task runs one lookup"
}
setup = shop.setup


async def run(ctx: Ctx) -> None:
    sessions = async_sessionmaker(ctx.async_engine())

    async def price(product_id: int) -> int:
        async with sessions() as session:
            product = await session.get(shop.Product, product_id)
            return product.price

    prices = await asyncio.gather(*(price(i) for i in range(1, 11)))
    assert prices == [10 * i for i in range(1, 11)]
