"""BUG repeated-statement: one product lookup reached through two helpers in turn.

Shape from Prosopite #14 and #68 (grouping by call stack split one loop in
two). An invoice builder formats even lines with ``price_line`` and odd ones
with ``discount_line``; both call ``product_by_id``. The same SELECT runs
once per line from two different stacks: still one query per line, which an
``IN`` lookup before the loop would replace.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REPEAT, Ctx

VERDICT = BUG
KINDS = frozenset({REPEAT})
setup = shop.setup


def product_by_id(session: Session, product_id: int) -> shop.Product:
    return session.scalars(
        select(shop.Product).where(shop.Product.id == product_id)
    ).one()


def price_line(session: Session, product_id: int, qty: int) -> str:
    product = product_by_id(session, product_id)
    return f"{product.name} x{qty}: {product.price * qty}"


def discount_line(session: Session, product_id: int, qty: int) -> str:
    product = product_by_id(session, product_id)
    return f"{product.name} x{qty}: {product.price * qty * 9 // 10} (-10%)"


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = session.execute(
            select(shop.OrderLine.product_id, shop.OrderLine.qty)
            .where(shop.OrderLine.order_id <= 4)
            .order_by(shop.OrderLine.id)
        ).all()
        invoice = [
            (price_line if n % 2 == 0 else discount_line)(session, pid, qty)
            for n, (pid, qty) in enumerate(rows)
        ]
    assert len(invoice) == 8
