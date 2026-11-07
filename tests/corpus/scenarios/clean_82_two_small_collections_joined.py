"""CLEAN: two collections joined side by side that hold one or two rows each.

A user and their roles and verified e-mail addresses, for the session of a
login: the product is at most 2 x 2 rows per user, and one round trip is
the point. A reviewer would not ask for a ``selectinload`` here. The
cartesian-load rule reads loader strategies, not row counts, so it warns.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    joinedload,
    mapped_column,
    relationship,
)

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
KNOWN = {
    "*": "the cartesian-load rule reads the loader strategies, not how many rows "
    "the collections hold; two joined collections are reported whatever their size",
}


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "user_account"
    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(40))
    roles: Mapped[list[Role]] = relationship()
    emails: Mapped[list[Email]] = relationship()


class Role(Base):
    __tablename__ = "role"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_account.id"))
    name: Mapped[str] = mapped_column(String(20))


class Email(Base):
    __tablename__ = "email"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_account.id"))
    address: Mapped[str] = mapped_column(String(80))


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add(User(id=1, login="ann"))
        session.add_all(
            [Role(id=1, user_id=1, name="admin"), Role(id=2, user_id=1, name="staff")]
        )
        session.add_all(
            [
                Email(id=1, user_id=1, address="ann@example.com"),
                Email(id=2, user_id=1, address="ann@work.example"),
            ]
        )
        session.commit()


def load_principal(session: Session, login: str) -> tuple[set[str], list[str]]:
    stmt = (
        select(User)
        .where(User.login == login)
        .options(joinedload(User.roles), joinedload(User.emails))
    )
    user = session.scalars(stmt).unique().one()
    return {r.name for r in user.roles}, [e.address for e in user.emails]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        roles, emails = load_principal(session, "ann")
    assert roles == {"admin", "staff"} and len(emails) == 2
