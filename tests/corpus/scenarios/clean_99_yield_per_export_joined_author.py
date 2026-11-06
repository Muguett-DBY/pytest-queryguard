"""CLEAN: a streaming export with ``yield_per`` and a joined many-to-one.

A background job writes every article, body included, with its author's name, 50 rows per
batch, ``joinedload(Article.author)`` in the same query. A joined
many-to-one is allowed with ``yield_per`` and adds no query.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..domains import content
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


def setup(ctx: Ctx) -> None:
    content.setup(ctx, authors=10, articles=30, comments=0, attachments=0, links=0)


def export(session: Session) -> int:
    stmt = (
        select(content.Article)
        .options(joinedload(content.Article.author))
        .execution_options(yield_per=50)
    )
    lines = 0
    for article in session.scalars(stmt):
        _ = (
            f"{article.id};{article.title};{article.slug};{article.status};"
            f"{article.published_at};{article.summary};{article.body};"
            f"{article.author.name}"
        )
        lines += 1
    return lines


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        assert export(session) == 300
