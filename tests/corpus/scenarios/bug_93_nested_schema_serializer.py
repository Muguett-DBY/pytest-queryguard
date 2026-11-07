"""BUG relationship-load and unused-columns: a nested schema reads comments per article.

``ArticleSchema`` declares a nested ``comments`` field; dumping a list of
articles reads ``article.comments`` for each one, one SELECT per article.
The query that loaded the articles has no loader option, and the schema
dumps three of the article's twelve columns.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import content
from ..domains.content import Article
from ..kit import BUG, COLUMNS, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL, COLUMNS})
PATCH = True
setup = content.setup


class Schema:
    fields: tuple[str, ...] = ()
    nested: dict[str, "Schema"] = {}

    def dump(self, obj: Any) -> dict[str, Any]:
        out = {name: getattr(obj, name) for name in self.fields}
        for name, schema in self.nested.items():
            out[name] = [schema.dump(child) for child in getattr(obj, name)]
        return out


class CommentSchema(Schema):
    fields = ("id", "author_name", "body")


class ArticleSchema(Schema):
    fields = ("id", "title", "slug")
    nested = {"comments": CommentSchema()}


def list_articles(session: Session) -> list[dict[str, Any]]:
    articles = session.scalars(select(Article).order_by(Article.id))
    schema = ArticleSchema()
    return [schema.dump(a) for a in articles]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        data = list_articles(session)
    assert len(data) == 20
