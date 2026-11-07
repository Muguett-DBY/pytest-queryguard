"""BUG column-load: a polymorphic collection, subclass columns read per payment.

``Customer.payments`` points at the joined-table base ``Payment``. The
statement page loads the collection with ``selectinload``, which reads the
base table only; ``last4`` and ``iban`` live in the subclass tables, so each
payment loads its subclass row on first access: a SELECT per payment.
``selectin_polymorphic`` or ``polymorphic_load="selectin"`` would load them
in one query per subclass.
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

from ..kit import BUG, COLUMN, Ctx

VERDICT = BUG
KINDS = frozenset({COLUMN})


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
    __mapper_args__ = {"polymorphic_identity": "card"}


class TransferPayment(Payment):
    __tablename__ = "transfer_payment"
    id: Mapped[int] = mapped_column(ForeignKey("payment.id"), primary_key=True)
    iban: Mapped[str] = mapped_column(String(34))
    __mapper_args__ = {"polymorphic_identity": "transfer"}


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
