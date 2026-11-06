"""A small publishing site: authors, wide articles, comments, tags, attachments.

``Article`` is the wide row of a typical CMS: twelve columns, among them a
large ``body`` and a JSON-ish ``meta`` text, which is what the unused-columns
check is about. Authors and articles each have two collections side by side
(``Author.articles`` and ``Author.links``; ``Article.comments``,
``Article.tags`` and ``Article.attachments``), which is what the cartesian-load
check is about. Comments form a tree (``Comment.replies``). Every
relationship loads lazily (``select``).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, String, Table, Text
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)

from ..kit import Ctx


class Base(AsyncAttrs, DeclarativeBase):
    pass


article_tag = Table(
    "article_tag",
    Base.metadata,
    Column("article_id", ForeignKey("article.id"), primary_key=True),
    Column("tag_id", ForeignKey("tag.id"), primary_key=True),
)


class Author(Base):
    __tablename__ = "author"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    bio: Mapped[str] = mapped_column(Text, default="")
    articles: Mapped[list[Article]] = relationship(back_populates="author")
    links: Mapped[list[SocialLink]] = relationship(back_populates="author")


class SocialLink(Base):
    __tablename__ = "social_link"
    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"), index=True)
    url: Mapped[str] = mapped_column(String(200))
    author: Mapped[Author] = relationship(back_populates="links")


class Tag(Base):
    __tablename__ = "tag"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))


class Article(Base):
    __tablename__ = "article"
    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    language: Mapped[str] = mapped_column(String(8), default="en")
    views: Mapped[int] = mapped_column(default=0)
    word_count: Mapped[int] = mapped_column(default=0)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    author: Mapped[Author] = relationship(back_populates="articles")
    comments: Mapped[list[Comment]] = relationship(back_populates="article")
    tags: Mapped[list[Tag]] = relationship(secondary=article_tag)
    attachments: Mapped[list[Attachment]] = relationship(back_populates="article")


class Comment(Base):
    __tablename__ = "comment"
    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("article.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("comment.id"), index=True)
    author_name: Mapped[str] = mapped_column(String(60))
    body: Mapped[str] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(default=True)
    article: Mapped[Article] = relationship(back_populates="comments")
    replies: Mapped[list[Comment]] = relationship(back_populates="parent")
    parent: Mapped[Comment | None] = relationship(
        back_populates="replies", remote_side=[id]
    )


class Attachment(Base):
    __tablename__ = "attachment"
    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("article.id"), index=True)
    filename: Mapped[str] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(default=0)
    article: Mapped[Article] = relationship(back_populates="attachments")


def seed(
    session: Session,
    *,
    authors: int = 4,
    articles: int = 5,
    comments: int = 4,
    replies: int = 1,
    tags: int = 6,
    attachments: int = 3,
    links: int = 3,
) -> None:
    """Deterministic rows: ids start at 1 and are dense."""
    tag_rows = [Tag(id=i, name=f"tag{i}") for i in range(1, tags + 1)]
    session.add_all(tag_rows)
    article_id = comment_id = attachment_id = link_id = 0
    for a in range(1, authors + 1):
        author = Author(id=a, name=f"author{a}", bio="x" * 200)
        session.add(author)
        for _ in range(links):
            link_id += 1
            session.add(
                SocialLink(id=link_id, author=author, url=f"https://e.org/{link_id}")
            )
        for _ in range(articles):
            article_id += 1
            article = Article(
                id=article_id,
                author=author,
                title=f"Article {article_id}",
                slug=f"article-{article_id}",
                summary="s" * 100,
                body="b" * 2000,
                meta='{"seo": true}',
                status="published" if article_id % 4 else "draft",
                views=article_id * 10,
                word_count=300 + article_id,
                published_at=datetime(
                    2026, 1, 1 + article_id % 28, tzinfo=timezone.utc
                ),
                tags=tag_rows[: 1 + article_id % tags],
            )
            session.add(article)
            for _ in range(attachments):
                attachment_id += 1
                session.add(
                    Attachment(
                        id=attachment_id,
                        article=article,
                        filename=f"file{attachment_id}.png",
                        size=1000 + attachment_id,
                    )
                )
            for c in range(comments):
                comment_id += 1
                top = Comment(
                    id=comment_id,
                    article=article,
                    author_name=f"reader{c}",
                    body="nice",
                    approved=c % 2 == 0,
                )
                session.add(top)
                for _ in range(replies):
                    comment_id += 1
                    session.add(
                        Comment(
                            id=comment_id,
                            article=article,
                            parent=top,
                            author_name="author",
                            body="thanks",
                        )
                    )
    session.commit()


def setup(ctx: Ctx, **seed_kwargs: int) -> None:
    """The shared ``setup`` of the scenarios that only need the content rows."""
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        seed(session, **seed_kwargs)
