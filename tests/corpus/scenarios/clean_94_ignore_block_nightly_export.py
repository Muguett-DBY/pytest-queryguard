"""CLEAN: an accepted N+1 inside an ``ignore()`` block with its reason.

The nightly export runs over a few hundred rows once a day and the team
chose to keep it simple: the loop is wrapped in
``ignore("relationship-load", reason=...)``, every lazy load inside it. A
tool should respect that choice, also in strict mode.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from pytest_queryguard import ignore

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


def nightly_export(session: Session) -> list[str]:
    with ignore("relationship-load", reason="nightly, a few hundred rows"):
        return [
            f"{o.id};{o.customer.email};{o.total}"
            for o in session.scalars(select(shop.Order))
        ]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = nightly_export(session)
    assert len(rows) == 24
