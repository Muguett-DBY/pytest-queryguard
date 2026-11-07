"""BUG repeated-statement: a "batched" loader called with one customer at a time.

Shape from django-zeal #57 and nplusone #50. ``load_with_orders(ids)`` is a
batch API (``selectinload`` inside), but the caller passes one id per loop
pass, so each call runs its two statements for a single customer: the
customer SELECT and the orders SELECT each run once per customer.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import shop
from ..kit import BUG, REPEAT, Ctx

VERDICT = BUG
KINDS = frozenset({REPEAT})
setup = shop.setup


def load_with_orders(session: Session, ids: list[int]) -> list[shop.Customer]:
    stmt = (
        select(shop.Customer)
        .where(shop.Customer.id.in_(ids))
        .options(selectinload(shop.Customer.orders))
    )
    return list(session.scalars(stmt))


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        totals = {}
        for customer_id in range(1, 9):
            (customer,) = load_with_orders(session, [customer_id])
            totals[customer.name] = sum(o.total for o in customer.orders)
    assert len(totals) == 8
