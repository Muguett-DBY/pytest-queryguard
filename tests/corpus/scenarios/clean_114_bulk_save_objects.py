"""CLEAN: ``session.bulk_save_objects()`` for a list of new rows.

A seed command builds forty tags and saves them with
``bulk_save_objects``: one ``INSERT`` sent as executemany, no flush per row.
"""

from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        session.bulk_save_objects(
            [shop.Tag(id=100 + i, name=f"bulk{i}") for i in range(40)]
        )
        session.commit()
