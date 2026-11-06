"""BUG relationship-load: a ``response_model`` serializes lazy relationships.

The endpoint returns ORM orders; FastAPI validates them into ``OrderOut`` with
``from_attributes``, which reads ``customer`` and ``lines`` of each order after
the endpoint code has finished. No frame of the application is on the stack, so
the finding has no call site.
"""

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
KNOWN = {
    "strict": "Pydantic catches the strict NPlusOneDetected and raises its own "
    "validation error holding its message; the test fails with that error, so "
    "the report at the end, which records the finding, does not run",
}
setup = shop.setup


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    customer: CustomerOut


def make_app(ctx: Ctx) -> FastAPI:
    app = FastAPI()

    def get_session():  # type: ignore[no-untyped-def]
        with Session(ctx.engine()) as session:
            yield session

    @app.get("/orders", response_model=list[OrderOut])
    def orders(session: Session = Depends(get_session)) -> list[shop.Order]:
        return list(session.scalars(select(shop.Order)))

    return app


def run(ctx: Ctx) -> None:
    with TestClient(make_app(ctx)) as client:
        response = client.get("/orders")
    assert response.status_code == 200 and len(response.json()) == 24
