"""CLEAN: a nested loop reads what the outer query's option chain loads.

Shape from orm-lens #146 (a static check that reported this). The query
loads the orders with ``selectinload(Order.lines).joinedload(OrderLine.product)``;
the inner loop over ``order.lines`` reads ``line.product`` of each line. Both
steps of the path come with the orders: three statements in all.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


def invoice_lines(session: Session) -> list[tuple[int, str, int]]:
    stmt = select(shop.Order).options(
        selectinload(shop.Order.lines).joinedload(shop.OrderLine.product)
    )
    rows = []
    for order in session.scalars(stmt):
        for line in order.lines:
            rows.append((order.id, line.product.name, line.qty))
    return rows


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = invoice_lines(session)
    assert len(rows) == 48
