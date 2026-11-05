"""``session.refresh()`` of an object whose relationship is mapped eager.

Refreshing a collection mapped ``lazy="selectin"`` (or reaching one through a
``lazy="joined"`` relationship) runs the mapped eager loader for that one
object. The loader's query is not a lazy load, so it is not an N+1 and the
hint to use ``selectinload`` would name the strategy that is already mapped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import ForeignKey, create_engine, select
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    immediateload,
    mapped_column,
    reconstructor,
    relationship,
)
from sqlalchemy.pool import StaticPool

from pytest_queryguard import NPlusOneDetected, listeners, record_queries

# pytester-free, but the outer plugin watches these tests too.
pytestmark = pytest.mark.allow_nplusone


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "author"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    books: Mapped[list[Book]] = relationship(back_populates="author", lazy="selectin")
    prizes: Mapped[list[Prize]] = relationship(lazy="subquery")
    awards: Mapped[list[Award]] = relationship(lazy="joined")
    # the strategy the finding is about: loaded one parent at a time
    notes: Mapped[list[Note]] = relationship(lazy="select")


class Book(Base):
    __tablename__ = "book"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"))
    author: Mapped[Author] = relationship(back_populates="books")


class Prize(Base):
    __tablename__ = "prize"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"))


class Award(Base):
    __tablename__ = "award"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"))


class Note(Base):
    __tablename__ = "note"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"))


class Review(Base):
    """A row whose ``author`` is mapped joined, like the issue's token -> user."""

    __tablename__ = "review"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"))
    author: Mapped[Author] = relationship(lazy="joined")


ROWS = 6


def _seed(session: Session) -> None:
    for i in range(1, ROWS + 1):
        session.add(
            Author(
                id=i,
                name=f"a{i}",
                books=[Book(id=i)],
                prizes=[Prize(id=i)],
                awards=[Award(id=i)],
                notes=[Note(id=i)],
            )
        )
    session.commit()


@pytest.fixture
def engine() -> Iterator[Any]:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as seed:
        _seed(seed)
    yield engine
    engine.dispose()


def _report(log: Any) -> str:
    try:
        log.assert_no_nplusone()
    except NPlusOneDetected as exc:
        return str(exc)
    return ""


def _create(session: Session, id_: int) -> Author:
    # the repository "create": add, flush, refresh the one new row
    author = Author(id=id_, name=f"n{id_}")
    session.add(author)
    session.flush()
    session.refresh(author)
    return author


class _OverridingSession(Session):
    """A Session subclass between the lazy loader and the ORM execution."""

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return super().execute(*args, **kwargs)


def _deep(depth: int, fn: Any) -> Any:
    return _deep(depth - 1, fn) if depth else fn()


@pytest.mark.parametrize("session_class", [Session, _OverridingSession])
def test_creates_with_refresh_deep_in_the_stack_are_clean(
    engine: Any, session_class: type[Session]
) -> None:
    # The loader check walks from the listener to the lazy loader, not up the
    # whole stack: a test far down the stack and an execute() override in
    # between change nothing.
    def body() -> Any:
        with session_class(engine) as session, record_queries() as log:
            _create(session, 100)
            _create(session, 101)
            for author in session.scalars(select(Author)).unique().all():
                _ = author.notes
        return log

    report = _report(_deep(300, body))
    assert "Author.books" not in report
    assert "Author.notes was lazy-loaded" in report


def test_two_creates_with_refresh_are_clean(engine: Any) -> None:
    with Session(engine) as session, record_queries() as log:
        _create(session, 100)
        _create(session, 101)
    assert _report(log) == ""


def test_many_creates_with_refresh_are_clean(engine: Any) -> None:
    # neither a relationship-load nor (each refresh reads back its own
    # write) a column-load finding
    with Session(engine) as session, record_queries() as log:
        for i in range(100, 108):
            _create(session, i)
    assert _report(log) == ""


def test_refresh_with_attribute_names_is_clean(engine: Any) -> None:
    with Session(engine) as session, record_queries() as log:
        a, b = session.get(Author, 1), session.get(Author, 2)
        assert a is not None and b is not None
        session.refresh(a, ["books"])
        session.refresh(b, ["books"])
    assert _report(log) == ""


