"""BUG repeated-statement: each row re-read with ``populate_existing`` inside a loop.

A sync job wants the latest status of the orders it holds and re-selects
them one at a time with ``execution_options(populate_existing=True)``. That
is a SELECT by id per order; one ``select`` with ``populate_existing`` over
the whole list would refresh them all.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REPEAT, Ctx

VERDICT = BUG
KINDS = frozenset({REPEAT})
setup = shop.setup


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        orders = session.scalars(
            select(shop.Order).where(shop.Order.customer_id <= 3)
        ).all()
        statuses = []
        for order in orders:
            fresh = session.execute(
                select(shop.Order)
                .where(shop.Order.id == order.id)
                .execution_options(populate_existing=True)
            ).scalar_one()
            statuses.append(fresh.status)
    assert len(statuses) == 9
