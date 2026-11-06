"""BUG relationship-load: a ``lazy="select"`` collection read per parent.

The same mapping as the refresh scenarios, plus a second collection with the
default loading. The refreshes with the selectin collection are fine; reading
the lazy one for every parent is the N+1.
"""

from sqlalchemy import ForeignKey, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True


class Base(DeclarativeBase):
    pass


class Parent(Base):
    __tablename__ = "parent"
    id: Mapped[int] = mapped_column(primary_key=True)
    children: Mapped[list["Child"]] = relationship(lazy="selectin")
    notes: Mapped[list["Note"]] = relationship()


class Child(Base):
    __tablename__ = "child"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("parent.id"))


class Note(Base):
    __tablename__ = "note"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("parent.id"))


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Parent(id=i, notes=[Note(), Note()]) for i in range(1, 9))
        session.commit()


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        parents = session.scalars(select(Parent)).all()
        new = Parent(id=101)
        session.add(new)
        session.flush()
        session.refresh(new, ["children"])
        counts = [len(p.notes) for p in parents]
    assert counts == [2] * 8
