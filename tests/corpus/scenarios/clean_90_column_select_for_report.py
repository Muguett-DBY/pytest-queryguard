"""CLEAN: a report selects the columns it needs, not the entity.

``select(Article.id, Article.title, Article.views)`` returns rows of three
values; no ORM instance is loaded, so no column is loaded for nothing.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import content
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = content.setup


def run(ctx: Ctx) -> None:
    stmt = select(content.Article.id, content.Article.title, content.Article.views)
    with Session(ctx.engine()) as session:
        report = [f"{row.id};{row.title};{row.views}" for row in session.execute(stmt)]
    assert len(report) == 20
