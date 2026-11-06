"""BUG cartesian-load: two collections with ``lazy="joined"`` on the mapping.

The model asks for both collections to be joined on every query of the
parent, so a plain ``select(Project)`` returns one row per pair of a member and
a milestone. The same product as two ``joinedload`` options, set where the
query does not show it.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..kit import BUG, CARTESIAN, Ctx

VERDICT = BUG
KINDS = frozenset({CARTESIAN})
KNOWN = {
    "*": "loader strategies set on the mapping (lazy='joined') are not options "
    "of the query, and the cartesian-load rule reads only the query's options",
}


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "project"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    members: Mapped[list[Member]] = relationship(lazy="joined")
    milestones: Mapped[list[Milestone]] = relationship(lazy="joined")


class Member(Base):
    __tablename__ = "member"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"))
    login: Mapped[str] = mapped_column(String(40))


class Milestone(Base):
    __tablename__ = "milestone"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"))
    title: Mapped[str] = mapped_column(String(40))


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        for p in range(1, 4):
            session.add(Project(id=p, name=f"p{p}"))
            session.add_all(
                Member(id=p * 100 + i, project_id=p, login=f"u{i}") for i in range(12)
            )
            session.add_all(
                Milestone(id=p * 100 + i, project_id=p, title=f"m{i}")
                for i in range(12)
            )
        session.commit()


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        projects = session.scalars(select(Project)).unique().all()
        board = [(p.name, len(p.members), len(p.milestones)) for p in projects]
    assert board[0] == ("p1", 12, 12)
