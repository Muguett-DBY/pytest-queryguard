"""BUG relationship-load: a list view reads ``customer.orders`` for every row.

The classic: the query loads the customers, the template-ish loop touches the
lazy ``orders`` collection of each one, one SELECT per customer.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
setup = shop.setup


def customer_list_view(session: Session) -> list[dict[str, object]]:
    customers = session.scalars(select(shop.Customer).order_by(shop.Customer.id)).all()
    return [{"name": c.name, "order_count": len(c.orders)} for c in customers]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = customer_list_view(session)
    assert len(rows) == 8
