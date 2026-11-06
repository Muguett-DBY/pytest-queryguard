"""CLEAN: a chain of joined collections, ``articles`` then their ``comments``.

``joinedload(Author.articles).joinedload(Article.comments)``: one row per
comment (and per article without comments). The rows grow with the deepest
collection, not as a product of two siblings.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..domains import content
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = content.setup


def run(ctx: Ctx) -> None:
    stmt = (
        select(content.Author)
        .order_by(content.Author.id)
        .options(
            joinedload(content.Author.articles).joinedload(content.Article.comments)
        )
    )
    with Session(ctx.engine()) as session:
        stats = {
            author.name: sum(len(a.comments) for a in author.articles)
            for author in session.scalars(stmt).unique()
        }
    assert stats["author1"] == 40
