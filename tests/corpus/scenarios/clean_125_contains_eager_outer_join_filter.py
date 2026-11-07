"""CLEAN: ``contains_eager`` over an outer join that keeps only empty collections.

Shape from Bullet #210 (a false positive there). The "customers without
orders" report outer-joins the orders, keeps the rows where no order matched
and tells the ORM with ``contains_eager`` that ``Customer.orders`` came with
the row, so the template's ``len(customer.orders)`` (always 0 here) runs no
query. The eager load is chosen on purpose and read.
"""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, contains_eager

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


def setup(ctx: Ctx) -> None:
    shop.setup(ctx)
    with Session(ctx.engine()) as session:
        dropped = select(shop.Order.id).where(shop.Order.customer_id > 3)
        session.execute(
            delete(shop.OrderLine).where(shop.OrderLine.order_id.in_(dropped))
        )
        session.execute(delete(shop.Order).where(shop.Order.customer_id > 3))
        session.commit()


def run(ctx: Ctx) -> None:
    stmt = (
        select(shop.Customer)
        .outerjoin(shop.Customer.orders)
        .where(shop.Order.id.is_(None))
        .order_by(shop.Customer.id)
        .options(contains_eager(shop.Customer.orders))
    )
    with Session(ctx.engine()) as session:
        idle = [(c.name, len(c.orders)) for c in session.scalars(stmt).unique()]
    assert [n for _, n in idle] == [0] * 5
