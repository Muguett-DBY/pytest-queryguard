"""CLEAN: ``session.merge(obj, load=False)`` for objects from a cache.

A request rehydrates products that an earlier session loaded and a cache
kept (detached, unchanged). ``merge(load=False)`` attaches a copy of each
without asking the database, as SQLAlchemy documents for caches; reading
their names runs no SQL.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


def run(ctx: Ctx) -> None:
    with Session(ctx.engine(), expire_on_commit=False) as warm:
        cache = list(warm.scalars(select(shop.Product).order_by(shop.Product.id)))
    with Session(ctx.engine()) as session:
        merged = [session.merge(p, load=False) for p in cache]
        names = [p.name for p in merged]
    assert len(names) == 12
