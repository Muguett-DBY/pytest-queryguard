"""BUG relationship-load: an ``if`` guard does not make a lazy read run once.

Only the orders that pass ``if order.total > 50`` read their lines, but
that is most of them: one SELECT per passing order. The result list is
first given another name (``open_orders = orders``), as code often does.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
setup = shop.setup


def big_orders(session: Session) -> dict[int, int]:
    orders = session.scalars(select(shop.Order)).all()
    open_orders = orders
    sizes = {}
    for order in open_orders:
        if order.total > 50:
            sizes[order.id] = sum(line.qty for line in order.lines)
    return sizes


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        sizes = big_orders(session)
    assert len(sizes) == 14
