"""BUG relationship-load: a model property counts a lazy collection.

``Book.review_count`` is ``len(self.reviews)``; the report sums it over all
books, so each access to the property runs a SELECT. Nothing in the report code
looks like a query.
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
        books = session.scalars(select(library.Book)).all()
        total = sum(b.review_count for b in books)
    assert total == 36
