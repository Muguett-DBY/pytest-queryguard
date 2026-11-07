"""CLEAN: the sitemap with ``load_only`` of the two columns it prints."""

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from ..domains import content
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = content.setup


def sitemap(session: Session) -> list[str]:
    stmt = (
        select(content.Article)
        .where(content.Article.status == "published")
        .order_by(content.Article.id)
        .options(load_only(content.Article.slug, content.Article.title))
    )
    return [f"<url>/{a.slug}</url><!-- {a.title} -->" for a in session.scalars(stmt)]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        entries = sitemap(session)
    assert len(entries) == 15
