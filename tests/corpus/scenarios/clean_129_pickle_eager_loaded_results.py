"""CLEAN: eager-loaded results pickled for a cache.

Shape from django-zeal #76 (a crash there: the instrumentation could not be
pickled). A cache layer (dogpile.cache, Redis) pickles the result of
``select(Customer)`` with ``selectinload(Customer.orders)`` and the page
reads the unpickled copies. Pickling reads every loaded attribute, the
copies need no SQL, and the plugin's own bookkeeping must not break pickling.
"""

import pickle

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


def run(ctx: Ctx) -> None:
    stmt = (
        select(shop.Customer)
        .order_by(shop.Customer.id)
        .options(selectinload(shop.Customer.orders))
    )
    with Session(ctx.engine()) as session:
        blob = pickle.dumps(session.scalars(stmt).all())
    cached = pickle.loads(blob)
    page = [(c.name, [o.total for o in c.orders]) for c in cached]
    assert len(page) == 8
