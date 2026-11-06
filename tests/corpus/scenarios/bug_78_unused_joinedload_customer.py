"""BUG unused-eager-load: ``joinedload(Order.customer)`` and only the foreign key read.

An order list for an internal dashboard was copied from the page that shows
the customer's name; this one shows ``customer_id`` only, which is a column
of the order. The joined customer is never read.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..domains import shop
from ..kit import BUG, UNUSED, Ctx

VERDICT = BUG
KINDS = frozenset({UNUSED})
setup = shop.setup


def order_rows(session: Session) -> list[dict[str, object]]:
    stmt = (
        select(shop.Order)
        .order_by(shop.Order.id)
        .options(joinedload(shop.Order.customer))
    )
    return [
        {"id": o.id, "customer": o.customer_id, "total": o.total, "status": o.status}
        for o in session.scalars(stmt)
    ]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = order_rows(session)
    assert len(rows) == 24
