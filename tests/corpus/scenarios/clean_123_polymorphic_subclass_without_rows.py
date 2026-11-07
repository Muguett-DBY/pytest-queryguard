"""CLEAN: an eager load on a subclass relationship when the subclass has no rows.

Shape from nplusone #36 (a false positive there). The org chart selects
``with_polymorphic(Employee, [Manager])`` and loads ``Manager.reports`` with
``selectinload``; the code reads the reports of every manager. This test's
data has no manager, so the loader has nothing to load and nothing reads it:
the option is still the right one for the page.
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
    with_polymorphic,
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
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("manager.id"))
    __mapper_args__ = {"polymorphic_on": "type", "polymorphic_identity": "employee"}


class Manager(Employee):
    __tablename__ = "manager"
    id: Mapped[int] = mapped_column(ForeignKey("employee.id"), primary_key=True)
    reports: Mapped[list[Employee]] = relationship(
        foreign_keys=[Employee.manager_id],
        primaryjoin="Employee.manager_id == Manager.id",
    )
    __mapper_args__ = {
        "polymorphic_identity": "manager",
        "inherit_condition": id == Employee.id,
    }


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Employee(id=i, name=f"e{i}") for i in range(1, 11))
        session.commit()


def run(ctx: Ctx) -> None:
    staff = with_polymorphic(Employee, [Manager])
    stmt = select(staff).order_by(staff.id).options(selectinload(staff.Manager.reports))
    with Session(ctx.engine()) as session:
        chart = {
            e.name: [r.name for r in e.reports] if isinstance(e, Manager) else []
            for e in session.scalars(stmt)
        }
    assert len(chart) == 10
