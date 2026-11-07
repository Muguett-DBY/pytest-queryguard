"""BUG relationship-load: concrete-table inheritance, each payment's store read lazily.

``CardPayment`` and ``TransferPayment`` have tables of their own, queried
together through ``ConcreteBase``'s polymorphic union. The settlement report
reads the lazy ``store`` of every payment: a SELECT per distinct store, for
each subclass.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.ext.declarative import ConcreteBase
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})


class Base(DeclarativeBase):
    pass


class Store(Base):
    __tablename__ = "store"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))


class Payment(ConcreteBase, Base):
    __tablename__ = "payment"
    id: Mapped[int] = mapped_column(primary_key=True)
    amount: Mapped[int]
    store_id: Mapped[int] = mapped_column(ForeignKey("store.id"))
    store: Mapped[Store] = relationship()
    __mapper_args__ = {"polymorphic_identity": "payment", "concrete": True}


class CardPayment(Payment):
    __tablename__ = "card_payment"
    id: Mapped[int] = mapped_column(primary_key=True)
    amount: Mapped[int]
    store_id: Mapped[int] = mapped_column(ForeignKey("store.id"))
    last4: Mapped[str] = mapped_column(String(4))
    store: Mapped[Store] = relationship()
    __mapper_args__ = {"polymorphic_identity": "card", "concrete": True}


class TransferPayment(Payment):
    __tablename__ = "transfer_payment"
    id: Mapped[int] = mapped_column(primary_key=True)
    amount: Mapped[int]
    store_id: Mapped[int] = mapped_column(ForeignKey("store.id"))
    iban: Mapped[str] = mapped_column(String(34))
    store: Mapped[Store] = relationship()
    __mapper_args__ = {"polymorphic_identity": "transfer", "concrete": True}


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Store(id=i, name=f"store{i}") for i in range(1, 5))
        session.flush()
        for i in range(1, 9):
            session.add(
                CardPayment(id=i, amount=10 * i, store_id=1 + i % 4, last4=f"{i:04}")
            )
            session.add(
                TransferPayment(
                    id=100 + i, amount=20 * i, store_id=1 + i % 4, iban=f"PL{i:026}"
                )
            )
        session.commit()


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        payments = session.scalars(select(Payment)).all()
        by_store: dict[str, int] = {}
        for payment in payments:
            name = payment.store.name
            by_store[name] = by_store.get(name, 0) + payment.amount
    assert len(by_store) == 4
