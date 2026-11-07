"""CLEAN: ``undefer_group`` loads a deferred group with the list.

``Article.summary`` and ``Article.body`` are deferred in the group
``"content"``. The feed, which shows the summary, asks for the group with
``undefer_group("content")``, so the columns come in the list query and no
article loads them on its own.
"""

from __future__ import annotations

from sqlalchemy import String, Text, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    undefer_group,
)

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


class Base(DeclarativeBase):
    pass


class Article(Base):
    __tablename__ = "article"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(80))
    summary: Mapped[str] = mapped_column(Text, deferred=True, deferred_group="content")
    body: Mapped[str] = mapped_column(Text, deferred=True, deferred_group="content")


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(
            Article(id=i, title=f"title{i}", summary=f"summary{i}", body="x" * 500)
            for i in range(1, 11)
        )
        session.commit()


def run(ctx: Ctx) -> None:
    stmt = select(Article).order_by(Article.id).options(undefer_group("content"))
    with Session(ctx.engine()) as session:
        feed = [(a.title, a.summary[:40]) for a in session.scalars(stmt)]
    assert len(feed) == 10
