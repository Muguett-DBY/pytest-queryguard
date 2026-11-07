"""CLEAN: a loop over the mapped classes, not over loaded objects.

Shape from orm-lens #72 (a static check that reported this). A retention
job walks ``Base.registry.mappers`` and reads class-level attributes of each
mapped class (its table name, a ``RETENTION`` setting) and counts the rows
of each table with one aggregate. The loop runs once per class, not once per
row; nothing is lazy-loaded.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup

RETENTION_DAYS = {"order": 365, "order_line": 365}


def retention_report(session: Session) -> dict[str, tuple[int, int]]:
    report = {}
    for mapper in shop.Base.registry.mappers:
        cls = mapper.class_
        days = RETENTION_DAYS.get(cls.__tablename__)
        if days is None:
            continue
        count = session.scalar(select(func.count()).select_from(cls))
        report[cls.__tablename__] = (days, count or 0)
    return report


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        report = retention_report(session)
    assert report == {"order": (365, 24), "order_line": (365, 48)}
