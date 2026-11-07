"""CLEAN: an edit form reads most of the article's columns.

The form shows title, slug, summary, body, status, language and the
publication date: seven of twelve columns, plus the keys. Loading the whole
row is right.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import content
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = content.setup


def edit_forms(session: Session, ids: list[int]) -> list[dict[str, object]]:
    stmt = select(content.Article).where(content.Article.id.in_(ids))
    return [
        {
            "title": a.title,
            "slug": a.slug,
            "summary": a.summary,
            "body": a.body,
            "status": a.status,
            "language": a.language,
            "published_at": a.published_at,
        }
        for a in session.scalars(stmt)
    ]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        forms = edit_forms(session, [1, 2, 3])
    assert len(forms) == 3
