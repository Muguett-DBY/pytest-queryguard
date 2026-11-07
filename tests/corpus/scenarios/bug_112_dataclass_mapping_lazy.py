"""BUG relationship-load: ``MappedAsDataclass`` models, each book's author read lazily.

The models are mapped as dataclasses (``MappedAsDataclass``, ``init=False``
on the keys and relationships). That changes the constructor and ``repr``,
not the loading: the reading list reads the lazy ``author`` of every book.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    MappedAsDataclass,
    Session,
    mapped_column,
    relationship,
)

from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})


class Base(MappedAsDataclass, DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "author"
    name: Mapped[str] = mapped_column(String(40))
    id: Mapped[int] = mapped_column(primary_key=True, default=None)


class Book(Base):
    __tablename__ = "book"
    title: Mapped[str] = mapped_column(String(60))
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"), index=True)
    id: Mapped[int] = mapped_column(primary_key=True, default=None)
    author: Mapped[Author] = relationship(init=False, repr=False)


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Author(name=f"author{a}", id=a) for a in range(1, 6))
        session.flush()
        session.add_all(
            Book(title=f"book{b}", author_id=1 + b % 5, id=b) for b in range(1, 13)
        )
        session.commit()


def run(ctx: Ctx) -> None:
    stmt = select(Book).order_by(Book.id)
    with Session(ctx.engine()) as session:
        reading_list = [f"{b.title} by {b.author.name}" for b in session.scalars(stmt)]
    assert len(reading_list) == 12
