"""BUG relationship-load: a self-referential many-to-many read for every user.

``User.following`` joins ``user`` to itself through the ``follow`` table
(``secondary`` with ``primaryjoin`` and ``secondaryjoin``). The "who follows
whom" page reads it lazily for each user: a SELECT per user.
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, String, Table, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)

from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})


class Base(DeclarativeBase):
    pass


follow = Table(
    "follow",
    Base.metadata,
    Column("follower_id", ForeignKey("user.id"), primary_key=True),
    Column("followed_id", ForeignKey("user.id"), primary_key=True),
)


class User(Base):
    __tablename__ = "user"
    id: Mapped[int] = mapped_column(primary_key=True)
    handle: Mapped[str] = mapped_column(String(40))
    following: Mapped[list[User]] = relationship(
        secondary=follow,
        primaryjoin=lambda: User.id == follow.c.follower_id,
        secondaryjoin=lambda: User.id == follow.c.followed_id,
    )


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        users = [User(id=i, handle=f"user{i}") for i in range(1, 11)]
        for i, user in enumerate(users):
            user.following = [users[(i + k) % 10] for k in (1, 3)]
        session.add_all(users)
        session.commit()


def run(ctx: Ctx) -> None:
    stmt = select(User).order_by(User.id)
    with Session(ctx.engine()) as session:
        graph = {
            u.handle: sorted(f.handle for f in u.following)
            for u in session.scalars(stmt)
        }
    assert all(len(v) == 2 for v in graph.values())
