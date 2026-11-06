"""BUG relationship-load and repeated-statement: a report with two loops.

A sales report first walks the customers' orders lazily, then looks up each
product with ``session.get`` by id. Both loops are N+1s. In strict mode the
first one raises where it happens, so the second loop never runs.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..domains.shop import Customer, Product
from ..kit import BUG, REL, REPEAT, Ctx

VERDICT = BUG
KINDS = frozenset({REL, REPEAT})
PATCH = True
KNOWN = {
    "strict": "strict mode raises at the first N+1, so the code after it, the "
    "second loop, never runs and is not reported",
}
setup = shop.setup


def sales_report(session: Session) -> dict[str, object]:
    customers = session.scalars(select(Customer).order_by(Customer.id)).all()
    per_customer = {c.name: sum(o.total for o in c.orders) for c in customers}
    names = [session.get(Product, pid).name for pid in range(1, 13)]  # type: ignore[union-attr]
    return {"revenue": per_customer, "products": names}


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        report = sales_report(session)
    assert len(report["products"]) == 12  # type: ignore[arg-type]
