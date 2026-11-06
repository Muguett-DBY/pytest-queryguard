"""CLEAN: one joined collection on a paginated query.

``select(Author).options(joinedload(Author.articles)).limit(2)``: SQLAlchemy
wraps the limited parent query in a subquery and joins the one collection to
it. One collection, so no product.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..domains import content
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = content.setup


def page(session: Session, number: int, size: int = 2) -> list[tuple[str, int]]:
    stmt = (
        select(content.Author)
        .order_by(content.Author.id)
        .options(joinedload(content.Author.articles))
        .limit(size)
        .offset(number * size)
    )
    return [(a.name, len(a.articles)) for a in session.scalars(stmt).unique()]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        first = page(session, 0)
        second = page(session, 1)
    assert [n for n, _ in first + second] == [f"author{i}" for i in range(1, 5)]
