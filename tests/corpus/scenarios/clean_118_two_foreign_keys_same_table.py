"""CLEAN: two foreign keys to one table, each loaded with ``selectinload``.

Shape from Prosopite #28 (a false positive there). A ``Transfer`` has a
``source`` and a ``target`` account. The statement page loads both with
``selectinload``: two ``SELECT ... FROM account WHERE id IN (...)`` of the
same shape from the same line, one per relationship, whatever the number of
transfers. That is the fix, not a loop.
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


class Account(Base):
    __tablename__ = "account"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(String(40))


class Transfer(Base):
    __tablename__ = "transfer"
    id: Mapped[int] = mapped_column(primary_key=True)
    amount: Mapped[int]
    source_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    target_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    source: Mapped[Account] = relationship(foreign_keys=[source_id])
    target: Mapped[Account] = relationship(foreign_keys=[target_id])


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Account(id=i, owner=f"owner{i}") for i in range(1, 9))
        session.flush()
        session.add_all(
            Transfer(
                id=t, amount=t * 10, source_id=1 + t % 8, target_id=1 + (t + 3) % 8
            )
            for t in range(1, 21)
        )
        session.commit()


def run(ctx: Ctx) -> None:
    stmt = (
        select(Transfer)
        .order_by(Transfer.id)
        .options(selectinload(Transfer.source), selectinload(Transfer.target))
    )
    with Session(ctx.engine()) as session:
        lines = [
            f"{t.source.owner} -> {t.target.owner}: {t.amount}"
            for t in session.scalars(stmt)
        ]
    assert len(lines) == 20
