"""BUG unused-columns: a paginated list endpoint returns three fields per row.

Offset pagination over the articles; each page loads full rows and the
response keeps ``id``, ``title`` and ``published_at``.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import content
from ..kit import BUG, COLUMNS, Ctx

VERDICT = BUG
KINDS = frozenset({COLUMNS})
setup = content.setup


def list_articles(
    session: Session, page: int, size: int = 10
) -> list[dict[str, object]]:
    stmt = (
        select(content.Article)
        .order_by(content.Article.id)
        .limit(size)
        .offset(page * size)
    )
    return [
        {"id": a.id, "title": a.title, "published": a.published_at}
        for a in session.scalars(stmt)
    ]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        items = list_articles(session, 0) + list_articles(session, 1)
    assert len(items) == 20
