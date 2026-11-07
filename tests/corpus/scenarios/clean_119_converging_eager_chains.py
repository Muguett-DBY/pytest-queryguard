"""CLEAN: two ``selectinload`` chains that end at the same table.

Shape from django-nplus1 #25 (a false positive there). An order has a main
and a backup store, and each store a region. The page loads
``Order.main_store -> Store.region`` and ``Order.backup_store -> Store.region``:
the region SELECT of each chain has the same shape, two statements in all,
not one per order.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
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


class Region(Base):
    __tablename__ = "region"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))


class Store(Base):
    __tablename__ = "store"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    region_id: Mapped[int] = mapped_column(ForeignKey("region.id"))
    region: Mapped[Region] = relationship()


class Order(Base):
    __tablename__ = "order"
    id: Mapped[int] = mapped_column(primary_key=True)
    main_store_id: Mapped[int] = mapped_column(ForeignKey("store.id"))
    backup_store_id: Mapped[int] = mapped_column(ForeignKey("store.id"))
    main_store: Mapped[Store] = relationship(foreign_keys=[main_store_id])
    backup_store: Mapped[Store] = relationship(foreign_keys=[backup_store_id])


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Region(id=r, name=f"region{r}") for r in range(1, 5))
        session.flush()
        session.add_all(
            Store(id=s, name=f"store{s}", region_id=1 + s % 4) for s in range(1, 11)
        )
        session.flush()
        session.add_all(
            Order(id=o, main_store_id=1 + o % 5, backup_store_id=6 + o % 5)
            for o in range(1, 16)
        )
        session.commit()


def run(ctx: Ctx) -> None:
    stmt = (
        select(Order)
        .order_by(Order.id)
        .options(
            selectinload(Order.main_store).selectinload(Store.region),
            selectinload(Order.backup_store).selectinload(Store.region),
        )
    )
    with Session(ctx.engine()) as session:
        routing = [
            (o.id, o.main_store.region.name, o.backup_store.region.name)
            for o in session.scalars(stmt)
        ]
    assert len(routing) == 15
