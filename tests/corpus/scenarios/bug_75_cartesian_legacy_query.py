"""BUG cartesian-load and unused-columns: legacy ``session.query()``, two joined collections.

Older Flask-era code: ``session.query(Article).options(joinedload(...),
joinedload(...))``. The comments and the attachments of each article are
joined side by side, so the rows grow as their product. The cards show the
title only, of twelve loaded columns.
"""

from sqlalchemy.orm import Session, joinedload

from ..domains import content
from ..kit import BUG, CARTESIAN, COLUMNS, Ctx

VERDICT = BUG
KINDS = frozenset({CARTESIAN, COLUMNS})
setup = content.setup


def article_cards(session: Session) -> list[tuple[str, int, int]]:
    articles = (
        session.query(content.Article)
        .options(
            joinedload(content.Article.comments),
            joinedload(content.Article.attachments),
        )
        .filter(content.Article.status == "published")
        .all()
    )
    return [(a.title, len(a.comments), len(a.attachments)) for a in articles]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        cards = article_cards(session)
    assert len(cards) == 15
