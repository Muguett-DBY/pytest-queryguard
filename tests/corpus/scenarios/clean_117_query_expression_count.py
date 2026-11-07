"""CLEAN: a ``query_expression`` filled per query with ``with_expression``.

``Post.comment_count`` is a ``query_expression()``: empty unless a query asks
for it. The list page fills it with a correlated count through
``with_expression``, so every post comes with its count in the one SELECT.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, func, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    query_expression,
    with_expression,
)

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


class Base(DeclarativeBase):
    pass


class Comment(Base):
    __tablename__ = "comment"
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), index=True)


class Post(Base):
    __tablename__ = "post"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(80))
    comment_count: Mapped[int | None] = query_expression()


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        for p in range(1, 11):
            session.add(Post(id=p, title=f"post{p}"))
            session.add_all(Comment(id=p * 10 + i, post_id=p) for i in range(p % 4))
        session.commit()


def run(ctx: Ctx) -> None:
    count = (
        select(func.count(Comment.id))
        .where(Comment.post_id == Post.id)
        .correlate(Post)
        .scalar_subquery()
    )
    stmt = (
        select(Post)
        .order_by(Post.id)
        .options(with_expression(Post.comment_count, count))
    )
    with Session(ctx.engine()) as session:
        rows = [(p.title, p.comment_count) for p in session.scalars(stmt)]
    assert sum(c or 0 for _, c in rows) == sum(p % 4 for p in range(1, 11))
