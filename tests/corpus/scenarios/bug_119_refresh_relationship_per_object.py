"""BUG column-load: ``session.refresh(customer, ["orders"])`` for every customer.

Shape from nplusone #50 and django-zeal #57 (per-item batch APIs in a
loop). A "reload the orders" step refreshes the ``orders`` attribute of each
customer in turn: each call is a SELECT for one customer. A refresh loop is a
column-load finding (the refresh hint), whichever attributes it names;
``select(Customer).options(selectinload(Customer.orders))`` with
``populate_existing`` reloads them in two statements.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, COLUMN, Ctx

VERDICT = BUG
KINDS = frozenset({COLUMN})
setup = shop.setup


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        customers = session.scalars(select(shop.Customer)).all()
        for customer in customers:
            session.refresh(customer, ["orders"])
        counts = [len(c.orders) for c in customers]
    assert counts == [3] * 8
