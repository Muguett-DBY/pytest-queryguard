"""CLEAN: a mapped class whose ``__eq__`` compares a joined-loaded relationship.

Shape from django-zeal #28 (a crash there: recursion in the instrumentation).
``Seat.__eq__`` compares the ``event`` of two seats; the booking page loads
``Seat.event`` with ``joinedload`` and checks ``seat in basket`` for each
seat, which calls ``__eq__`` many times. Every ``event`` is loaded: no SQL in
the loop, and nothing for strict mode or the unused-eager-load check to trip
on.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    joinedload,
    mapped_column,
    relationship,
)

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "event"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))


class Seat(Base):
    __tablename__ = "seat"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(10))
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id"))
    event: Mapped[Event] = relationship()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Seat):
            return NotImplemented
        return self.event == other.event and self.label == other.label

    def __hash__(self) -> int:
        return hash(self.label)


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Event(id=i, name=f"event{i}") for i in range(1, 4))
        session.flush()
        session.add_all(
            Seat(id=i, label=f"R{i % 5}", event_id=1 + i % 3) for i in range(1, 16)
        )
        session.commit()


def run(ctx: Ctx) -> None:
    stmt = select(Seat).order_by(Seat.id).options(joinedload(Seat.event))
    with Session(ctx.engine()) as session:
        seats = session.scalars(stmt).all()
        basket = seats[::4]
        taken = [seat.id for seat in seats if seat in basket]
    assert len(taken) >= len(basket)
