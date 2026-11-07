"""BUG relationship-load: Pydantic ``from_attributes`` models over lazy relationships.

``OrderOut.model_validate(order)`` reads ``order.customer`` and ``order.lines``
to fill the nested models, so validating a list of ORM objects lazy-loads for
each of them.
"""

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
KNOWN = {
    "strict": "Pydantic catches the strict NPlusOneDetected and raises its own "
    "validation error holding its message; the test fails with that error, so "
    "the report at the end, which records the finding, does not run",
}
setup = shop.setup


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


class LineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int
    qty: int


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
    customer: CustomerOut
    lines: list[LineOut]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        orders = session.scalars(select(shop.Order)).all()
        out = [OrderOut.model_validate(o) for o in orders]
    assert len(out) == 24 and len(out[0].lines) == 2
