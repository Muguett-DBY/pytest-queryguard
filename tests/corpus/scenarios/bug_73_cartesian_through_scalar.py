"""BUG cartesian-load: a scalar on the way does not stop the product.

A moderation queue loads comments with their article and the article's tags
(``joinedload(Comment.article).joinedload(Article.tags)``) and, next to it,
each comment's replies (``joinedload(Comment.replies)``). The article is one
row per comment, but tags and replies are two collections in one SELECT:
every comment comes back once per pair of a tag and a reply.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..domains import content
from ..kit import BUG, CARTESIAN, Ctx

VERDICT = BUG
KINDS = frozenset({CARTESIAN})


def setup(ctx: Ctx) -> None:
    content.setup(ctx, authors=2, articles=3, comments=3, replies=3)


def moderation_queue(session: Session) -> list[tuple[str, list[str], int]]:
    stmt = (
        select(content.Comment)
        .where(content.Comment.parent_id.is_(None))
        .order_by(content.Comment.id)
        .options(
            joinedload(content.Comment.article).joinedload(content.Article.tags),
            joinedload(content.Comment.replies),
        )
    )
    return [
        (c.body, [t.name for t in c.article.tags], len(c.replies))
        for c in session.scalars(stmt).unique()
    ]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        queue = moderation_queue(session)
    assert len(queue) == 18
