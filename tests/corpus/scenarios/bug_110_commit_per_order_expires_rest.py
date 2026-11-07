"""BUG column-load and write-loop: a ``commit()`` per order, which expires the next ones.

A billing job adds a fee to each order and commits after each, "so a failure
loses only one". The commit expires every object in the session, so each
following order reloads its row before its ``total`` can be read: a SELECT
per order, and an ``UPDATE`` per order from the same line.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, COLUMN, WRITE, Ctx

VERDICT = BUG
KINDS = frozenset({COLUMN, WRITE})
setup = shop.setup


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        orders = session.scalars(
            select(shop.Order).where(shop.Order.customer_id <= 3)
        ).all()
        for order in orders:
            order.total = order.total + 5
            session.commit()
    assert len(orders) == 9
