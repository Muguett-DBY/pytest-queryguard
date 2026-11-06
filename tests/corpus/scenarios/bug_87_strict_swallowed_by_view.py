"""BUG relationship-load: a view that turns every error into an empty response.

The handler wraps its body in ``except Exception`` and logs, a common
"never 500" pattern. The loop lazy-loads ``order.customer`` per order. In
strict mode the ``NPlusOneDetected`` raised at the access is caught by that
handler; the report at the end of the test must still fail it.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..domains.shop import Order
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
setup = shop.setup

log = logging.getLogger(__name__)


def recent_orders(session: Session) -> list[dict[str, object]]:
    try:
        orders = session.scalars(select(Order).order_by(Order.id)).all()
        return [{"id": o.id, "customer": o.customer.name} for o in orders]
    except Exception:
        log.exception("recent orders failed")
        return []


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        recent_orders(session)
