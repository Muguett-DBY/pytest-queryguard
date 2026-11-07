"""CLEAN: two detail lookups at different lines, each touching one relationship.

A "compare two orders" page loads one order with ``session.get`` and the
other with a ``select``, and shows each one's customer. Two lazy
loads of ``Order.customer``, but for one parent per query: no loop over
rows. The report groups the parents by the query line that loaded them, as
strict mode does, so neither raises (#307).
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


def compare(session: Session, left_id: int, right_id: int) -> tuple[str, str]:
    left = session.get(shop.Order, left_id)
    right = session.scalars(select(shop.Order).where(shop.Order.id == right_id)).one()
    assert left is not None
    return left.customer.name, right.customer.name


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        names = compare(session, 1, 24)
    assert names == ("customer1", "customer8")
