"""CLEAN: appending to ``WriteOnlyMapped`` collections, then one commit.

An import adds a welcome post to every author through ``author.posts.add()``.
A write-only collection never loads its rows to append, so the loop runs no
SQL; the one ``commit()`` writes all the posts in one flush.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    WriteOnlyMapped,
    mapped_column,
    relationship,
)

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


class Base(DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "author"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    posts: WriteOnlyMapped[Post] = relationship()


class Post(Base):
    __tablename__ = "post"
    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"), index=True)
    title: Mapped[str] = mapped_column(String(80))


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Author(id=a, name=f"author{a}") for a in range(1, 11))
        session.commit()


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        for author in session.scalars(select(Author).order_by(Author.id)):
            author.posts.add(Post(title=f"Welcome, {author.name}"))
        session.commit()
