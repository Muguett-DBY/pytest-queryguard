"""CLEAN: a ``Session`` subclass whose ``execute`` override every query goes through.

From SQLModel 0.0.24 (``sqlmodel/orm/session.py``, its tutorial
``docs_src/tutorial/many_to_many/tutorial001.py``), seen by
``.github/workflows/realworld.yml``: SQLModel's ``Session`` overrides
``execute`` and adds ``exec``, both calling ``super().execute()``. The tutorial
refreshes three new heroes, one per line, and prints each hero's teams. Three
lazy loads of ``Hero.teams``, but each for the one parent a line of its own
refreshed: no loop over rows (see ``clean_93_two_single_object_lookups``).

In SQLModel's own test suite its package is not in ``site-packages`` (an
editable install), and the same holds for any application that subclasses
``Session`` in its own code. The plugin took the override's
``return super().execute(...)`` line for the line that loaded each parent, so
the three refreshes looked like one query line returning three rows.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


class AppSession(Session):
    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        return super().execute(statement, *args, **kwargs)

    def exec(self, statement: Any) -> Any:
        return super().execute(statement).scalars()


def run(ctx: Ctx) -> None:
    with AppSession(ctx.engine()) as session:
        first = session.get(shop.Customer, 1)
        second = session.get(shop.Customer, 2)
        third = session.exec(select(shop.Customer).where(shop.Customer.id == 3)).one()
        assert first is not None and second is not None
        session.expire_all()
        session.refresh(first)
        session.refresh(second)
        session.refresh(third)
        counts = [len(first.orders), len(second.orders), len(third.orders)]
    assert all(count > 0 for count in counts)
