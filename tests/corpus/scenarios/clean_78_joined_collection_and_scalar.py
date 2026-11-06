"""CLEAN: one joined collection and a joined many-to-one, no product.

The article page joins its comments and its author. The author is one row
per article, so the rows are the number of comments, which is what one
``joinedload`` of a collection costs anyway.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..domains import content
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
_ONE_ROW = (
    "the unused-columns check has no minimum number of rows: a single article "
    "page that reads a few of its 12 columns is reported, which no reviewer "
    "would ask to change"
)
KINDS = frozenset()
KNOWN = {
    "unused-columns": _ONE_ROW,
    "all-opt-in": _ONE_ROW,
}
setup = content.setup


def article_page(session: Session, article_id: int) -> dict[str, object]:
    stmt = (
        select(content.Article)
        .where(content.Article.id == article_id)
        .options(
            joinedload(content.Article.comments), joinedload(content.Article.author)
        )
    )
    article = session.scalars(stmt).unique().one()
    return {
        "title": article.title,
        "body": article.body,
        "author": article.author.name,
        "comments": [(c.author_name, c.body) for c in article.comments],
    }


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        page = article_page(session, 2)
    assert page["author"] == "author1"
