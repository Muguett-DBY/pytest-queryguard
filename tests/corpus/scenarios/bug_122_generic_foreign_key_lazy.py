"""BUG relationship-load: a "generic foreign key" relationship read for every comment.

Shape from django-zeal #66 (generic relations). SQLAlchemy's generic
association recipe: a ``Comment`` has ``target_type`` and ``target_id``, and
``Comment.post`` and ``Comment.photo`` are ``viewonly`` relationships with a
``primaryjoin`` on both. The moderation queue reads the target of every
comment, a lazy load per comment.
"""

from __future__ import annotations

from sqlalchemy import String, and_, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    foreign,
    mapped_column,
    relationship,
)

from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})


class Base(DeclarativeBase):
    pass


class Post(Base):
    __tablename__ = "post"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(40))


class Photo(Base):
    __tablename__ = "photo"
    id: Mapped[int] = mapped_column(primary_key=True)
    caption: Mapped[str] = mapped_column(String(40))


class Comment(Base):
    __tablename__ = "comment"
    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str] = mapped_column(String(80))
    target_type: Mapped[str] = mapped_column(String(10))
    target_id: Mapped[int]
    post: Mapped[Post | None] = relationship(
        primaryjoin=lambda: and_(
            foreign(Comment.target_id) == Post.id, Comment.target_type == "post"
        ),
        viewonly=True,
    )
    photo: Mapped[Photo | None] = relationship(
        primaryjoin=lambda: and_(
            foreign(Comment.target_id) == Photo.id, Comment.target_type == "photo"
        ),
        viewonly=True,
    )

    @property
    def target_name(self) -> str:
        if self.target_type == "post":
            assert self.post is not None
            return self.post.title
        assert self.photo is not None
        return self.photo.caption


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Post(id=i, title=f"post{i}") for i in range(1, 6))
        session.add_all(Photo(id=i, caption=f"photo{i}") for i in range(1, 6))
        session.add_all(
            Comment(
                id=i,
                body=f"comment{i}",
                target_type="post" if i % 2 else "photo",
                target_id=1 + i % 5,
            )
            for i in range(1, 17)
        )
        session.commit()


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        stmt = select(Comment).order_by(Comment.id)
        queue = [(c.body, c.target_name) for c in session.scalars(stmt)]
    assert len(queue) == 16
