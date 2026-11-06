"""BUG relationship-load: a comment thread renders replies lazily, level by level.

The thread view loads the top-level comments of an article and renders each
one's ``replies`` recursively; every comment loads its replies with its own
SELECT.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import content
from ..domains.content import Comment
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})


def setup(ctx: Ctx) -> None:
    content.setup(ctx, authors=1, articles=2, comments=6, replies=2)


def render(comment: Comment, depth: int = 0) -> list[str]:
    lines = ["  " * depth + comment.body]
    for reply in comment.replies:
        lines.extend(render(reply, depth + 1))
    return lines


def thread(session: Session, article_id: int) -> list[str]:
    stmt = select(Comment).where(
        Comment.article_id == article_id, Comment.parent_id.is_(None)
    )
    out: list[str] = []
    for top in session.scalars(stmt):
        out.extend(render(top))
    return out


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        lines = thread(session, 1)
    assert len(lines) == 18
