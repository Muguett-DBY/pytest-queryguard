"""CLEAN: a migration job decorated with ``@ignore("repeated-statement")``.

A one-off data fix looks up each legacy id on its own on purpose (the
legacy table has no index usable for an ``IN`` list). The decorator records
the choice; the per-id lookups all run inside it.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from pytest_queryguard import ignore

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


@ignore("repeated-statement", reason="one-off migration")
def backfill_names(session: Session, ids: list[int]) -> list[str]:
    names = []
    for pid in ids:
        product = session.scalars(
            select(shop.Product).where(shop.Product.id == pid)
        ).one()
        names.append(product.name.upper())
    return names


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        names = backfill_names(session, list(range(1, 13)))
    assert len(names) == 12
