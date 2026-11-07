"""CLEAN: one ``populate_existing`` query refreshes the whole list after a bulk ``UPDATE``.

A job marks a customer's orders shipped with an ORM bulk ``UPDATE`` that
skips the session synchronization, then re-selects the same orders once with
``execution_options(populate_existing=True)``, which overwrites the objects
in the identity map. Two statements for nine orders.
"""

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


def run(ctx: Ctx) -> None:
    mine = shop.Order.customer_id <= 3
    with Session(ctx.engine()) as session:
        orders = session.scalars(select(shop.Order).where(mine)).all()
        session.execute(
            update(shop.Order)
            .where(mine)
            .values(status="shipped")
            .execution_options(synchronize_session=False)
        )
        session.scalars(
            select(shop.Order).where(mine).execution_options(populate_existing=True)
        ).all()
        assert {o.status for o in orders} == {"shipped"}
