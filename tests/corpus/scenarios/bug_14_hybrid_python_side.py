"""BUG relationship-load: a hybrid property evaluated on instances.

``Author.has_books`` has a SQL expression, but the loop filters in Python, so
the instance side runs ``len(self.books)`` for every author.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import library
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
setup = library.setup


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        authors = session.scalars(select(library.Author)).all()
        with_books = [a.name for a in authors if a.has_books]
    assert len(with_books) == 6
