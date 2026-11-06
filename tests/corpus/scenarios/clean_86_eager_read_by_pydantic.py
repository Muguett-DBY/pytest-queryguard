"""CLEAN: Pydantic ``from_attributes`` models read the eager-loaded relationships.

The response schema has nested ``orders`` with nested ``lines``; Pydantic
reads them with ``getattr``, through the ORM's attributes, so both eager
loads are used.
"""

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


class LineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    qty: int
    product_id: int


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    total: int
    lines: list[LineOut]


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str
    orders: list[OrderOut]


def run(ctx: Ctx) -> None:
    stmt = select(shop.Customer).options(
        selectinload(shop.Customer.orders).selectinload(shop.Order.lines)
    )
    with Session(ctx.engine()) as session:
        out = [CustomerOut.model_validate(c) for c in session.scalars(stmt)]
    assert sum(len(o.lines) for c in out for o in c.orders) == 48
