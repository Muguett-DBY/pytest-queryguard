"""BUG relationship-load: a polymorphic list reads a subclass collection per row.

Joined-table inheritance: ``Vehicle`` with ``Truck`` and ``Van``. The fleet
page selects every vehicle with ``with_polymorphic`` (so the subclass columns
come in one query) but reads each vehicle's ``services`` collection lazily.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    with_polymorphic,
)

from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})


class Base(DeclarativeBase):
    pass


class Vehicle(Base):
    __tablename__ = "vehicle"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    plate: Mapped[str] = mapped_column(String(10))
    services: Mapped[list[Service]] = relationship()
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "vehicle"}


class Truck(Vehicle):
    __tablename__ = "truck"
    id: Mapped[int] = mapped_column(ForeignKey("vehicle.id"), primary_key=True)
    payload: Mapped[int]
    __mapper_args__ = {"polymorphic_identity": "truck"}


class Van(Vehicle):
    __tablename__ = "van"
    id: Mapped[int] = mapped_column(ForeignKey("vehicle.id"), primary_key=True)
    seats: Mapped[int]
    __mapper_args__ = {"polymorphic_identity": "van"}


class Service(Base):
    __tablename__ = "service"
    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicle.id"))
    note: Mapped[str] = mapped_column(String(40))


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(
            Truck(id=i, plate=f"T{i}", payload=1000 * i) for i in range(1, 6)
        )
        session.add_all(Van(id=i, plate=f"V{i}", seats=i) for i in range(6, 11))
        session.add_all(
            Service(id=v * 10 + k, vehicle_id=v, note="oil")
            for v in range(1, 11)
            for k in range(2)
        )
        session.commit()


def fleet(session: Session) -> list[tuple[str, int, int]]:
    vehicles = with_polymorphic(Vehicle, [Truck, Van])
    rows = []
    for v in session.scalars(select(vehicles).order_by(vehicles.id)):
        size = v.payload if isinstance(v, Truck) else v.seats  # type: ignore[attr-defined]
        rows.append((v.plate, size, len(v.services)))
    return rows


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = fleet(session)
    assert len(rows) == 10
