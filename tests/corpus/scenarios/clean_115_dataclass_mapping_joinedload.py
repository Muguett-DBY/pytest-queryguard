"""CLEAN: ``MappedAsDataclass`` models with ``joinedload`` of the author.

The same dataclass-mapped models as the lazy version; the reading list
joins each book's author into the one query.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    MappedAsDataclass,
    Session,
    joinedload,
    mapped_column,
    relationship,
)

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


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
    stmt = select(Book).order_by(Book.id).options(joinedload(Book.author))
    with Session(ctx.engine()) as session:
        reading_list = [f"{b.title} by {b.author.name}" for b in session.scalars(stmt)]
    assert len(reading_list) == 12
