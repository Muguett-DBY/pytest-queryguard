"""The private SQLAlchemy names of ``pytest_queryguard._compat`` still exist.

Each test runs real SQLAlchemy code and checks that the attribute, option,
frame or code object the plugin reads is there and holds what the plugin
expects. A SQLAlchemy release that renames or drops one fails here, with the
name in the message, before the listeners quietly stop labelling loads.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy
from sqlalchemy import ForeignKey, create_engine, event, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    contains_eager,
    joinedload,
    mapped_column,
    relationship,
    selectinload,
)

from pytest_queryguard import _compat, callsite, eager, listeners
from pytest_queryguard._compat import (
    ADAPTED_CURSOR,
    ATTRIBUTE_DESCRIPTOR,
    CHUNKS_CODE,
    CHUNKS_CONTEXT,
    CHUNKS_CURSOR,
    COMPILE_OPTIONS,
    COMPILE_STATE_PLUGIN,
    CURRENT_PATH,
    EAGER_FROM_ALIAS,
    ELEMENT_LOCAL_OPTS,
    ELEMENT_PATH,
    ELEMENT_STRATEGY,
    EVENTS_TODO,
    FLUSH_CONTEXT,
    FLUSH_FUNCTION,
    FOR_REFRESH_STATE,
    INSTANCE_CODE,
    INSTANCE_LOAD_PATH,
    INSTANCE_STATE,
    INVOKED_STATEMENT,
    IS_USER_REFRESH,
    LAZY_LOADED_FROM,
    LOAD_CONTEXT,
    LOAD_OPTIONS,
    ONLY_LOAD_PROPS,
    OPTION_ENGINE_PARENT,
    ORM_PLUGIN,
    PROPAGATE_ATTRS,
    PYMYSQL_RESULT,
    QUERY_LOAD_OPTIONS,
    QUERY_REFRESH_STATE,
    QUERY_STATEMENT,
    REFRESH_STATE,
    RUNID,
    SESSION_FILE,
    STATE_LOAD_PATH,
    TOP_LEVEL_CONTEXT,
    TOP_LEVEL_ORM_CONTEXT,
    WITH_OPTIONS,
    QueryContext,
    attribute_get,
    load_option_attribute,
    option_engine_parent,
)

# The outer plugin measures these tests too; their loads are on purpose.
pytestmark = pytest.mark.allow_nplusone

VERSION = sqlalchemy.__version__


def _gone(what: str, name: str) -> str:
    return (
        f"SQLAlchemy {VERSION}: {what} has no {name!r} any more; "
        f"update pytest_queryguard/_compat.py (and the code that reads it)"
    )


def _attr(obj: Any, name: str, what: str) -> Any:
    """``getattr(obj, name)``, failing with the name when it is missing."""
    assert hasattr(obj, name), _gone(what, name)
    return getattr(obj, name)


def _option(options: Any, name: str, what: str) -> Any:
    assert name in options, _gone(what, name)
    return options[name]


class Base(DeclarativeBase):
    pass


class Shelf(Base):
    __tablename__ = "compat_shelf"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    books: Mapped[list[Book]] = relationship(back_populates="shelf")
    # a mapped eager loader, run once per object by session.refresh()
    tags: Mapped[list[Tag]] = relationship(lazy="selectin")


class Book(Base):
    __tablename__ = "compat_book"
    id: Mapped[int] = mapped_column(primary_key=True)
    shelf_id: Mapped[int] = mapped_column(ForeignKey("compat_shelf.id"))
    shelf: Mapped[Shelf] = relationship(back_populates="books")


class Tag(Base):
    __tablename__ = "compat_tag"
    id: Mapped[int] = mapped_column(primary_key=True)
    shelf_id: Mapped[int] = mapped_column(ForeignKey("compat_shelf.id"))


@pytest.fixture
def engine() -> Iterator[Engine]:
    db = create_engine("sqlite://")
    Base.metadata.create_all(db)
    with Session(db) as s:
        s.add_all(
            Shelf(id=i, name=f"s{i}", books=[Book(id=i)], tags=[Tag(id=i)])
            for i in (1, 2)
        )
        s.commit()
    yield db
    db.dispose()


@pytest.fixture
def contexts(engine: Engine) -> Iterator[list[tuple[str, Any]]]:
    """(SQL, execution context) of each statement the engine runs."""
    seen: list[tuple[str, Any]] = []

    def spy(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append((statement, args[-2]))

    event.listen(engine, "before_cursor_execute", spy)
    yield seen
    event.remove(engine, "before_cursor_execute", spy)


def _orm(context: Any) -> tuple[Any, Any, Any]:
    """(statement, compile options, load options) of an ORM SELECT's context."""
    statement = _attr(context, INVOKED_STATEMENT, "the execution context")
    attrs = _attr(statement, PROPAGATE_ATTRS, "an ORM select()")
    assert _option(attrs, COMPILE_STATE_PLUGIN, "_propagate_attrs") == ORM_PLUGIN
    compile_options = _attr(statement, COMPILE_OPTIONS, "an ORM select()")
    load = context.execution_options.get(LOAD_OPTIONS)
    assert listeners.orm_select(context) is not None
    return statement, compile_options, load


