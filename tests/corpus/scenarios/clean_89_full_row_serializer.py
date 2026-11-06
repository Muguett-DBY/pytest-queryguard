"""CLEAN: a ``to_dict`` serializer that returns every column of the row.

The admin API's generic serializer walks the mapper's columns; every loaded
column is read, so nothing is loaded for nothing.
"""

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from ..domains import content
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = content.setup


def to_dict(obj: object) -> dict[str, object]:
    mapper = inspect(type(obj))
    return {attr.key: getattr(obj, attr.key) for attr in mapper.column_attrs}


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        rows = [to_dict(a) for a in session.scalars(select(content.Article))]
    assert len(rows) == 20 and len(rows[0]) == 12
