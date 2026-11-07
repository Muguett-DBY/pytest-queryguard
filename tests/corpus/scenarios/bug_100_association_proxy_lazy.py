"""BUG relationship-load: an ``association_proxy`` over a lazy collection, in a loop.

``Item.label_names`` proxies ``Item.labels`` (many-to-many). The catalogue
reads the proxy for every item without an eager option, so each read lazily
loads that item's labels: a SELECT per item, hidden behind a plain-looking
attribute.
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, String, Table, select
from sqlalchemy.ext.associationproxy import AssociationProxy, association_proxy
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})


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
    with Session(ctx.engine()) as session:
        stmt = select(Item).order_by(Item.id)
        catalogue = [(i.title, ", ".join(i.label_names)) for i in session.scalars(stmt)]
    assert len(catalogue) == 12
