"""BUG relationship-load: the eager load ran in one Session, the loop reads in another.

Shape from Bullet #778. The badge query with ``selectinload(Order.lines)``
runs in a request-scoped Session; a helper opens its own Session for the open
orders and reads their lines. The identity map is per Session, so the eager
load does not help there: a lazy load per order.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
setup = shop.setup


def run(ctx: Ctx) -> None:
    mine = shop.Order.customer_id <= 4
    with Session(ctx.engine()) as session:
        badges = session.scalars(
            select(shop.Order).where(mine).options(selectinload(shop.Order.lines))
        ).all()
        assert all(len(o.lines) == 2 for o in badges)
        with Session(ctx.engine()) as other:
            open_orders = other.scalars(
                select(shop.Order).where(mine, shop.Order.id % 2 == 1)
            ).all()
            lines = {o.id: [line.qty for line in o.lines] for o in open_orders}
    assert len(lines) == 6
