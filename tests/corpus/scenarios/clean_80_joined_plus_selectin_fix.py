"""CLEAN: the fix of a cartesian load, one ``joinedload`` and a ``selectinload``.

The comments are joined, the tags and the attachments come in their own
SELECTs: three queries, no repeated rows. The article cards read most columns.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from ..domains import content
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = content.setup


def run(ctx: Ctx) -> None:
    stmt = (
        select(content.Article)
        .order_by(content.Article.id)
        .options(
            joinedload(content.Article.comments),
            selectinload(content.Article.tags),
            selectinload(content.Article.attachments),
        )
    )
    with Session(ctx.engine()) as session:
        rows = [
            {
                "title": a.title,
                "url": f"/{a.language}/{a.slug}",
                "teaser": a.summary,
                "date": a.published_at,
                "views": a.views,
                "comments": len(a.comments),
                "tags": [t.name for t in a.tags],
                "files": len(a.attachments),
            }
            for a in session.scalars(stmt).unique()
        ]
    assert len(rows) == 20
