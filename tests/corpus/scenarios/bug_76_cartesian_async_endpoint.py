"""BUG cartesian-load: an async endpoint joins comments and tags side by side.

The ``AsyncSession`` version of the detail page: ``joinedload`` of two
collections of the article in one SELECT.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ..domains import content
from ..kit import BUG, CARTESIAN, Ctx

VERDICT = BUG
_ONE_ROW = (
    "the unused-columns check has no minimum number of rows: a single article "
    "page that reads a few of its 12 columns is reported, which no reviewer "
    "would ask to change"
)
KINDS = frozenset({CARTESIAN})
KNOWN = {
    "unused-columns": _ONE_ROW,
    "all-opt-in": _ONE_ROW,
}
setup = content.setup


async def get_article(session: AsyncSession, article_id: int) -> dict[str, object]:
    stmt = (
        select(content.Article)
        .where(content.Article.id == article_id)
        .options(joinedload(content.Article.comments), joinedload(content.Article.tags))
    )
    article = (await session.scalars(stmt)).unique().one()
    return {
        "title": article.title,
        "comments": [c.body for c in article.comments],
        "tags": [t.name for t in article.tags],
    }


async def run(ctx: Ctx) -> None:
    async with AsyncSession(ctx.async_engine()) as session:
        page = await get_article(session, 3)
    assert page["title"] == "Article 3"
