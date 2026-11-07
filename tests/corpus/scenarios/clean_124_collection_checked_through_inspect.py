"""CLEAN: an eager-loaded collection read through ``inspect(obj).attrs``.

Shape from Bullet #642 (a false positive there). A serializer helper counts
a collection only when it is already loaded, reading
``inspect(author).attrs.articles.loaded_value`` so that it never triggers a
lazy load. The page loads the articles with ``selectinload``, so the helper
uses them for every author: the eager load is read.
"""

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.base import NO_VALUE

from ..domains import content
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
KNOWN = {
    "unused-eager-load": (
        "a read through inspect(obj).attrs.x.loaded_value does not go through "
        "the attribute's __get__, so the check does not see it (a documented "
        "limitation, like __dict__)"
    ),
}
setup = content.setup


def article_count(author: content.Author) -> int | None:
    loaded = inspect(author).attrs.articles.loaded_value
    return None if loaded is NO_VALUE else len(loaded)


def run(ctx: Ctx) -> None:
    stmt = (
        select(content.Author)
        .order_by(content.Author.id)
        .options(selectinload(content.Author.articles))
    )
    with Session(ctx.engine()) as session:
        counts = {a.name: article_count(a) for a in session.scalars(stmt)}
    assert None not in counts.values()
