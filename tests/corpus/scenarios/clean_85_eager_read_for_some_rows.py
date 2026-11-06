"""CLEAN: an eager load read for some of the rows only.

The invoice job loads every order with its lines but only expands the lines
of the paid ones; the others are listed by total. The eager load is used,
and loading the lines lazily for the paid orders would be the N+1.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


def setup(ctx: Ctx) -> None:
    shop.setup(ctx)
    with Session(ctx.engine()) as session:
        for order in session.scalars(select(shop.Order).where(shop.Order.id % 3 == 0)):
            order.status = "paid"
        session.commit()


def invoice_lines(session: Session) -> list[str]:
    stmt = (
        select(shop.Order)
        .order_by(shop.Order.id)
        .options(selectinload(shop.Order.lines))
    )
    out = []
    for order in session.scalars(stmt):
        if order.status == "paid":
            out.extend(
                f"{order.id}: {line.qty} x {line.product_id}" for line in order.lines
            )
        else:
            out.append(f"{order.id}: {order.total}")
    return out


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        lines = invoice_lines(session)
    assert len(lines) == 32
