"""BUG relationship-load: an HTML fragment built in Python, like a template loop.

``"".join(f"<li>...</li>" for ...)`` is a template's ``{% for %}`` written in
Python: each list item reads ``line.product.name``, a lazy many-to-one per
line.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
setup = shop.setup


def render_lines(session: Session) -> str:
    lines = session.scalars(select(shop.OrderLine).order_by(shop.OrderLine.id))
    items = "".join(f"<li>{line.qty} x {line.product.name}</li>" for line in lines)
    return f"<ul>{items}</ul>"


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        html = render_lines(session)
    assert html.count("<li>") == 48
