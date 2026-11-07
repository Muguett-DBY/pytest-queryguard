"""BUG repeated-statement: a ``WriteOnlyMapped`` collection queried for each author.

``Author.posts`` is ``write_only``: it never loads by itself, the code asks
for rows with ``author.posts.select()``. The dashboard runs that select for
every author to show their three latest posts: one SELECT per author, the
same statement with another ``author_id``.
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

from ..kit import BUG, REPEAT, Ctx

VERDICT = BUG
KINDS = frozenset({REPEAT})


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
        for a in range(1, 9):
            session.add(Author(id=a, name=f"author{a}"))
            session.add_all(
                Post(id=a * 10 + p, author_id=a, title=f"post{a}.{p}") for p in range(5)
            )
        session.commit()


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        dashboard = {}
        for author in session.scalars(select(Author).order_by(Author.id)):
            latest = author.posts.select().order_by(Post.id.desc()).limit(3)
            dashboard[author.name] = [p.title for p in session.scalars(latest)]
    assert len(dashboard) == 8
