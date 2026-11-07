"""BUG relationship-load: single-table inheritance, each vehicle's owner read lazily.

``Car`` and ``Truck`` share the ``vehicle`` table. The fleet report selects
the base class, which loads every subclass in one query, then reads the
lazy many-to-one ``owner`` of each vehicle: a SELECT per distinct owner.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})


class Base(DeclarativeBase):
    pass


class Owner(Base):
    __tablename__ = "owner"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))


class Vehicle(Base):
    __tablename__ = "vehicle"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    plate: Mapped[str] = mapped_column(String(10))
    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id"))
    owner: Mapped[Owner] = relationship()
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "vehicle"}


class Car(Vehicle):
    seats: Mapped[int | None] = mapped_column(nullable=True)
    __mapper_args__ = {"polymorphic_identity": "car"}


class Truck(Vehicle):
    payload_kg: Mapped[int | None] = mapped_column(nullable=True)
    __mapper_args__ = {"polymorphic_identity": "truck"}


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Owner(id=i, name=f"owner{i}") for i in range(1, 7))
        session.flush()
        for i in range(1, 13):
            cls = Car if i % 2 else Truck
            extra = {"seats": 5} if cls is Car else {"payload_kg": 1000 * i}
            session.add(cls(id=i, plate=f"WA{i:04}", owner_id=1 + i % 6, **extra))
        session.commit()


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        stmt = select(Vehicle).order_by(Vehicle.id)
        report = [(v.plate, v.owner.name) for v in session.scalars(stmt)]
    assert len(report) == 12
