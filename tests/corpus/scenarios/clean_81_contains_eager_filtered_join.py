"""CLEAN: ``contains_eager`` of the approved comments the query joined to filter.

"Articles with their approved comments": the query joins the comments to
keep only approved ones and fills ``Article.comments`` from that join, and
joins the author for the byline. One collection, chosen rows, one query.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, contains_eager

from ..domains import content
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = content.setup


def approved_threads(session: Session) -> list[tuple[str, str, str, int]]:
    stmt = (
        select(content.Article)
        .join(content.Article.comments)
        .join(content.Article.author)
        .where(content.Comment.approved.is_(True))
        .order_by(content.Article.id)
        .options(
            contains_eager(content.Article.comments),
            contains_eager(content.Article.author),
        )
    )
    return [
        (
            f"{a.title} ({a.language}, {a.status})",
            f"/{a.slug} {a.published_at:%Y-%m-%d}: {a.summary[:20]}",
            a.author.name,
            len(a.comments),
        )
        for a in session.scalars(stmt).unique()
    ]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        threads = approved_threads(session)
    assert len(threads) == 20
    assert all(count == 6 for *_, count in threads)
