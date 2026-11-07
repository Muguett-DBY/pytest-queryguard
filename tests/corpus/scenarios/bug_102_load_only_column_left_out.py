"""BUG column-load: ``load_only`` leaves out a column the page then reads.

The book list was narrowed with ``load_only(Book.title)``; later the template
grew an ISBN column. Each ``book.isbn`` loads the column that ``load_only``
deferred, one SELECT per book.
"""

from __future__ import annotations

from sqlalchemy import String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, load_only, mapped_column

from ..kit import BUG, COLUMN, Ctx

VERDICT = BUG
KINDS = frozenset({COLUMN})


class Base(DeclarativeBase):
    pass


class Book(Base):
    __tablename__ = "book"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(80))
    isbn: Mapped[str] = mapped_column(String(13))
    publisher: Mapped[str] = mapped_column(String(40))


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(
            Book(id=i, title=f"book{i}", isbn=f"{i:013}", publisher="acme")
            for i in range(1, 11)
        )
        session.commit()


def render_row(book: Book) -> str:
    return f"<tr><td>{book.title}</td><td>{book.isbn}</td></tr>"


def run(ctx: Ctx) -> None:
    stmt = select(Book).order_by(Book.id).options(load_only(Book.title))
    with Session(ctx.engine()) as session:
        html = "".join(render_row(b) for b in session.scalars(stmt))
    assert html.count("<tr>") == 10
