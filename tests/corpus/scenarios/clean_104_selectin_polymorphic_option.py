"""CLEAN: ``selectin_polymorphic`` on a query of the joined-table base class.

The staff directory selects ``Employee`` and asks for the subclass columns of
``Engineer`` and ``Manager`` with ``selectin_polymorphic``: one query for the
base rows and one per subclass, whatever the number of employees. Reading
``language`` and ``budget`` afterwards runs no SQL.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    selectin_polymorphic,
)

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = "employee"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(40))
    __mapper_args__ = {"polymorphic_on": "type", "polymorphic_identity": "employee"}


class Engineer(Employee):
    __tablename__ = "engineer"
    id: Mapped[int] = mapped_column(ForeignKey("employee.id"), primary_key=True)
    language: Mapped[str] = mapped_column(String(20))
    __mapper_args__ = {"polymorphic_identity": "engineer"}


class Manager(Employee):
    __tablename__ = "manager"
    id: Mapped[int] = mapped_column(ForeignKey("employee.id"), primary_key=True)
    budget: Mapped[int]
    __mapper_args__ = {"polymorphic_identity": "manager"}


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        for i in range(1, 15):
            if i % 3:
                session.add(Engineer(id=i, name=f"eng{i}", language="python"))
            else:
                session.add(Manager(id=i, name=f"mgr{i}", budget=1000 * i))
        session.commit()


def run(ctx: Ctx) -> None:
    stmt = (
        select(Employee)
        .order_by(Employee.id)
        .options(selectin_polymorphic(Employee, [Engineer, Manager]))
    )
    with Session(ctx.engine()) as session:
        directory = [
            (e.name, e.language if isinstance(e, Engineer) else e.budget)
            for e in session.scalars(stmt)
        ]
    assert len(directory) == 14
