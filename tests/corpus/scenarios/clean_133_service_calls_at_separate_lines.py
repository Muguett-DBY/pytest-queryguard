"""CLEAN: a service that opens a Session per call, called at separate lines.

From MLflow 2.22.0 (``tests/store/tracking/test_sqlalchemy_store.py``,
``test_set_experiment_tag``), seen by ``.github/workflows/realworld.yml``: each
method of ``SqlAlchemyStore`` opens its own Session, loads the experiment with
``_get_experiment()`` and reads its tags. The test calls the store on several
lines, for two experiments, with no loop: each call is a unit of work of its
own, and each loads one experiment.

The plugin groups the parents by the line of the query that loaded them, here
the one line of the shared helper, and by default counts the whole test as
one unit (``queryguard_scope = test``), so the two experiments look like one
query returning two rows. Telling a loop over the helper (an N+1, see
``bug_47_session_per_item``) from calls at separate lines needs the callers of
the query line, which the grouping does not use. With
``queryguard_scope = session`` each call counts on its own.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
_REASON = (
    "known limit: by default the test is one unit, and the parents are grouped "
    "by the query line of the shared helper, not by the lines that called it; "
    "queryguard_scope = session counts each call's Session on its own"
)
KNOWN = {
    config: _REASON
    for config in (
        "default",
        "small-repeat",
        "write-loop",
        "column-load-2",
        "unused-eager-load",
        "unused-columns",
        "strict",
        "all-opt-in",
    )
}
setup = shop.setup


def _get_customer(session: Session, customer_id: int) -> shop.Customer:
    return session.scalars(
        select(shop.Customer).where(shop.Customer.id == customer_id)
    ).one()


def order_count(ctx: Ctx, customer_id: int) -> int:
    with Session(ctx.engine()) as session:
        return len(_get_customer(session, customer_id).orders)


def run(ctx: Ctx) -> None:
    first = order_count(ctx, 1)
    second = order_count(ctx, 2)
    assert first > 0 and second > 0
