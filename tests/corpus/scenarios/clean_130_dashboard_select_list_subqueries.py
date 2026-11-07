"""CLEAN: six dashboard tiles whose queries differ only in a scalar subquery of the select list.

Shape from django-perf-rec #285 (fingerprints that merged different
queries). Each tile selects the customers with one correlated count or
``exists()`` in the select list: of orders, of open orders, of order lines,
of large orders, of tagged products bought, of lines with two items. The
statements have one ``FROM`` and one ``ORDER BY``, but six different
subqueries: six statements run once each, not one statement run six times.
"""

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup

C, O, L, P = shop.Customer, shop.Order, shop.OrderLine, shop.Product


def tiles() -> dict[str, object]:
    of_customer = O.customer_id == C.id
    return {
        "orders": select(func.count(O.id)).where(of_customer).scalar_subquery(),
        "open": exists().where(of_customer, O.status == "new"),
        "lines": select(func.count(L.id))
        .join(O, L.order_id == O.id)
        .where(of_customer)
        .scalar_subquery(),
        "large": exists().where(of_customer, O.total > 50),
        "pricey": exists()
        .where(of_customer, L.order_id == O.id, L.product_id == P.id)
        .where(P.price > 60),
        "doubles": select(func.max(L.qty))
        .join(O, L.order_id == O.id)
        .where(of_customer)
        .scalar_subquery(),
    }


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        board = {
            name: session.execute(select(C.name, column).order_by(C.id)).all()
            for name, column in tiles().items()
        }
    assert all(len(rows) == 8 for rows in board.values())
