"""BUG relationship-load: a loop over ``select(aliased(Order))`` reads the customer.

The query selects an ``aliased()`` entity (the alias is there for a
self-join in the real code); its rows are ordinary ``Order`` objects and
``o.customer`` is lazy-loaded for each one.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
setup = shop.setup


def order_emails(session: Session) -> list[tuple[int, str]]:
    order = aliased(shop.Order)
    stmt = select(order).where(order.total > 0).order_by(order.id)
    return [(o.id, o.customer.email) for o in session.scalars(stmt)]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = order_emails(session)
    assert len(rows) == 24
