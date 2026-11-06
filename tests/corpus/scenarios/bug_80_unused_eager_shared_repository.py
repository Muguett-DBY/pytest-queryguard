"""BUG unused-eager-load: a repository method shared by a page that needs less.

``CustomerRepository.list_with_orders()`` was written for the customer page;
the newsletter export reuses it and reads only names and e-mail addresses,
so the ``selectinload`` of the orders is a query for nothing.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domains import shop
from ..kit import BUG, UNUSED, Ctx

VERDICT = BUG
KINDS = frozenset({UNUSED})
setup = shop.setup


class CustomerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_with_orders(self) -> list[shop.Customer]:
        stmt = (
            select(shop.Customer)
            .order_by(shop.Customer.id)
            .options(selectinload(shop.Customer.orders))
        )
        return list(self.session.scalars(stmt))


def newsletter_recipients(repo: CustomerRepository) -> list[str]:
    return [f"{c.name} <{c.email}>" for c in repo.list_with_orders()]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        recipients = newsletter_recipients(CustomerRepository(session))
    assert len(recipients) == 8
