"""CLEAN: a polymorphic collection whose subclasses load with ``polymorphic_load="selectin"``.

The fix of the subclass-columns N+1 on the mapping: each subclass mapper says
``polymorphic_load="selectin"``. ``selectinload(Customer.payments)`` loads the
base rows, then SQLAlchemy runs one SELECT per subclass for all of them, so
reading ``last4`` and ``iban`` needs no query per payment.
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
    payments: Mapped[list[Payment]] = relationship(order_by="Payment.id")


class Payment(Base):
    __tablename__ = "payment"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    method: Mapped[str] = mapped_column(String(10))
    amount: Mapped[int]
    __mapper_args__ = {"polymorphic_on": "method", "polymorphic_identity": "payment"}


class CardPayment(Payment):
    __tablename__ = "card_payment"
    id: Mapped[int] = mapped_column(ForeignKey("payment.id"), primary_key=True)
    last4: Mapped[str] = mapped_column(String(4))
    __mapper_args__ = {"polymorphic_identity": "card", "polymorphic_load": "selectin"}


class TransferPayment(Payment):
    __tablename__ = "transfer_payment"
    id: Mapped[int] = mapped_column(ForeignKey("payment.id"), primary_key=True)
    iban: Mapped[str] = mapped_column(String(34))
    __mapper_args__ = {
        "polymorphic_identity": "transfer",
        "polymorphic_load": "selectin",
    }


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        for c in range(1, 9):
            session.add(Customer(id=c, name=f"customer{c}"))
            session.add(CardPayment(id=2 * c, customer_id=c, amount=c, last4=f"{c:04}"))
            session.add(
                TransferPayment(
                    id=2 * c + 1, customer_id=c, amount=2 * c, iban=f"PL{c:026}"
                )
            )
        session.commit()


def label(payment: Payment) -> str:
    if isinstance(payment, CardPayment):
        return f"card *{payment.last4}"
    assert isinstance(payment, TransferPayment)
    return f"transfer {payment.iban[-4:]}"


def run(ctx: Ctx) -> None:
    stmt = (
        select(Customer).order_by(Customer.id).options(selectinload(Customer.payments))
    )
    with Session(ctx.engine()) as session:
        statement = {
            c.name: [(label(p), p.amount) for p in c.payments]
            for c in session.scalars(stmt)
        }
    assert len(statement) == 8
