"""BUG relationship-load: a search loop reads the relationship in its condition.

Unlike a read in the ``return`` only, the ``if`` that decides whether to
return reads ``order.customer.email`` of every order it passes: the
customers are lazy-loaded one by one until the match, here near the end.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
setup = shop.setup


def first_order_of(session: Session, email: str) -> int | None:
    for order in session.scalars(select(shop.Order).order_by(shop.Order.id)):
        if order.customer.email == email:
            return order.id
    return None


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        found = first_order_of(session, "c8@example.com")
    assert found == 22
