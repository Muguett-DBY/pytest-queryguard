"""BUG relationship-load: the option on its own line loads one level of two.

The statement is built over several lines and
``stmt = stmt.options(selectinload(Customer.orders))`` loads the orders with
the customers, but the inner loop also reads ``order.lines``, which no option
names: one SELECT per order.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
setup = shop.setup


def order_sizes(session: Session, name: str | None = None) -> list[tuple[str, int]]:
    stmt = select(shop.Customer)
    if name is not None:
        stmt = stmt.where(shop.Customer.name == name)
    stmt = stmt.options(selectinload(shop.Customer.orders))
    rows = []
    for customer in session.scalars(stmt):
        for order in customer.orders:
            rows.append((customer.name, len(order.lines)))
    return rows


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = order_sizes(session)
    assert len(rows) == 24
