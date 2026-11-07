"""CLEAN: ``session.get()`` with a composite key, answered by the identity map.

The cart page loads every variant of the products in the cart with one
query and keeps the list; then each line looks up its variant with
``session.get(Variant, (code, size))``, which the identity map answers
without SQL.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


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
    with Session(ctx.engine()) as session:
        items = session.scalars(select(CartItem).order_by(CartItem.id)).all()
        codes = {item.product_code for item in items}
        variants = session.scalars(
            select(Variant).where(Variant.product_code.in_(codes))
        ).all()
        total = 0
        for item in items:
            variant = session.get(Variant, (item.product_code, item.size))
            assert variant is not None
            total += item.qty * variant.price
    assert variants and total > 0
