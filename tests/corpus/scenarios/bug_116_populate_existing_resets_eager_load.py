"""BUG relationship-load: a ``populate_existing`` query drops the lines a first query loaded.

Shape from Bullet #778. Like the identity-map case, but the second query
asks for ``populate_existing`` to read fresh statuses. That repopulates the
instances, and a relationship with the default lazy strategy goes back to
unloaded; the loop then lazy-loads ``lines`` for each order. The second query
needs the ``selectinload`` too.
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
        open_orders = session.scalars(
            select(shop.Order)
            .where(mine, shop.Order.id % 2 == 1)
            .execution_options(populate_existing=True)
        ).all()
        lines = {o.id: [line.qty for line in o.lines] for o in open_orders}
    assert len(lines) == 6
