"""A lazy load keeps its relationship when other statements run before its SELECT.

The attribution is parked on the Session of the ORM execution and handed only
to a SELECT on a connection that Session opened, so asyncio tasks, statements
of begin listeners and threads cannot take it or clear it.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import ForeignKey, create_engine, event, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    selectinload,
)
from sqlalchemy.pool import NullPool, StaticPool

from pytest_queryguard import NPlusOneDetected, listeners, record_queries

pytest.importorskip("greenlet")
pytest.importorskip("aiosqlite")
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    create_async_engine,
)

# The inner assertions are about the log, not about this module's own queries.
pytestmark = pytest.mark.allow_nplusone


class Base(AsyncAttrs, DeclarativeBase): ...


class Parent(Base):
    __tablename__ = "parent"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(default="")
    children: Mapped[list[Child]] = relationship()


class Child(Base):
    __tablename__ = "child"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("parent.id"))


class Thing(Base):
    __tablename__ = "thing"
    id: Mapped[int] = mapped_column(primary_key=True)


def _child_relationships(log: Any) -> set[str | None]:
    return {q.relationship for q in log if "FROM child" in q.sql}


def _only_child_loads_have_a_relationship(log: Any) -> None:
    tagged = [q for q in log if q.relationship is not None]
    assert tagged
    assert all("FROM child" in q.sql for q in tagged)


async def _lazy_loads(session: AsyncSession) -> None:
    for parent in (await session.scalars(select(Parent))).all():
        parent.name = "changed"  # a pending UPDATE, autoflushed by the lazy load
        await parent.awaitable_attrs.children


async def _plain_selects(session: AsyncSession) -> None:
    for i in range(1, 7):
        await session.execute(select(Thing).where(Thing.id == i))
        await asyncio.sleep(0)


async def _concurrent(url: str, poolclass: type, tasks: int) -> Any:
    engine = create_async_engine(url, poolclass=poolclass)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as seed:
        seed.add_all(Parent(id=i, children=[Child()]) for i in range(1, 7))
        seed.add_all(Thing(id=i) for i in range(1, 7))
        await seed.commit()
    sessions = [AsyncSession(engine) for _ in range(tasks)]
    jobs = [
        _lazy_loads(s) if n == 0 else _plain_selects(s) for n, s in enumerate(sessions)
    ]
    with record_queries() as log:
        await asyncio.gather(*jobs)
    for s in sessions:
        await s.close()
    await engine.dispose()
    return log


@pytest.mark.parametrize("poolclass", [NullPool, StaticPool])
@pytest.mark.parametrize("tasks", [2, 3, 5])
def test_concurrent_async_sessions_keep_each_attribution(
    tmp_path: Path, poolclass: type, tasks: int
) -> None:
    url = (
        f"sqlite+aiosqlite:///{tmp_path}/db.sqlite"
        if poolclass is NullPool
        else "sqlite+aiosqlite://"
    )
    log = asyncio.run(_concurrent(url, poolclass, tasks))
    assert _child_relationships(log) == {"Parent.children"}
    _only_child_loads_have_a_relationship(log)
    assert not [q for q in log if "FROM thing" in q.sql and q.relationship]


def test_concurrent_lazy_loads_are_counted_once(tmp_path: Path) -> None:
    log = asyncio.run(
        _concurrent(f"sqlite+aiosqlite:///{tmp_path}/db.sqlite", NullPool, 3)
    )
    # The lazy loads are one finding of their own: with the attribution lost
    # the repeat rule would report the same loop a second time.
    with pytest.raises(NPlusOneDetected) as report:
        log.assert_no_nplusone(relationship_threshold=5, repeat_threshold=5)
    assert str(report.value).count("FROM child") == 1


def _recipe_log(recipe: bool) -> Any:
    engine = create_engine("sqlite://")
    if recipe:
        # SQLAlchemy's documented pysqlite recipe for SAVEPOINT and DDL.
        @event.listens_for(engine, "connect")
        def do_connect(dbapi_connection: Any, record: Any) -> None:
            dbapi_connection.isolation_level = None

        @event.listens_for(engine, "begin")
        def do_begin(conn: Any) -> None:
            conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(Parent(id=i, children=[Child()]) for i in range(1, 7))
        session.commit()
    with Session(engine, expire_on_commit=False) as session, record_queries() as log:
        parents = session.scalars(select(Parent)).all()
        session.commit()
        for parent in parents:
            _ = parent.children  # the first statement of a new transaction
            session.commit()
    return log


@pytest.mark.parametrize("recipe", [False, True])
def test_a_begin_listener_statement_does_not_take_the_attribution(
    recipe: bool,
) -> None:
    log = _recipe_log(recipe)
    assert _child_relationships(log) == {"Parent.children"}
    _only_child_loads_have_a_relationship(log)
    # The recipe's own BEGIN repeats are a separate matter (see the scope of a
    # statement that runs before after_begin); the lazy loads must not repeat.
    try:
        log.assert_no_nplusone(relationship_threshold=100, repeat_threshold=5)
    except NPlusOneDetected as exc:
        assert "FROM child" not in str(exc)


def test_an_after_begin_select_does_not_take_the_attribution() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as seed:
        seed.add_all(Parent(id=i, children=[Child()]) for i in range(1, 4))
        seed.commit()

    @event.listens_for(Session, "after_begin")
    def probe(session: Session, transaction: Any, connection: Any) -> None:
        connection.exec_driver_sql("SELECT 1")

    try:
        with (
            Session(engine, expire_on_commit=False) as session,
            record_queries() as log,
        ):
            parents = session.scalars(select(Parent)).all()
            session.commit()
            for parent in parents:
                _ = parent.children
                session.commit()
    finally:
        event.remove(Session, "after_begin", probe)
    assert _child_relationships(log) == {"Parent.children"}


def _deep(depth: int, fn: Any) -> Any:
    return _deep(depth - 1, fn) if depth else fn()


class _OverridingSession(Session):
    """A Session subclass between the lazy loader and the ORM execution."""

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return super().execute(*args, **kwargs)


@pytest.mark.parametrize("session_class", [Session, _OverridingSession])
def test_a_nested_after_begin_select_deep_in_the_stack_keeps_out(
    session_class: type[Session],
) -> None:
    # A begin listener that reaches its SELECT through helpers, in a test that
    # runs far down the stack, is still told apart from the lazy load's own
    # SELECT.
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as seed:
        seed.add_all(Parent(id=i, children=[Child()]) for i in range(1, 4))
        seed.commit()

    @event.listens_for(Session, "after_begin")
    def probe(session: Session, transaction: Any, connection: Any) -> None:
        _deep(10, lambda: connection.exec_driver_sql("SELECT 1"))

    def body() -> Any:
        with (
            session_class(engine, expire_on_commit=False) as session,
            record_queries() as log,
        ):
            parents = session.scalars(select(Parent)).all()
            session.commit()
            for parent in parents:
                _ = parent.children
                session.commit()
        return log

    try:
        log = _deep(300, body)
    finally:
        event.remove(Session, "after_begin", probe)
    assert _child_relationships(log) == {"Parent.children"}
    _only_child_loads_have_a_relationship(log)
    assert listeners.internal_error() is None


def test_autoflush_writes_before_a_lazy_load_keep_the_attribution() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as seed:
        seed.add_all(Parent(id=i, children=[Child()]) for i in range(1, 7))
        seed.commit()
    with Session(engine) as session, record_queries() as log:
        for parent in session.scalars(select(Parent)).all():
            parent.name = "changed"
            _ = parent.children
    assert _child_relationships(log) == {"Parent.children"}
    assert all(
        q.relationship is None for q in log if q.sql.lstrip().startswith("UPDATE")
    )


def test_threads_keep_each_attribution(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path}/db.sqlite", poolclass=NullPool)
    Base.metadata.create_all(engine)
    with Session(engine) as seed:
        seed.add_all(Parent(id=i, children=[Child()]) for i in range(1, 7))
        seed.add_all(Thing(id=i) for i in range(1, 7))
        seed.commit()
    barrier = threading.Barrier(3)
    errors: list[BaseException] = []

    def lazy() -> None:
        try:
            with Session(engine) as session:
                parents = session.scalars(select(Parent)).all()
                barrier.wait()
                for parent in parents:
                    _ = parent.children
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def plain() -> None:
        try:
            with Session(engine) as session:
                barrier.wait()
                for i in range(1, 7):
                    session.execute(select(Thing).where(Thing.id == i)).all()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with record_queries() as log:
        workers = [threading.Thread(target=f) for f in (lazy, plain, lazy)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
    assert not errors
    assert _child_relationships(log) == {"Parent.children"}
    assert not [q for q in log if "FROM thing" in q.sql and q.relationship]


def _shared_connection_engine() -> Any:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as seed:
        seed.add_all(Parent(id=i, children=[Child()]) for i in range(1, 5))
        seed.add_all(Thing(id=i) for i in range(1, 5))
        seed.commit()
    return engine


@pytest.mark.parametrize("interleaved", [False, True])
def test_two_sessions_on_one_connection_keep_each_attribution(
    interleaved: bool,
) -> None:
    # The "test in a transaction" fixture pattern, with a second Session (a
    # factory, an app's own session) joined to the same external connection.
    engine = _shared_connection_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        first = Session(bind=conn, join_transaction_mode="rollback_only")
        second = Session(bind=conn, join_transaction_mode="rollback_only")
        with record_queries() as log:
            a = first.scalars(select(Parent)).all()
            # The second Session begins on the connection after the first.
            b = second.scalars(select(Parent)).all()
            if interleaved:
                for x, y in zip(a, b, strict=True):
                    _ = x.children
                    _ = y.children
            else:
                for x in a:
                    _ = x.children
        first.close()
        second.close()
        trans.rollback()
    assert _child_relationships(log) == {"Parent.children"}
    _only_child_loads_have_a_relationship(log)


def test_async_sessions_on_one_connection_keep_each_attribution() -> None:
    async def run() -> Any:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as seed:
            seed.add_all(Parent(id=i, children=[Child()]) for i in range(1, 5))
            await seed.commit()
        async with engine.connect() as conn:
            await conn.begin()
            first = AsyncSession(bind=conn, join_transaction_mode="rollback_only")
            second = AsyncSession(bind=conn, join_transaction_mode="rollback_only")
            with record_queries() as log:
                a = (await first.scalars(select(Parent))).all()
                await second.scalars(select(Parent))
                for parent in a:
                    await parent.awaitable_attrs.children
            await first.close()
            await second.close()
            await conn.rollback()
        await engine.dispose()
        return log

    log = asyncio.run(run())
    assert _child_relationships(log) == {"Parent.children"}


def test_a_lazy_load_without_a_select_labels_no_later_statement() -> None:
    # A do_orm_execute listener of the user answers the lazy load from a cache,
    # so its SELECT never runs. The next statement carries its own options,
    # never the attribution of that load.
    engine = _shared_connection_engine()
    with Session(engine) as session:
        frozen = session.execute(select(Child).where(Child.id == -1)).freeze()

        def cache(state: Any) -> Any:
            return frozen() if state.is_relationship_load else None

        event.listen(session, "do_orm_execute", cache)
        parents = session.scalars(select(Parent)).all()
        with record_queries() as log:
            assert parents[0].children == []
            session.scalars(select(Thing).where(Thing.id == 1)).all()
    assert [q.relationship for q in log] == [None]


def test_a_selectin_load_gets_the_scope_of_its_own_session() -> None:
    # The connection's tag names the Session that began on it last; a loader
    # statement of the other Session still belongs to the Session it ran in.
    from pytest_queryguard import collector

    engine = _shared_connection_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        first = Session(bind=conn, join_transaction_mode="rollback_only")
        second = Session(bind=conn, join_transaction_mode="rollback_only")
        first.scalars(select(Thing)).all()
        second.scalars(select(Thing)).all()  # begins last: the connection's tag
        target = collector.QueryCollector()
        collector.push(target)
        try:
            first.scalars(select(Parent).options(selectinload(Parent.children))).all()
        finally:
            collector.pop(target)
        first.close()
        second.close()
        trans.rollback()
    [load] = target.relationship_loads
    assert load.relationship == "Parent.children"
    assert load.scope == listeners.scope_of_session(first)
    assert load.scope != listeners.scope_of_session(second)
