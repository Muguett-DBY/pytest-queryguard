"""BUG repeated-statement: an ``IN`` lookup per order, with lists of one to four ids.

Shape from django-perf-rec #261 (``IN (?)`` and ``IN (?, ?, ?)`` must be one
shape). Each order's products are looked up with ``Product.id.in_(ids)``,
and the orders have one to four lines, so the lists differ in length. They
are still one statement run per order; ``selectinload`` of the lines and
their products loads them in two.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REPEAT, Ctx

VERDICT = BUG
KINDS = frozenset({REPEAT})
setup = shop.setup


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        order_ids = session.scalars(select(shop.Order.id).order_by(shop.Order.id)).all()
        names = {}
        for n, order_id in enumerate(order_ids[:8]):
            product_ids = [1 + (order_id + k) % 12 for k in range(1 + n % 4)]
            names[order_id] = session.scalars(
                select(shop.Product.name).where(shop.Product.id.in_(product_ids))
            ).all()
    assert len(names) == 8
