"""BUG relationship-load: the parents come from a fixture, the loop is in the test.

The fixture opens a Session and loads the customers (the common
"``customers`` fixture"); the code under test walks ``customer.orders`` of
each one. The parent query ran before the test body, but the lazy loads,
one per customer, run in it.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})

STATE: dict[str, object] = {}


def setup(ctx: Ctx) -> None:
    shop.setup(ctx)
    session = Session(ctx.engine())
    STATE["session"] = session
    STATE["customers"] = session.scalars(select(shop.Customer)).all()


def totals(customers: list[shop.Customer]) -> list[int]:
    return [sum(o.total for o in c.orders) for c in customers]


def run(ctx: Ctx) -> None:
    session = STATE.pop("session")
    try:
        result = totals(STATE.pop("customers"))  # type: ignore[arg-type]
    finally:
        session.close()  # type: ignore[attr-defined]
    assert len(result) == 8
