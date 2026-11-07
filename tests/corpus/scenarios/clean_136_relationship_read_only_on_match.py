"""CLEAN: a search loop reads the relationship only of the row it returns.

The loop compares a column of each order and returns as soon as one
matches; ``order.customer`` is read in the ``return`` only, once per call.
One query for the orders and one lazy load.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


def customer_of_total(session: Session, total: int) -> str | None:
    for order in session.scalars(select(shop.Order).order_by(shop.Order.id)):
        if order.total == total:
            return order.customer.email
    return None


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        email = customer_of_total(session, 60)
    assert email == "c4@example.com"