@pytest.mark.parametrize("attr", ["books", "prizes", "awards"])
def test_each_eager_strategy_is_clean_with_named_refresh(
    engine: Any, attr: str
) -> None:
    with Session(engine) as session, record_queries() as log:
        authors = [session.get(Author, i) for i in (1, 2, 3)]
        for author in authors:
            assert author is not None
            session.refresh(author, [attr])
    assert "lazy-loaded" not in _report(log)


def test_joined_relationship_pulling_a_selectin_collection_is_clean(
    engine: Any,
) -> None:
    with Session(engine) as session:
        session.add_all([Review(id=1, author_id=1), Review(id=2, author_id=2)])
        session.commit()
        session.expunge_all()
    with Session(engine) as session, record_queries() as log:
        for review_id in (1, 2):
            review = session.get(Review, review_id)
            assert review is not None
            session.refresh(review)
    assert "lazy-loaded" not in _report(log)


def test_loading_one_by_one_is_clean(engine: Any) -> None:
    with Session(engine) as session, record_queries() as log:
        for i in (1, 2, 3):
            session.get(Author, i)
        for i in (4, 5):
            session.scalars(select(Author).where(Author.id == i)).unique().one()
    assert "lazy-loaded" not in _report(log)


def test_a_real_lazy_load_next_to_refreshes_is_still_reported(engine: Any) -> None:
    with Session(engine) as session, record_queries() as log:
        authors = session.scalars(select(Author)).unique().all()
        _create(session, 100)
        _create(session, 101)
        for author in authors:
            _ = author.notes  # lazy="select": one query per author
    report = _report(log)
    assert f"Author.notes was lazy-loaded {ROWS} times" in report
    assert "Author.books" not in report
    assert "Author.prizes" not in report


def test_a_refresh_loop_followed_by_lazy_access_is_reported(engine: Any) -> None:
    with Session(engine) as session, record_queries() as log:
        for author in session.scalars(select(Author)).unique().all():
            session.refresh(author)
            _ = author.notes
    report = _report(log)
    assert f"Author.notes was lazy-loaded {ROWS} times" in report
    assert "Author.books" not in report


def test_a_refresh_loop_is_still_a_column_load_finding(engine: Any) -> None:
    with Session(engine) as session, record_queries() as log:
        for author in session.scalars(select(Author)).unique().all():
            session.refresh(author)
    report = _report(log)
    assert f"reloaded with session.refresh() {ROWS} times" in report
    assert "lazy-loaded" not in report


def test_an_immediateload_query_is_still_a_lazy_load_per_parent(engine: Any) -> None:
    with Session(engine) as session, record_queries() as log:
        session.scalars(
            select(Author).options(immediateload(Author.notes))
        ).unique().all()
    assert f"Author.notes was lazy-loaded {ROWS} times" in _report(log)


def test_expired_selectin_collections_are_an_expired_column_load(engine: Any) -> None:
    # SQLAlchemy reloads the expired object through the same refresh path, so
    # the mapped selectin load is not a lazy load; the loop is the expired
    # column-load finding
    with Session(engine) as session, record_queries() as log:
        authors = session.scalars(select(Author)).unique().all()
        session.expire_all()
        for author in authors:
            _ = author.books
    report = _report(log)
    assert "Author.books was lazy-loaded" not in report
    assert "Author (expired)" in report


# ------------------------------------------------------------------- async


def _run(work: Any) -> Any:
    async def main() -> Any:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as seed:
                for i in range(1, ROWS + 1):
                    seed.add(
                        Author(
                            id=i,
                            name=f"a{i}",
                            books=[Book(id=i)],
                            prizes=[Prize(id=i)],
                            awards=[Award(id=i)],
                            notes=[Note(id=i)],
                        )
                    )
                await seed.commit()
            with record_queries() as log:
                async with maker() as session:
                    await work(session)
            return log
        finally:
            await engine.dispose()

    return asyncio.run(main())


