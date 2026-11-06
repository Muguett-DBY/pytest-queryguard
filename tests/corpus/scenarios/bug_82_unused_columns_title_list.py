"""BUG unused-columns: a sitemap loads whole articles to print slugs and titles.

Every column of every article, the 2 KB ``body`` and the ``meta`` text
included, travels to the client and into the Session, and two are read.
``load_only(Article.slug, Article.title)`` or a select of the two columns
is the fix.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import content
from ..kit import BUG, COLUMNS, Ctx

VERDICT = BUG
KINDS = frozenset({COLUMNS})
setup = content.setup


def sitemap(session: Session) -> list[str]:
    stmt = (
        select(content.Article)
        .where(content.Article.status == "published")
        .order_by(content.Article.id)
    )
    return [f"<url>/{a.slug}</url><!-- {a.title} -->" for a in session.scalars(stmt)]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        entries = sitemap(session)
    assert len(entries) == 15
