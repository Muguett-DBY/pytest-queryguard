"""BUG repeated-statement: an ``exists()`` uniqueness check for every imported row.

Shape from Prosopite #1. A CSV import validates that each row's SKU is not
taken yet with ``select(exists().where(Product.name == row))``: one SELECT
per row. One query with ``Product.name.in_(names)`` answers all of them.
"""

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REPEAT, Ctx

VERDICT = BUG
KINDS = frozenset({REPEAT})
setup = shop.setup


def run(ctx: Ctx) -> None:
    incoming = [f"product{i}" for i in range(8, 18)]
    with Session(ctx.engine()) as session:
        taken = [
            name
            for name in incoming
            if session.scalar(select(exists().where(shop.Product.name == name)))
        ]
    assert taken == [f"product{i}" for i in range(8, 13)]
