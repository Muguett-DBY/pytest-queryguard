"""CLEAN: ``selectinload`` of collections that are all empty in this test.

Shape from nplusone #13 and #36 (false positives there). The list page loads
every customer's orders with ``selectinload`` and reads them; in this test no
customer has an order yet. The loop reads each (empty) collection, so the
eager load is used: no unused eager load, and nothing to fix.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


def setup(ctx: Ctx) -> None:
    shop.setup(ctx, orders=0)


def run(ctx: Ctx) -> None:
    stmt = (
        select(shop.Customer)
        .order_by(shop.Customer.id)
        .options(selectinload(shop.Customer.orders))
    )
    with Session(ctx.engine()) as session:
        page = [(c.name, [o.id for o in c.orders]) for c in session.scalars(stmt)]
    assert all(not orders for _, orders in page)