def test_a_top_level_query(engine: Engine, contexts: list[tuple[str, Any]]) -> None:
    with Session(engine) as session:
        session.scalars(select(Shelf)).all()
    _, compile_options, _ = _orm(contexts[0][1])
    path = _attr(compile_options, CURRENT_PATH, "the ORM compile options")
    assert path.is_root, "a top-level query's _current_path is the root path"
    assert not _attr(compile_options, FOR_REFRESH_STATE, "the ORM compile options")


def test_a_core_statement_is_not_orm(
    engine: Engine, contexts: list[tuple[str, Any]]
) -> None:
    with engine.connect() as conn:
        conn.execute(select(Shelf.__table__)).all()
    assert listeners.orm_select(contexts[0][1]) is None


def test_a_lazy_load(engine: Engine, contexts: list[tuple[str, Any]]) -> None:
    with Session(engine) as session:
        shelf = session.get(Shelf, 1)
        assert shelf is not None
        contexts.clear()
        _ = shelf.books
        _, compile_options, load = _orm(contexts[0][1])
        path = _attr(compile_options, CURRENT_PATH, "the ORM compile options")
        assert not path.is_root
        assert path[-1] is Shelf.books.property
        parent = _attr(load, LAZY_LOADED_FROM, "the ORM load options")
        assert parent is inspect(shelf)


@pytest.mark.parametrize("user_refresh", [False, True])
def test_a_column_load(
    engine: Engine, contexts: list[tuple[str, Any]], user_refresh: bool
) -> None:
    with Session(engine) as session:
        shelf = session.get(Shelf, 1)
        assert shelf is not None
        contexts.clear()
        if user_refresh:
            session.refresh(shelf, ["name"])
        else:
            session.expire(shelf, ["name"])
            _ = shelf.name
        _, compile_options, load = _orm(contexts[0][1])
        assert _attr(compile_options, FOR_REFRESH_STATE, "the ORM compile options")
        only = _attr(compile_options, ONLY_LOAD_PROPS, "the ORM compile options")
        assert "name" in set(only)
        state = _attr(load, REFRESH_STATE, "the ORM load options")
        assert state is inspect(shelf)
        assert _attr(load, IS_USER_REFRESH, "the ORM load options") is user_refresh


def test_a_selectin_loader_and_a_refresh_of_its_parent(
    engine: Engine, contexts: list[tuple[str, Any]]
) -> None:
    with Session(engine) as session:
        session.scalars(select(Shelf).options(selectinload(Shelf.books))).all()
        loader = next(c for sql, c in contexts if "FROM compat_book" in sql)
        top = _option(
            loader.execution_options, TOP_LEVEL_ORM_CONTEXT, "a selectin loader"
        )
        assert isinstance(top, QueryContext)
        assert _attr(top, "session", "the top-level QueryContext") is session
        assert _attr(top, QUERY_REFRESH_STATE, "the top-level QueryContext") is None
        assert listeners._loader_session(loader.execution_options) is session

        shelf = session.get(Shelf, 1)
        contexts.clear()
        session.refresh(shelf)
        loader = next(c for sql, c in contexts if "FROM compat_tag" in sql)
        top = _option(
            loader.execution_options, TOP_LEVEL_ORM_CONTEXT, "a refresh's loader"
        )
        assert getattr(top, QUERY_REFRESH_STATE) is inspect(shelf)
        assert listeners._is_refresh_eager_load(loader.execution_options)


