"""CLEAN: a loop over loaded objects that reads columns and ``@property`` values.

Shape from orm-lens #85 (a static check that reported this). The objects of
one query are rendered from their own columns and from a Python property
computed from those columns. A property that only reads columns runs no SQL.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


class Base(DeclarativeBase):
    pass


class Owner(Base):
    __tablename__ = "owner"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    invoices: Mapped[list[Invoice]] = relationship(back_populates="owner")


class Invoice(Base):
    __tablename__ = "invoice"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id"), index=True)
    net: Mapped[int]
    vat_rate: Mapped[int]
    owner: Mapped[Owner] = relationship(back_populates="invoices")

    @property
    def gross(self) -> int:
        return self.net + self.net * self.vat_rate // 100


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Owner(id=i, name=f"owner{i}") for i in range(1, 5))
        session.flush()
        session.add_all(
            Invoice(id=i, owner_id=1 + i % 4, net=100 * i, vat_rate=23)
            for i in range(1, 21)
        )
        session.commit()


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        stmt = select(Invoice).order_by(Invoice.id)
        rows = [(i.id, i.owner_id, i.net, i.gross) for i in session.scalars(stmt)]
    assert len(rows) == 20 and rows[0] == (1, 2, 100, 123)
