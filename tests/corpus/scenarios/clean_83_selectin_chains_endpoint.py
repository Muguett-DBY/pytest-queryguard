"""CLEAN: a FastAPI-style endpoint with two ``selectinload`` chains, all read.

``selectinload(Author.articles).selectinload(Article.comments)`` and
``...selectinload(Article.tags)``: one SELECT per level, no joined
collections, and the response model reads everything it loaded.
"""

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import content
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = content.setup


def make_app(ctx: Ctx) -> FastAPI:
    app = FastAPI()

    def get_session():  # type: ignore[no-untyped-def]
        with Session(ctx.engine()) as session:
            yield session

    @app.get("/authors")
    def list_authors(session: Session = Depends(get_session)) -> list[dict]:
        articles = selectinload(content.Author.articles)
        stmt = (
            select(content.Author)
            .order_by(content.Author.id)
            .options(
                articles.selectinload(content.Article.comments),
                articles.selectinload(content.Article.tags),
            )
        )
        return [
            {
                "name": author.name,
                "articles": [
                    {
                        "title": a.title,
                        "comments": [c.body for c in a.comments],
                        "tags": [t.name for t in a.tags],
                    }
                    for a in author.articles
                ],
            }
            for author in session.scalars(stmt)
        ]

    return app


def run(ctx: Ctx) -> None:
    with TestClient(make_app(ctx)) as client:
        body = client.get("/authors").json()
    assert len(body) == 4
