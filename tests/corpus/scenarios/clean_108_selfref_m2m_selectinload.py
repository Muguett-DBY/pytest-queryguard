"""CLEAN: a self-referential many-to-many loaded with ``selectinload``.

``User.following`` joins ``user`` to itself through ``follow``. The page asks
for it with ``selectinload``: one SELECT through the secondary table for all
users. The followed users are rows of the same list, already in the identity
map.
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, String, Table, select
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
    stmt = select(User).order_by(User.id).options(selectinload(User.following))
    with Session(ctx.engine()) as session:
        graph = {
            u.handle: sorted(f.handle for f in u.following)
            for u in session.scalars(stmt)
        }
    assert all(len(v) == 2 for v in graph.values())
