"""BUG relationship-load: an ``ignore()`` block that one later access escapes.

The export of the first orders is wrapped in ``ignore("relationship-load")``,
but the caller then prints the customer of each of the last orders: lazy
loads outside the block, from the same parent query. The N+1 is no longer
only the accepted one; the documented rule is to report it in full.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from pytest_queryguard import ignore

from ..domains import shop
from ..domains.shop import Order
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
setup = shop.setup


def export_orders(session: Session) -> list[Order]:
    orders = session.scalars(select(Order).order_by(Order.id)).all()
    with ignore("relationship-load", reason="export"):
        for o in orders[:12]:
            _ = o.customer.email
    return list(orders)


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        orders = export_orders(session)
        footer = {o.customer.name for o in orders[-6:]}
    assert footer == {"customer7", "customer8"}
