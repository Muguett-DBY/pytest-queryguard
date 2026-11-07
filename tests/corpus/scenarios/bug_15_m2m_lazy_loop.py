"""BUG relationship-load: a many-to-many ``Product.tags`` read per product.

The association table makes each lazy load a join through ``product_tag``; the
loop still pays one per product.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
setup = shop.setup


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        products = session.scalars(select(shop.Product)).all()
        labels = {p.name: sorted(t.name for t in p.tags) for p in products}
    assert len(labels) == 12
