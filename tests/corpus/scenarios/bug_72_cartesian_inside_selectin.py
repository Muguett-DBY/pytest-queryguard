"""BUG cartesian-load: two joined collections inside the ``selectinload`` query.

``selectinload(Author.articles).joinedload(Article.comments)`` and
``selectinload(Author.articles).joinedload(Article.tags)``: the authors come in
one SELECT, the articles in a second one, and that second SELECT joins the
comments and the tags of each article side by side. Every article comes back
once per pair of a comment and a tag.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import content
from ..kit import BUG, CARTESIAN, Ctx

VERDICT = BUG
KINDS = frozenset({CARTESIAN})
setup = content.setup


def authors_feed(session: Session) -> list[dict[str, object]]:
    articles = selectinload(content.Author.articles)
    stmt = (
        select(content.Author)
        .order_by(content.Author.id)
        .options(
            articles.joinedload(content.Article.comments),
            articles.joinedload(content.Article.tags),
        )
    )
    return [
        {
            "author": author.name,
            "articles": [
                {
                    "title": a.title,
                    "comments": len(a.comments),
                    "tags": [t.name for t in a.tags],
                }
                for a in author.articles
            ],
        }
        for author in session.scalars(stmt)
    ]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        feed = authors_feed(session)
    assert len(feed) == 4
