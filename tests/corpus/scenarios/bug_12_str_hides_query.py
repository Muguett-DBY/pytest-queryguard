"""BUG relationship-load: ``__str__`` reads ``self.author``.

Building a catalogue line with ``str(book)`` looks harmless; the model's
``__str__`` touches the lazy ``author``, one SELECT per distinct author.
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
        catalogue = "\n".join(str(b) for b in books)
    assert "by author1" in catalogue
