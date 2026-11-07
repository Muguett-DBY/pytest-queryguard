"""BUG column-load: a ``deferred_group`` read on every row of a list.

``Article.summary`` and ``Article.body`` are deferred in the group
``"content"`` so that ordinary queries skip them. The feed shows a summary
per article; the first access to a column of the group loads the whole group
for that one row: a SELECT per article. ``undefer_group("content")`` would
load it with the list.
"""

from __future__ import annotations

from sqlalchemy import String, Text, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
)

from ..kit import BUG, COLUMN, Ctx

VERDICT = BUG
KINDS = frozenset({COLUMN})


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
    stmt = select(Article).order_by(Article.id)
    with Session(ctx.engine()) as session:
        feed = [(a.title, a.summary[:40]) for a in session.scalars(stmt)]
    assert len(feed) == 10
