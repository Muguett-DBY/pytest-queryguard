"""CLEAN: a ``Session`` subclass whose ``execute`` override every query goes through.

From SQLModel 0.0.24 (``sqlmodel/orm/session.py``), seen by
``.github/workflows/realworld.yml``: SQLModel's ``Session`` overrides
``execute`` and adds ``exec``, both calling ``super().execute()``. In
SQLModel's own suite the package is not in ``site-packages`` (an editable
install), and every finding in its tutorials was located at that
``return super().execute(...)`` line instead of the tutorial's. The same holds
for any application that subclasses ``Session`` in its own code.

The line of a query is also how the relationship-load rule groups parents:
here three customers are looked up at three lines, through ``get()``,
``exec()`` and ``execute()``, each reading its orders. No loop over rows (see
``clean_93_two_single_object_lookups``), but with the override as the line of
every query the three looked like one query returning three rows.
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
        second = session.exec(select(shop.Customer).where(shop.Customer.id == 2)).one()
        third = session.execute(
            select(shop.Customer).where(shop.Customer.id == 3)
        ).scalar_one()
        assert first is not None
        counts = [len(first.orders), len(second.orders), len(third.orders)]
    assert all(count > 0 for count in counts)
