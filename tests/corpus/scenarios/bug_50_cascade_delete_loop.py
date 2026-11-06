"""BUG relationship-load and write-loop: deleting parents one by one with an ORM delete cascade.

``cascade="all, delete-orphan"`` makes ``session.delete(parent)`` load the
children first, one SELECT per parent (and that load autoflushes the previous
delete, so the DELETEs go out folder by folder). ``passive_deletes=True`` with an
``ON DELETE CASCADE`` foreign key, or a bulk ``DELETE``, avoids it.
"""

from sqlalchemy import ForeignKey, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..kit import BUG, REL, WRITE, Ctx

VERDICT = BUG
KINDS = frozenset({REL, WRITE})
PATCH = True


class Base(DeclarativeBase):
    pass


class Folder(Base):
    __tablename__ = "folder"
    id: Mapped[int] = mapped_column(primary_key=True)
    files: Mapped[list["File"]] = relationship(cascade="all, delete-orphan")


class File(Base):
    __tablename__ = "file"
    id: Mapped[int] = mapped_column(primary_key=True)
    folder_id: Mapped[int] = mapped_column(ForeignKey("folder.id"))


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Folder(id=i, files=[File(), File()]) for i in range(1, 9))
        session.commit()


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        for folder in session.scalars(select(Folder)).all():
            session.delete(folder)
        session.commit()
