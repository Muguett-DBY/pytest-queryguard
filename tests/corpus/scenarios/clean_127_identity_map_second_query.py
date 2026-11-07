"""CLEAN: a second query returns objects whose collection the first one loaded.

Shape from Bullet #778 (the inverse: in SQLAlchemy the identity map shares
the instance). A page loads all orders of customers 1-4 with
``selectinload(Order.lines)`` for the badges, then a second query without the
option selects the open ones among them and reads their lines. The second
query hands back the same instances, whose ``lines`` are loaded already: no
SELECT per order.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


def run(ctx: Ctx) -> None:
    mine = shop.Order.customer_id <= 4
    with Session(ctx.engine()) as session:
        badges = session.scalars(
            select(shop.Order).where(mine).options(selectinload(shop.Order.lines))
        ).all()
        assert all(len(o.lines) == 2 for o in badges)
        open_orders = session.scalars(
            select(shop.Order).where(mine, shop.Order.id % 2 == 1)
        ).all()
        lines = {o.id: [line.qty for line in o.lines] for o in open_orders}
    assert len(lines) == 6