@pytest.mark.parametrize("option", listeners._LEAKED_LOAD_OPTIONS)
def test_the_load_options_keep_the_execution_options_they_read(option: str) -> None:
    defaults = QueryContext.default_load_options
    _attr(defaults, load_option_attribute(option), "the ORM load options")


def test_the_orm_execute_state(engine: Engine) -> None:
    seen: list[Any] = []

    def listener(state: Any) -> None:
        if not seen:
            todo = _attr(state, EVENTS_TODO, "ORMExecuteState")
            seen.append(list(todo))
            seen.append(state.local_execution_options)
            seen.append(_attr(state.statement, WITH_OPTIONS, "an ORM select()"))

    with Session(engine) as session:
        event.listen(session, "do_orm_execute", listener)
        stmt = select(Shelf).options(joinedload(Shelf.books))
        session.scalars(stmt).unique().all()
    todo, local, options = seen
    assert listener in todo
    assert hasattr(local, "get")
    assert len(options) == 1


def test_loader_options() -> None:
    stmt = select(Shelf).options(joinedload(Shelf.books))
    (option,) = _attr(stmt, WITH_OPTIONS, "a select()")
    (element,) = _attr(option, LOAD_CONTEXT, "a Load option")
    path = _attr(_attr(element, ELEMENT_PATH, "a load element"), "path", "its path")
    assert path[1] is Shelf.books.property
    assert _attr(element, ELEMENT_STRATEGY, "a load element") == (("lazy", "joined"),)
    assert isinstance(_attr(element, ELEMENT_LOCAL_OPTS, "a load element"), dict)
    assert [(p[1], s) for p, s in eager.loader_paths([option])] == [
        (Shelf.books.property, "joined")
    ]
    joined = select(Shelf).join(Shelf.books).options(contains_eager(Shelf.books))
    (option,) = joined._with_options
    (element,) = getattr(option, LOAD_CONTEXT)
    assert EAGER_FROM_ALIAS in getattr(element, ELEMENT_LOCAL_OPTS), _gone(
        "contains_eager()'s local_opts", EAGER_FROM_ALIAS
    )


def test_the_query_context_and_frames_of_a_load(engine: Engine) -> None:
    """What the Mapper ``load`` event and the frames above it hold."""
    assert CHUNKS_CODE is not None, _gone("loading.instances", "chunks")
    assert INSTANCE_CODE is not None, _gone("loading._instance_processor", "_instance")
    seen: dict[str, Any] = {}

    def on_load(state: Any, context: Any) -> None:
        if seen:
            return
        seen["context"] = context
        seen["state"] = state
        frame: Any = sys._getframe(1)
        while frame is not None:
            if frame.f_code is CHUNKS_CODE:
                seen["chunks"] = dict(frame.f_locals)
            if frame.f_code is INSTANCE_CODE:
                seen["instance"] = dict(frame.f_locals)
            frame = frame.f_back

    event.listen(Book, "load", on_load, raw=True)
    try:
        with Session(engine) as session:
            stmt = select(Book)
            (book,) = session.scalars(stmt.where(Book.id == 1)).all()
            context = seen["context"]
            assert isinstance(context, QueryContext)
            assert _attr(context, TOP_LEVEL_CONTEXT, "QueryContext") is None
            _attr(context, QUERY_LOAD_OPTIONS, "QueryContext")
            assert _attr(context, QUERY_STATEMENT, "QueryContext") is not None
            assert isinstance(_attr(context, RUNID, "QueryContext"), int)
            assert _attr(context, QUERY_REFRESH_STATE, "QueryContext") is None
            state = seen["state"]
            assert _attr(state, STATE_LOAD_PATH, "InstanceState") is not None
            assert _attr(book, INSTANCE_STATE, "a mapped instance") is state
            chunks = seen.get("chunks")
            assert chunks is not None, _gone("the stack of a load", "chunks frame")
            assert _option(chunks, CHUNKS_CONTEXT, "chunks' locals") is context
            cursor = _option(chunks, CHUNKS_CURSOR, "chunks' locals")
            _attr(cursor, "context", "the CursorResult")
            instance = seen.get("instance")
            assert instance is not None, _gone("the stack of a load", "_instance")
            _option(instance, INSTANCE_LOAD_PATH, "_instance's locals")
    finally:
        event.remove(Book, "load", on_load)


