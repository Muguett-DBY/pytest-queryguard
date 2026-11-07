"""BUG repeated-statement: a home-made "attach related" helper called per object.

Shape from Prosopite #62 (preloader-like helpers). ``attach_orders(session,
customer)`` queries a customer's orders and stores them with
``set_committed_value`` so that the template reads ``customer.orders``
without a lazy load. Called in a loop, it is still one SELECT per customer;
it hides the N+1 from the relationship rule, not from the database.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ..domains import shop
from ..kit import BUG, REPEAT, Ctx

VERDICT = BUG
KINDS = frozenset({REPEAT})
setup = shop.setup


def attach_orders(session: Session, customer: shop.Customer) -> None:
    orders = session.scalars(
        select(shop.Order).where(shop.Order.customer_id == customer.id)
    ).all()
    set_committed_value(customer, "orders", list(orders))


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        customers = session.scalars(select(shop.Customer)).all()
        for customer in customers:
            attach_orders(session, customer)
        page = [(c.name, len(c.orders)) for c in customers]
    assert all(n == 3 for _, n in page)
