"""BUG unused-columns: articles loaded by ``selectinload`` with one column read.

An author index lists the titles of each author's articles. The authors are
narrow, but ``selectinload(Author.articles)`` loads every column of every
article, ``body`` included, and only ``title`` is read.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import content
from ..kit import BUG, COLUMNS, Ctx

VERDICT = BUG
KINDS = frozenset({COLUMNS})
KNOWN = {
    "unused-columns": "the unused-columns check groups only the instances a "
    "query returns itself, not those loaded through a relationship",
    "all-opt-in": "the unused-columns check groups only the instances a "
    "query returns itself, not those loaded through a relationship",
}
setup = content.setup


def author_index(session: Session) -> dict[str, list[str]]:
    stmt = select(content.Author).options(selectinload(content.Author.articles))
    return {a.name: [x.title for x in a.articles] for a in session.scalars(stmt)}


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        index = author_index(session)
    assert len(index["author1"]) == 5
