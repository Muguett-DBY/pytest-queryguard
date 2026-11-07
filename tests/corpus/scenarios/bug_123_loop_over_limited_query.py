"""BUG relationship-load: a loop over ``select(...).limit(10)`` reading each customer.

Shape from orm-lens #145. The "latest orders" widget takes the ten newest
orders with ``limit(10)`` and shows each customer's e-mail. A ``LIMIT`` caps
the loop, it does not batch it: the many-to-one is still lazy-loaded per
distinct customer.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
setup = shop.setup


def run(ctx: Ctx) -> None:
    stmt = select(shop.Order).order_by(shop.Order.id.desc()).limit(10)
    with Session(ctx.engine()) as session:
        widget = [(o.id, o.customer.email) for o in session.scalars(stmt)]
    assert len(widget) == 10
