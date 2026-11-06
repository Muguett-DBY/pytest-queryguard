"""BUG repeated-statement: a Celery-style task enqueued once per order id.

``send_receipt.delay(order_id)`` for every order of a batch; in tests the tasks
run eagerly (``task_always_eager``), so each one opens a session and loads its
order. One task that takes the list of ids would load them together.
"""

from collections.abc import Callable

from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REPEAT, Ctx

VERDICT = BUG
KINDS = frozenset({REPEAT})
KNOWN = {
    "session-scope": "by design: with queryguard_scope = session each Session is counted on its own, and each task runs one lookup"
}
setup = shop.setup


class EagerTask:
    """The part of a Celery task that matters here: ``delay`` runs it now."""

    def __init__(self, fn: Callable[..., object]) -> None:
        self.fn = fn

    def delay(self, *args: object) -> object:
        return self.fn(*args)


def run(ctx: Ctx) -> None:
    sent: list[str] = []

    @EagerTask
    def send_receipt(order_id: int) -> None:
        with Session(ctx.engine()) as session:
            order = session.get(shop.Order, order_id)
            sent.append(f"receipt for order {order.id}: {order.total}")

    for order_id in range(1, 9):
        send_receipt.delay(order_id)
    assert len(sent) == 8
