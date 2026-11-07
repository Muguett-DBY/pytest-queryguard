"""CLEAN: new products get tags loaded in one query, through a two-way many-to-many.

Shape from Bullet #501 (a false positive there). The tags are loaded with
one ``IN`` query and assigned to eight new products; ``Tag.products`` is the
other side (``back_populates``), so each assignment also appends to the
tag's collection. SQLAlchemy queues those appends without loading the tags'
collections, and the flush writes the association rows: no read per product.
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, String, Table, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


class Base(DeclarativeBase):
    pass


product_tag = Table(
    "product_tag",
    Base.metadata,
    Column("product_id", ForeignKey("product.id"), primary_key=True),
    Column("tag_id", ForeignKey("tag.id"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tag"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(20))
    products: Mapped[list[Product]] = relationship(
        secondary=product_tag, back_populates="tags"
    )


class Product(Base):
    __tablename__ = "product"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    tags: Mapped[list[Tag]] = relationship(
        secondary=product_tag, back_populates="products"
    )


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        tags = [Tag(id=i, name=f"tag{i}") for i in range(1, 6)]
        session.add_all(tags)
        session.add_all(
            Product(id=i, name=f"old{i}", tags=tags[: 1 + i % 5]) for i in range(1, 6)
        )
        session.commit()


def run(ctx: Ctx) -> None:
    payload = [
        {"name": f"new{i}", "tag_ids": [1 + i % 5, 1 + (i + 2) % 5]} for i in range(8)
    ]
    with Session(ctx.engine()) as session:
        ids = {i for item in payload for i in item["tag_ids"]}
        tags = {t.id: t for t in session.scalars(select(Tag).where(Tag.id.in_(ids)))}
        for item in payload:
            session.add(
                Product(
                    name=item["name"],
                    tags=[tags[i] for i in item["tag_ids"]],
                )
            )
        session.commit()