def test_the_flush_frame(engine: Engine) -> None:
    assert SESSION_FILE is not None
    found: list[Any] = []

    def spy(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        frame: Any = sys._getframe(1)
        while frame is not None:
            code = frame.f_code
            if code.co_name == FLUSH_FUNCTION and code.co_filename == SESSION_FILE:
                found.append(frame.f_locals.get(FLUSH_CONTEXT))
                break
            frame = frame.f_back
        found.append(callsite.find_flush())

    event.listen(engine, "before_cursor_execute", spy)
    try:
        with Session(engine) as session:
            session.add(Shelf(id=3, name="s3"))
            session.flush()
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert found, "no statement ran"
    assert found[0] is not None, _gone(
        f"Session.{FLUSH_FUNCTION} in {Path(SESSION_FILE).name}", FLUSH_CONTEXT
    )
    assert isinstance(found[1], int)


def test_the_attribute_descriptor(engine: Engine) -> None:
    original = attribute_get()
    reads: list[str] = []

    def tracked(self: Any, instance: Any, owner: Any) -> Any:
        if instance is not None:
            reads.append(self.key)
        return original(self, instance, owner)

    with Session(engine) as session:
        shelf = session.get(Shelf, 1)
        ATTRIBUTE_DESCRIPTOR.__get__ = tracked  # type: ignore[method-assign]
        try:
            _ = shelf.name  # type: ignore[union-attr]
        finally:
            ATTRIBUTE_DESCRIPTOR.__get__ = original  # type: ignore[method-assign]
    assert reads == ["name"], (
        f"SQLAlchemy {VERSION}: a read of a mapped attribute no longer goes "
        f"through {ATTRIBUTE_DESCRIPTOR.__name__}.__get__; update _compat.py"
    )


def test_an_option_engine_leads_to_its_parent(engine: Engine) -> None:
    derived = engine.execution_options(isolation_level="AUTOCOMMIT")
    assert hasattr(derived, OPTION_ENGINE_PARENT), _gone(
        "an OptionEngine", OPTION_ENGINE_PARENT
    )
    assert option_engine_parent(derived) is engine
    assert option_engine_parent(engine) is None


def test_the_async_mysql_adapter_cursor_keeps_the_driver_cursor() -> None:
    adapter = pytest.importorskip("sqlalchemy.dialects.mysql.aiomysql")
    cls = adapter.AsyncAdapt_aiomysql_cursor

    def sets_it(klass: type) -> bool:
        if ADAPTED_CURSOR in getattr(klass, "__slots__", ()):
            return True
        init = klass.__dict__.get("__init__")
        return ADAPTED_CURSOR in getattr(
            getattr(init, "__code__", None), "co_names", ()
        )

    assert any(sets_it(k) for k in cls.__mro__), _gone(cls.__name__, ADAPTED_CURSOR)


def test_the_pymysql_cursor_keeps_its_result() -> None:
    cursors = pytest.importorskip("pymysql.cursors")
    cursor = cursors.SSCursor(None)
    assert hasattr(cursor, PYMYSQL_RESULT), (
        f"PyMySQL: its cursor has no {PYMYSQL_RESULT!r} any more; update _compat.py"
    )


def test_every_name_is_checked_here() -> None:
    """Each constant and accessor of _compat is used in this file."""
    source = Path(__file__).read_text(encoding="utf-8")
    names = [
        name
        for name, value in vars(_compat).items()
        if not name.startswith("_")
        and not isinstance(value, ModuleType)
        and name not in ("annotations", "Any", "nested_code")
    ]
    # imported once, then used in a check at least once
    missing = [name for name in names if len(re.findall(rf"\b{name}\b", source)) < 2]
    assert not missing, f"tests/test_compat.py does not check {missing}"
