"""CLEAN: an imperative (classical) mapping with the relationship loaded by ``selectinload``.

Plain classes mapped with ``registry.map_imperatively``; the ticket list asks
for ``selectinload(Ticket.project)``, one more SELECT for all projects.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column, ForeignKey, Integer, String, Table, select
from sqlalchemy.orm import Session, registry, relationship, selectinload

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()

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
    stmt = (
        select(Ticket).order_by(ticket_table.c.id).options(selectinload(Ticket.project))
    )
    with Session(ctx.engine()) as session:
        board = [(t.title, t.project.name) for t in session.scalars(stmt)]
    assert len(board) == 12
