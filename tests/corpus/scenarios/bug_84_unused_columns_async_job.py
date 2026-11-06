"""BUG unused-columns: an async background job reads two columns of each article.

A nightly job adds up the words of the published articles. It loads the
whole rows through an ``AsyncSession`` and reads ``status`` and
``word_count``.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domains import content
from ..kit import BUG, COLUMNS, Ctx

VERDICT = BUG
KINDS = frozenset({COLUMNS})
setup = content.setup


async def words_published(session: AsyncSession) -> int:
    articles = await session.scalars(select(content.Article))
    return sum(a.word_count for a in articles if a.status == "published")


async def run(ctx: Ctx) -> None:
    async with AsyncSession(ctx.async_engine()) as session:
        total = await words_published(session)
    assert total > 0
