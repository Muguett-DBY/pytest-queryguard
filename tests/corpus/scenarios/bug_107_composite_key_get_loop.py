"""BUG repeated-statement: ``session.get()`` with a composite key, once per imported line.

A price import receives ``(product_code, size, price)`` rows and looks up each
variant with ``session.get(Variant, (code, size))``; none is in the identity
map yet, so every line is a SELECT by primary key. One ``select`` with a
tuple ``IN`` would load them all.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, String
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..kit import BUG, REPEAT, Ctx

VERDICT = BUG
KINDS = frozenset({REPEAT})


class Base(DeclarativeBase):
    pass


class Variant(Base):
    """A product variant, keyed by product code and size."""

    __tablename__ = "variant"
    product_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    size: Mapped[str] = mapped_column(String(4), primary_key=True)
    price: Mapped[int]


class CartItem(Base):
    __tablename__ = "cart_item"
    __table_args__ = (
        ForeignKeyConstraint(
            ["product_code", "size"], ["variant.product_code", "variant.size"]
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    product_code: Mapped[str] = mapped_column(String(10))
    size: Mapped[str] = mapped_column(String(4))
    qty: Mapped[int]
    variant: Mapped[Variant] = relationship()


SIZES = ("S", "M", "L", "XL")


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(
            Variant(product_code=f"P{p}", size=s, price=100 * p + i)
            for p in range(1, 4)
            for i, s in enumerate(SIZES)
        )
        session.flush()
        session.add_all(
            CartItem(id=i, product_code=f"P{1 + i % 3}", size=SIZES[i % 4], qty=i)
            for i in range(1, 11)
        )
        session.commit()


def run(ctx: Ctx) -> None:
    feed = [(f"P{p}", s, 999) for p in range(1, 4) for s in SIZES[:3]]
    with Session(ctx.engine()) as session:
        changed = 0
        for code, size, price in feed:
            variant = session.get(Variant, (code, size))
            if variant is not None and variant.price != price:
                changed += 1
    assert changed == 9
