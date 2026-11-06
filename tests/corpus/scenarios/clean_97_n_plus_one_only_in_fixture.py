"""CLEAN: the fixture walks relationships lazily; the code under test does not.

The test's seeding fixture loops over the customers and reads their orders
lazily to build expected values, an N+1 in test set-up code that pytest
fixtures keep out of the measurement. The code under test uses
``selectinload``.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()

EXPECTED: dict[str, int] = {}


def setup(ctx: Ctx) -> None:
    shop.setup(ctx)
    with Session(ctx.engine()) as session:
        customers = session.scalars(select(shop.Customer)).all()
        EXPECTED.clear()
        EXPECTED.update({c.name: len(c.orders) for c in customers})


def order_counts(session: Session) -> dict[str, int]:
    stmt = select(shop.Customer).options(selectinload(shop.Customer.orders))
    return {c.name: len(c.orders) for c in session.scalars(stmt)}


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        assert order_counts(session) == EXPECTED
