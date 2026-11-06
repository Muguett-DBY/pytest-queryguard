"""BUG cartesian-load: an author profile endpoint joins two collections.

``joinedload(Author.articles)`` and ``joinedload(Author.links)`` in one query:
the SELECT returns one row per pair of an article and a link, so 20 articles
and 10 links are 200 rows for one author, folded back into 30 objects. The
shape of a FastAPI or Flask "detail" endpoint that wants everything on the
page in one round trip.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..domains import content
from ..kit import BUG, CARTESIAN, Ctx

VERDICT = BUG
KINDS = frozenset({CARTESIAN})


def setup(ctx: Ctx) -> None:
    content.setup(ctx, authors=2, articles=20, links=10, comments=0, attachments=0)


def author_profile(session: Session, author_id: int) -> dict[str, object]:
    stmt = (
        select(content.Author)
        .where(content.Author.id == author_id)
        .options(joinedload(content.Author.articles), joinedload(content.Author.links))
    )
    author = session.scalars(stmt).unique().one()
    return {
        "name": author.name,
        "articles": [a.title for a in author.articles],
        "links": [link.url for link in author.links],
    }


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        page = author_profile(session, 1)
    assert len(page["articles"]) == 20  # type: ignore[arg-type]
