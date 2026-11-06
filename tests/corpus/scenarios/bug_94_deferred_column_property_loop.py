"""BUG column-load: a deferred ``column_property`` read for every row.

The model maps a correlated count as ``column_property(..., deferred=True)``
so that ordinary queries skip it. A list page reads it for each post, and
each read runs the deferred load on its own: a query per row.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, func, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    column_property,
    mapped_column,
)

from ..kit import BUG, COLUMN, Ctx

VERDICT = BUG
KINDS = frozenset({COLUMN})


class Base(DeclarativeBase):
    pass


class Vote(Base):
    __tablename__ = "vote"
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"))


class Post(Base):
    __tablename__ = "post"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(80))
    vote_count: Mapped[int] = column_property(
        select(func.count(Vote.id))
        .where(Vote.post_id == id)
        .correlate_except(Vote)
        .scalar_subquery(),
        deferred=True,
    )


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        for p in range(1, 11):
            session.add(Post(id=p, title=f"post{p}"))
            session.add_all(Vote(id=p * 10 + i, post_id=p) for i in range(p % 5))
        session.commit()


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = [(p.title, p.vote_count) for p in session.scalars(select(Post))]
    assert len(rows) == 10
