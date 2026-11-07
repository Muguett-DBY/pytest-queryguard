"""BUG relationship-load: a FastAPI endpoint builds its list from lazy reads.

The endpoint queries the customers and returns a dict per customer with
the number of its orders; ``len(customer.orders)`` loads each collection
on its own.
"""

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
setup = shop.setup


def make_app(ctx: Ctx) -> FastAPI:
    app = FastAPI()

    def get_session():  # type: ignore[no-untyped-def]
        with Session(ctx.engine()) as session:
            yield session

    @app.get("/customers")
    def customers(session: Session = Depends(get_session)) -> list[dict[str, object]]:
        result = session.scalars(select(shop.Customer).order_by(shop.Customer.id))
        return [{"name": c.name, "orders": len(c.orders)} for c in result]

    return app


def run(ctx: Ctx) -> None:
    with TestClient(make_app(ctx)) as client:
        response = client.get("/customers")
    assert response.status_code == 200 and len(response.json()) == 8