def test_async_two_creates_with_refresh_are_clean() -> None:
    async def work(session: AsyncSession) -> None:
        for i in (100, 101):
            author = Author(id=i, name=f"n{i}")
            session.add(author)
            await session.flush()
            await session.refresh(author)

    assert _report(_run(work)) == ""


def test_async_refresh_with_attribute_names_is_clean() -> None:
    async def work(session: AsyncSession) -> None:
        for i in (1, 2, 3):
            author = await session.get(Author, i)
            assert author is not None
            await session.refresh(author, ["books"])

    assert _report(_run(work)) == ""


def test_async_refresh_next_to_a_real_lazy_load_is_still_reported() -> None:
    async def work(session: AsyncSession) -> None:
        authors = (await session.scalars(select(Author))).unique().all()
        for i in (100, 101):
            author = Author(id=i, name=f"n{i}")
            session.add(author)
            await session.flush()
            await session.refresh(author)
        for author in authors:
            await author.awaitable_attrs.notes

    report = _report(_run(work))
    assert f"Author.notes was lazy-loaded {ROWS} times" in report
    assert "Author.books" not in report


# ---------------------------------------------------------------- fallback


@pytest.mark.parametrize(
    "name, value", [("_IMMEDIATE_CODE", None), ("_MAX_LOADER_DEPTH", 0)]
)
def test_without_the_immediate_loader_refreshes_count_as_before(
    engine: Any, monkeypatch: pytest.MonkeyPatch, name: str, value: Any
) -> None:
    # A SQLAlchemy that renames the loader (or moves it out of reach) gets the
    # old behaviour: the refresh loads count as lazy loads, nothing breaks.
    monkeypatch.setattr(listeners, name, value)
    with Session(engine) as session, record_queries() as log:
        _create(session, 100)
        _create(session, 101)
    assert "Author.books was lazy-loaded 2 times" in _report(log)
    assert listeners._error is None


def test_the_immediate_loader_is_found_on_this_sqlalchemy() -> None:
    assert listeners._IMMEDIATE_CODE is not None


# ------------------------------------------- loads run by user code in a refresh


class HookBase(DeclarativeBase):
    pass


class Shelf(HookBase):
    __tablename__ = "shelf"

    id: Mapped[int] = mapped_column(primary_key=True)
    items: Mapped[list[Item]] = relationship(lazy="selectin")


class Keeper(HookBase):
    __tablename__ = "keeper"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class Item(HookBase):
    __tablename__ = "item"

    id: Mapped[int] = mapped_column(primary_key=True)
    shelf_id: Mapped[int] = mapped_column(ForeignKey("shelf.id"))
    keeper_id: Mapped[int] = mapped_column(ForeignKey("keeper.id"))
    keeper: Mapped[Keeper] = relationship(lazy="select")
    read_keeper = False

    @reconstructor
    def _on_load(self) -> None:
        # user code that runs while the refresh's immediate loader loads items
        if Item.read_keeper:
            self.keeper_name = self.keeper.name


def test_a_lazy_load_run_by_a_load_hook_inside_a_refresh_is_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the immediate loader's own query for the refreshed object is an
    # eager load; a lazy load that user code runs while it loads the children
    # (here a reconstructor reading Item.keeper) is a lazy load as anywhere else.
    engine = create_engine("sqlite://", poolclass=StaticPool)
    HookBase.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            for i in (1, 2, 3):
                session.add_all([Keeper(id=i, name=f"k{i}"), Shelf(id=i)])
                session.flush()
                session.add(Item(id=i, shelf_id=i, keeper_id=i))
            session.commit()
        with Session(engine) as session:
            shelves = session.scalars(select(Shelf)).all()
            for obj in list(session.identity_map.values()):
                if not isinstance(obj, Shelf):
                    session.expunge(obj)
            monkeypatch.setattr(Item, "read_keeper", True)
            with record_queries() as log:
                session.refresh(shelves[0])
                session.refresh(shelves[1])
    finally:
        engine.dispose()
    report = _report(log)
    assert "Item.keeper was lazy-loaded 2 times" in report
    assert "label: Shelf.items" not in report
