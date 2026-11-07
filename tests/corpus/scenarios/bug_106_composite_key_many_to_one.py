"""BUG relationship-load: a many-to-one over a composite foreign key, read per cart line.

``CartItem.variant`` joins on ``(product_code, size)``, the composite primary
key of ``Variant``. The cart total reads the lazy ``variant`` of every line:
a SELECT per distinct variant.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True


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
        total = sum(item.qty * item.variant.price for item in items)
    assert total > 0
