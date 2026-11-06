"""CLEAN: ``deferred`` large columns and a ``column_property`` count, in one query.

The model defers its two large text columns and maps a correlated count of
the comments as a ``column_property``. The list page reads the narrow
columns and the count; the large ones are never loaded, and the count comes
with the row, not one query per row.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, Text, func, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    column_property,
    deferred,
    mapped_column,
)

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


class Base(DeclarativeBase):
    pass


class Note(Base):
    __tablename__ = "note"
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"))
    text: Mapped[str] = mapped_column(String(80))


class Post(Base):
    __tablename__ = "post"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(80))
    slug: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = deferred(mapped_column(Text))
    raw_html: Mapped[str] = deferred(mapped_column(Text))
    note_count: Mapped[int] = column_property(
        select(func.count(Note.id))
        .where(Note.post_id == id)
        .correlate_except(Note)
        .scalar_subquery()
    )


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        for p in range(1, 13):
            session.add(
                Post(
                    id=p,
                    title=f"t{p}",
                    slug=f"s{p}",
                    status="ok",
                    body="b" * 500,
                    raw_html="h" * 500,
                )
            )
            session.add_all(
                Note(id=p * 10 + i, post_id=p, text="n") for i in range(p % 4)
            )
        session.commit()


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = [
            (p.title, p.slug, p.status, p.note_count)
            for p in session.scalars(select(Post).order_by(Post.id))
        ]
    assert rows[3] == ("t4", "s4", "ok", 0)
