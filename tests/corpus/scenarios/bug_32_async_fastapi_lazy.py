"""BUG relationship-load: an async FastAPI endpoint with ``awaitable_attrs`` in a loop."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
setup = shop.setup


def make_app(ctx: Ctx) -> FastAPI:
    app = FastAPI()
    # A pool-less engine: the TestClient runs the app in its own event loop.
    engine = create_async_engine(ctx.async_url, poolclass=NullPool)

    @app.get("/customers")
    async def customers() -> list[dict[str, object]]:
        async with AsyncSession(engine) as session:
            rows = (await session.scalars(select(shop.Customer))).all()
            return [
                {"name": c.name, "orders": len(await c.awaitable_attrs.orders)}
                for c in rows
            ]

    return app


def run(ctx: Ctx) -> None:
    with TestClient(make_app(ctx)) as client:
        response = client.get("/customers")
    assert response.status_code == 200 and len(response.json()) == 8
