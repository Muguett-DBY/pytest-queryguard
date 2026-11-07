"""BUG relationship-load: an imperative (classical) mapping, each ticket's project read lazily.

Plain classes mapped with ``registry.map_imperatively`` onto ``Table``
objects, the style of codebases that keep the domain classes free of
SQLAlchemy. The ticket list reads ``ticket.project`` for each ticket, a lazy
many-to-one: a SELECT per distinct project.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column, ForeignKey, Integer, String, Table, select
from sqlalchemy.orm import Session, registry, relationship

from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})

mapper_registry = registry()

project_table = Table(
    "project",
    mapper_registry.metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(40)),
)

ticket_table = Table(
    "ticket",
    mapper_registry.metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String(80)),
    Column("project_id", ForeignKey("project.id"), index=True),
)


class Project:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class Ticket:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


mapper_registry.map_imperatively(Project, project_table)
mapper_registry.map_imperatively(
    Ticket, ticket_table, properties={"project": relationship(Project)}
)


def setup(ctx: Ctx) -> None:
    ctx.reset()
    mapper_registry.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        for p in range(1, 5):
            project = Project(id=p, name=f"project{p}")
            session.add(project)
        session.flush()
        for t in range(1, 13):
            session.add(Ticket(id=t, title=f"ticket{t}", project_id=1 + t % 4))
        session.commit()


def run(ctx: Ctx) -> None:
    stmt = select(Ticket).order_by(ticket_table.c.id)
    with Session(ctx.engine()) as session:
        board = [(t.title, t.project.name) for t in session.scalars(stmt)]
    assert len(board) == 12
