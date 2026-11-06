"""CLEAN: ``with_polymorphic`` joins a collection of each subclass, no product.

A fleet page loads every vehicle with ``with_polymorphic`` and joins the
``Truck.loads`` of the trucks and the ``Van.bookings`` of the vans. A row is
a truck or a van, never both, so the other subclass's join adds one row of
NULLs: the rows per vehicle are its own collection, not a product of two.
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
    with_polymorphic,
)

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


class Base(DeclarativeBase):
    pass


class Vehicle(Base):
    __tablename__ = "vehicle"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    plate: Mapped[str] = mapped_column(String(10))
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "vehicle"}


class Truck(Vehicle):
    __tablename__ = "truck"
    id: Mapped[int] = mapped_column(ForeignKey("vehicle.id"), primary_key=True)
    payload: Mapped[int]
    loads: Mapped[list[Load]] = relationship()
    __mapper_args__ = {"polymorphic_identity": "truck"}


class Van(Vehicle):
    __tablename__ = "van"
    id: Mapped[int] = mapped_column(ForeignKey("vehicle.id"), primary_key=True)
    seats: Mapped[int]
    bookings: Mapped[list[Booking]] = relationship()
    __mapper_args__ = {"polymorphic_identity": "van"}


class Load(Base):
    __tablename__ = "load"
    id: Mapped[int] = mapped_column(primary_key=True)
    truck_id: Mapped[int] = mapped_column(ForeignKey("truck.id"))
    weight: Mapped[int]


class Booking(Base):
    __tablename__ = "booking"
    id: Mapped[int] = mapped_column(primary_key=True)
    van_id: Mapped[int] = mapped_column(ForeignKey("van.id"))
    customer: Mapped[str] = mapped_column(String(40))


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Truck(id=i, plate=f"T{i}", payload=1000) for i in range(1, 6))
        session.add_all(Van(id=i, plate=f"V{i}", seats=8) for i in range(6, 11))
        session.add_all(
            Load(id=t * 10 + k, truck_id=t, weight=100 * k)
            for t in range(1, 6)
            for k in range(6)
        )
        session.add_all(
            Booking(id=v * 10 + k, van_id=v, customer=f"c{k}")
            for v in range(6, 11)
            for k in range(6)
        )
        session.commit()


def fleet(session: Session) -> list[tuple[str, int]]:
    vehicles = with_polymorphic(Vehicle, [Truck, Van])
    stmt = (
        select(vehicles)
        .order_by(vehicles.id)
        .options(joinedload(vehicles.Truck.loads), joinedload(vehicles.Van.bookings))
    )
    rows = []
    for v in session.scalars(stmt).unique():
        if isinstance(v, Truck):
            rows.append((v.plate, sum(load.weight for load in v.loads)))
        elif isinstance(v, Van):
            rows.append((v.plate, len(v.bookings)))
    return rows


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = fleet(session)
    assert len(rows) == 10
