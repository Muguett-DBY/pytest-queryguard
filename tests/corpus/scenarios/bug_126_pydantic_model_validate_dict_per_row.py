"""BUG relationship-load: ``model_validate`` of a dict built from each row.

A Pydantic schema is filled from a dict per order, and the dict reads
``order.customer.name``: the many-to-one is lazy-loaded once per customer
while the list of schemas is built.
"""

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
setup = shop.setup


class OrderRow(BaseModel):
    id: int
    customer: str
    total: int


def order_rows(session: Session) -> list[OrderRow]:
    orders = session.scalars(select(shop.Order).order_by(shop.Order.id)).all()
    return [
        OrderRow.model_validate(
            {"id": o.id, "customer": o.customer.name, "total": o.total}
        )
        for o in orders
    ]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = order_rows(session)
    assert len(rows) == 24
