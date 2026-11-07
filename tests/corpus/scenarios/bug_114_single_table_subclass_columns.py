"""BUG column-load: single-table inheritance, subclass columns read after a base-class query.

``Car.seats`` and ``Truck.payload_kg`` live in the shared ``vehicle`` table,
but ``select(Vehicle)`` selects only the columns mapped on ``Vehicle``
(SQLAlchemy 2.x). Each vehicle loads its subclass column on first access, a
SELECT per row although the data is in the same table.
``with_polymorphic(Vehicle, [Car, Truck])`` selects them with the list.
"""

from __future__ import annotations

from sqlalchemy import String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from ..kit import BUG, COLUMN, Ctx

VERDICT = BUG
KINDS = frozenset({COLUMN})


class Base(DeclarativeBase):
    pass


class Vehicle(Base):
    __tablename__ = "vehicle"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    plate: Mapped[str] = mapped_column(String(10))
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
        for i in range(1, 13):
            if i % 2:
                session.add(Car(id=i, plate=f"WA{i:04}", seats=5))
            else:
                session.add(Truck(id=i, plate=f"WA{i:04}", payload_kg=1000 * i))
        session.commit()


def describe(vehicle: Vehicle) -> str:
    if isinstance(vehicle, Car):
        return f"{vehicle.plate}: {vehicle.seats} seats"
    assert isinstance(vehicle, Truck)
    return f"{vehicle.plate}: {vehicle.payload_kg} kg"


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        stmt = select(Vehicle).order_by(Vehicle.id)
        rows = [describe(v) for v in session.scalars(stmt)]
    assert len(rows) == 12
