"""BUG unused-eager-load: the second link of a ``selectinload`` chain is never read.

``selectinload(Customer.orders).selectinload(Order.lines)``: the report sums
the order totals per customer, which needs the orders, but never looks at
an order line. The third SELECT is wasted.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import shop
from ..kit import BUG, UNUSED, Ctx

VERDICT = BUG
KINDS = frozenset({UNUSED})
setup = shop.setup


def revenue_per_customer(session: Session) -> dict[str, int]:
    stmt = select(shop.Customer).options(
        selectinload(shop.Customer.orders).selectinload(shop.Order.lines)
    )
    return {c.name: sum(o.total for o in c.orders) for c in session.scalars(stmt)}


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        revenue = revenue_per_customer(session)
    assert len(revenue) == 8
