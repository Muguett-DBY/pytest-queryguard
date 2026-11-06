"""BUG unused-columns: a three-field Pydantic summary of a twelve-column row.

``ArticleSummary.model_validate(article)`` reads ``id``, ``title`` and
``views``; the query loaded all twelve columns of every article.
"""

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import content
from ..kit import BUG, COLUMNS, Ctx

VERDICT = BUG
KINDS = frozenset({COLUMNS})
setup = content.setup


class ArticleSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    views: int


def most_read(session: Session) -> list[ArticleSummary]:
    stmt = select(content.Article).order_by(content.Article.views.desc()).limit(10)
    return [ArticleSummary.model_validate(a) for a in session.scalars(stmt)]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        top = most_read(session)
    assert top[0].id == 20
