"""BUG relationship-load: a service loops over a lazy repository result.

The layered shape without the eager option: ``OrderRepository.open_orders``
returns plain orders and ``ReportService.summary`` reads ``order.customer``
of each one. The query and the loop are in two classes, so a check that
reads one function at a time does not see it; at runtime it is one SELECT
per customer.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
setup = shop.setup


class OrderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def open_orders(self) -> list[shop.Order]:
        stmt = select(shop.Order).where(shop.Order.status == "new")
        return list(self.session.scalars(stmt))


class ReportService:
    def __init__(self, orders: OrderRepository) -> None:
        self.orders = orders

    def summary(self) -> list[tuple[int, str]]:
        return [(o.id, o.customer.email) for o in self.orders.open_orders()]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = ReportService(OrderRepository(session)).summary()
    assert len(rows) == 24
