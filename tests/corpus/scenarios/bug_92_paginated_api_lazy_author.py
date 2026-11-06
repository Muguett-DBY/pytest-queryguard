"""BUG relationship-load and unused-columns: a paginated API reads each author lazily.

``LIMIT 10 OFFSET 0``: the page itself is one query, then ``a.author.name``
loads each author on its own (four authors on the page, four SELECTs).
Pagination does not make the N+1 go away; it caps it at the page size. The
response keeps the title of twelve loaded columns.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import content
from ..domains.content import Article
from ..kit import BUG, COLUMNS, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL, COLUMNS})


def setup(ctx: Ctx) -> None:
    content.setup(ctx, authors=6, articles=2)


def articles_page(
    session: Session, page: int, size: int = 10
) -> list[dict[str, object]]:
    stmt = select(Article).order_by(Article.id).limit(size).offset(page * size)
    return [{"title": a.title, "author": a.author.name} for a in session.scalars(stmt)]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        items = articles_page(session, 0)
    assert len(items) == 10
