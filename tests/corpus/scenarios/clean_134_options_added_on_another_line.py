"""CLEAN: the statement is built over several lines and gets its option last.

A list view starts from ``select(Customer)``, adds a filter when one is
asked for and the loader option on a line of its own
(``stmt = stmt.options(selectinload(Customer.orders))``), then loops over
the customers and their orders. The orders come with the customers.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


def customer_orders(session: Session, name: str | None = None) -> list[tuple[str, int]]:
    stmt = select(shop.Customer)
    if name is not None:
        stmt = stmt.where(shop.Customer.name == name)
    stmt = stmt.order_by(shop.Customer.id)
    stmt = stmt.options(selectinload(shop.Customer.orders))
    rows = []
    for customer in session.scalars(stmt):
        for order in customer.orders:
            rows.append((customer.name, order.total))
    return rows


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = customer_orders(session)
    assert len(rows) == 24
