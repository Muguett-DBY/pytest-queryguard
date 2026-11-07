"""CLEAN: a service loops over what an eager-loading repository returns.

The common layered shape: ``OrderRepository.open_orders`` builds the query
with ``joinedload(Order.customer)`` and ``selectinload(Order.lines)``, and
``ReportService.summary`` loops over the result reading both. The loading
decision sits in the repository, the reads in the service: two statements.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


class OrderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def open_orders(self) -> list[shop.Order]:
        stmt = (
            select(shop.Order)
            .where(shop.Order.status == "new")
            .options(joinedload(shop.Order.customer), selectinload(shop.Order.lines))
        )
        return list(self.session.scalars(stmt).unique())


class ReportService:
    def __init__(self, orders: OrderRepository) -> None:
        self.orders = orders

    def summary(self) -> list[tuple[str, int]]:
        return [(o.customer.email, len(o.lines)) for o in self.orders.open_orders()]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = ReportService(OrderRepository(session)).summary()
    assert len(rows) == 24
