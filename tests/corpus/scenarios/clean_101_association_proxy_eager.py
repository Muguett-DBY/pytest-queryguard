"""CLEAN: an ``association_proxy`` reads the eager-loaded collection.

``Product.tag_names`` proxies ``Product.tags``; the catalogue loads the tags
with ``selectinload`` and shows ``product.tag_names``. The proxy reads the
relationship through the ORM attribute, so the eager load is used and there
is no query per product.
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, String, Table, select
from sqlalchemy.ext.associationproxy import AssociationProxy, association_proxy
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    selectinload,
)

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


class Base(DeclarativeBase):
    pass


item_label = Table(
    "item_label",
    Base.metadata,
    Column("item_id", ForeignKey("item.id"), primary_key=True),
    Column("label_id", ForeignKey("label.id"), primary_key=True),
)


class Label(Base):
    __tablename__ = "label"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(20))


class Item(Base):
    __tablename__ = "item"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(40))
    labels: Mapped[list[Label]] = relationship(secondary=item_label)
    label_names: AssociationProxy[list[str]] = association_proxy("labels", "name")


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        labels = [Label(id=i, name=f"l{i}") for i in range(1, 6)]
        session.add_all(
            Item(id=i, title=f"item{i}", labels=labels[: 1 + i % 5])
            for i in range(1, 13)
        )
        session.commit()


def run(ctx: Ctx) -> None:
    stmt = select(Item).order_by(Item.id).options(selectinload(Item.labels))
    with Session(ctx.engine()) as session:
        catalogue = [(i.title, list(i.label_names)) for i in session.scalars(stmt)]
    assert len(catalogue) == 12
