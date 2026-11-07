"""BUG relationship-load: ``lazy="immediate"`` on the mapping, a SELECT per loaded customer.

``Customer.orders`` is mapped with ``lazy="immediate"``: the collection is
loaded as each customer is loaded, by a SELECT of its own, the same query a
lazy load runs. A list of customers is one query plus one per customer even
before any code reads the orders.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customer"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    orders: Mapped[list[Order]] = relationship(lazy="immediate")


class Order(Base):
    __tablename__ = "order"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"), index=True)
    total: Mapped[int]


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Customer(id=c, name=f"customer{c}") for c in range(1, 9))
        session.flush()
        session.add_all(
            Order(id=c * 10 + k, customer_id=c, total=k)
            for c in range(1, 9)
            for k in range(3)
        )
        session.commit()


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        customers = session.scalars(select(Customer).order_by(Customer.id)).all()
        summary = {c.name: len(c.orders) for c in customers}
    assert sum(summary.values()) == 24
