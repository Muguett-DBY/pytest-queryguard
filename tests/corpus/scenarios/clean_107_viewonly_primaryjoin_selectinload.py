"""CLEAN: a ``viewonly`` relationship with a custom ``primaryjoin``, loaded with ``selectinload``.

``Customer.open_orders`` is a filtered view of the orders. The support
dashboard loads it with ``selectinload``: one more SELECT for all customers,
whose ``IN`` query carries the custom join condition.
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


class Customer(Base):
    __tablename__ = "customer"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    orders: Mapped[list[Order]] = relationship(back_populates="customer")
    open_orders: Mapped[list[Order]] = relationship(
        primaryjoin="and_(Customer.id == Order.customer_id, Order.status == 'open')",
        viewonly=True,
    )


class Order(Base):
    __tablename__ = "order"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"), index=True)
    status: Mapped[str] = mapped_column(String(10))
    customer: Mapped[Customer] = relationship(back_populates="orders")


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        for c in range(1, 9):
            session.add(Customer(id=c, name=f"customer{c}"))
            session.add_all(
                Order(id=c * 10 + k, customer_id=c, status="open" if k % 2 else "done")
                for k in range(4)
            )
        session.commit()


def run(ctx: Ctx) -> None:
    stmt = (
        select(Customer)
        .order_by(Customer.id)
        .options(selectinload(Customer.open_orders))
    )
    with Session(ctx.engine()) as session:
        board = {c.name: len(c.open_orders) for c in session.scalars(stmt)}
    assert set(board.values()) == {2}
