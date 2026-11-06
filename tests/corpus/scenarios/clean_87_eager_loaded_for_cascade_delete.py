"""CLEAN: the collections are eager-loaded because the delete cascades to them.

A cleanup job deletes stale carts. ``cascade="all, delete-orphan"`` makes the
ORM load each cart's items to delete them; ``selectinload`` loads them in one
query instead of one per cart. The code never reads ``Cart.items`` itself,
the unit of work does.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    selectinload,
)

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


class Base(DeclarativeBase):
    pass


class Cart(Base):
    __tablename__ = "cart"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(String(40))
    stale: Mapped[bool] = mapped_column(default=False)
    items: Mapped[list[CartItem]] = relationship(cascade="all, delete-orphan")


class CartItem(Base):
    __tablename__ = "cart_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    cart_id: Mapped[int] = mapped_column(ForeignKey("cart.id"))
    sku: Mapped[str] = mapped_column(String(20))


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        for c in range(1, 11):
            session.add(Cart(id=c, owner=f"u{c}", stale=c % 2 == 0))
            session.add_all(
                CartItem(id=c * 10 + i, cart_id=c, sku=f"s{i}") for i in range(3)
            )
        session.commit()


def purge_stale_carts(session: Session) -> list[str]:
    stmt = select(Cart).where(Cart.stale.is_(True)).options(selectinload(Cart.items))
    owners = []
    for cart in session.scalars(stmt).all():
        owners.append(cart.owner)  # for the audit log
        session.delete(cart)
    session.commit()
    return owners


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        assert len(purge_stale_carts(session)) == 5
