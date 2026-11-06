"""Loader statements keep their own load options with the plugin installed.

On SQLAlchemy 2.0.20 to 2.0.49 (and 2.1.0b1, 2.1.0b2) any ``do_orm_execute``
listener makes the outer statement's ``yield_per``, ``autoflush`` and similar
options become load options of its ``selectinload`` statement (SQLAlchemy issue
13301, fixed in 2.0.50). The plugin's listeners read the ORM's options from the
SELECT, with no such listener; the unused eager-load check and strict mode
still install one, so they remove the leaked options again when no other
listener takes part; see issue 169. Each test runs with the default listeners,
with that check on and in strict mode.

The expected values below are what SQLAlchemy 2.0.50 returns, with or without
the plugin; the CI job pinned to 2.0.49 checks that the plugin gets the same.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import sqlalchemy
from sqlalchemy import ForeignKey, create_engine, event, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    ORMExecuteState,
    Session,
    immediateload,
    mapped_column,
    relationship,
    selectinload,
)

from pytest_queryguard import eager, listeners

LEAKS = listeners._leaks_load_options()
# Loaders inherit the outer statement's execution options from 2.0.20 on
# (SQLAlchemy issue 10231).
_version = re.match(r"(\d+)\.(\d+)\.(\d+)", sqlalchemy.__version__)
INHERITS = _version is None or tuple(map(int, _version.groups())) >= (2, 0, 20)
UNIQUE_ERROR = "yield_per feature in conjunction with unique"


class Base(DeclarativeBase):
    pass


class Parent(Base):
    __tablename__ = "yp_parent"
    id: Mapped[int] = mapped_column(primary_key=True)
    lines: Mapped[list[Line]] = relationship()


class Line(Base):
    __tablename__ = "yp_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("yp_parent.id"))


@pytest.fixture
def eager_check() -> Iterator[None]:
    """The unused eager-load check on: the plugin's do_orm_execute listener."""
    listeners.install()
    eager.enable()
    try:
        yield
    finally:
        eager.disable()


@pytest.fixture
def strict_mode() -> Iterator[None]:
    """Strict mode on: the plugin's other do_orm_execute listener."""
    listeners.install()
    listeners.enable_strict()
    try:
        yield
    finally:
        listeners.disable_strict()


@pytest.fixture(params=["default", "eager-check", "strict"])
def session(request: pytest.FixtureRequest) -> Iterator[Session]:
    listeners.install()
    if request.param == "eager-check":
        request.getfixturevalue("eager_check")
    elif request.param == "strict":
        request.getfixturevalue("strict_mode")
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([Parent(id=i, lines=[Line()]) for i in range(1, 1201)])
        s.commit()
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def cursor_options() -> Iterator[list[tuple[str, Any]]]:
    seen: list[tuple[str, Any]] = []

    def spy(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        context = args[-2]
        table = "yp_line" if "FROM yp_line" in statement else "yp_parent"
        seen.append((table, context.execution_options.get("yield_per")))

    event.listen(sqlalchemy.engine.Engine, "before_cursor_execute", spy)
    yield seen
    event.remove(sqlalchemy.engine.Engine, "before_cursor_execute", spy)


def _stream(session: Session, batch: int = 200) -> int:
    stmt = (
        select(Parent)
        .options(selectinload(Parent.lines))
        .execution_options(yield_per=batch)
    )
    return sum(len(p.lines) for p in session.scalars(stmt))


def test_yield_per_with_selectinload_works_with_the_listener(session: Session) -> None:
    assert _stream(session) == 1200


def test_yield_per_method_with_selectinload_works(session: Session) -> None:
    stmt = select(Parent).options(selectinload(Parent.lines))
    assert sum(len(p.lines) for p in session.scalars(stmt).yield_per(300)) == 1200


def test_yield_per_given_to_execute_works(session: Session) -> None:
    stmt = select(Parent).options(selectinload(Parent.lines))
    result = session.execute(stmt, execution_options={"yield_per": 200}).scalars()
    assert [len(chunk) for chunk in result.partitions()] == [200] * 6


def test_legacy_query_yield_per_works(session: Session) -> None:
    query = session.query(Parent).options(selectinload(Parent.lines)).yield_per(200)
    assert sum(len(p.lines) for p in query) == 1200


@pytest.mark.allow_nplusone
def test_immediateload_with_yield_per_works(session: Session) -> None:
    stmt = (
        select(Parent)
        .where(Parent.id <= 20)
        .options(immediateload(Parent.lines))
        .execution_options(yield_per=5)
    )
    assert sum(len(p.lines) for p in session.scalars(stmt)) == 20


@pytest.mark.skipif(not INHERITS, reason="loaders do not inherit the options")
def test_the_cursor_options_are_those_of_sqlalchemy_2_0_50(
    session: Session, cursor_options: list[tuple[str, Any]]
) -> None:
    # The loader statement still streams with the outer yield_per, as it does
    # without any listener.
    stmt = (
        select(Parent)
        .options(selectinload(Parent.lines))
        .execution_options(yield_per=200)
    )
    sizes = [len(chunk) for chunk in session.scalars(stmt).partitions()]
    assert sizes == [200] * 6
    assert cursor_options == [("yp_parent", 200)] + [("yp_line", 200)] * 6


def test_autoflush_false_of_the_outer_statement_stays_there(session: Session) -> None:
    # Without listeners, and on 2.0.50, the loader statement autoflushes.
    session.add(Line(id=99999, parent_id=1))
    stmt = (
        select(Parent)
        .where(Parent.id == 1)
        .options(selectinload(Parent.lines))
        .execution_options(autoflush=False)
    )
    (parent,) = session.scalars(stmt).all()
    assert sorted(line.id for line in parent.lines) == [1, 99999]


def _own_orm_listeners() -> list[Callable[[ORMExecuteState], Any]]:
    candidates = [
        listeners._on_orm_execute_heal,
        listeners._on_orm_execute_strict,
        eager._on_orm_execute,
    ]
    return [fn for fn in candidates if event.contains(Session, "do_orm_execute", fn)]


def _outcome(engine: Any, run: Callable[[Session], Any]) -> tuple[str, Any]:
    with Session(engine) as s:
        try:
            return ("ok", run(s))
        except sqlalchemy.exc.InvalidRequestError as exc:
            return ("error", str(exc).splitlines()[0])


def _with_and_without_the_plugin(
    engine: Any, user: Callable[[ORMExecuteState], Any], run: Callable[[Session], Any]
) -> list[tuple[str, Any]]:
    """The outcome without the plugin's listeners, then with them before and after."""
    own = _own_orm_listeners()
    for fn in own:
        event.remove(Session, "do_orm_execute", fn)
    event.listen(Session, "do_orm_execute", user)
    try:
        without = _outcome(engine, run)
        for fn in own:  # the plugin's listeners after the user's
            event.listen(Session, "do_orm_execute", fn)
        after_user = _outcome(engine, run)
        event.remove(Session, "do_orm_execute", user)
        event.listen(Session, "do_orm_execute", user)  # the user's after the plugin's
        before_user = _outcome(engine, run)
    finally:
        event.remove(Session, "do_orm_execute", user)
        for fn in own:
            if not event.contains(Session, "do_orm_execute", fn):
                event.listen(Session, "do_orm_execute", fn)
    return [without, after_user, before_user]


def _loaders_stream(state: ORMExecuteState) -> None:
    if state.is_relationship_load:
        state.update_execution_options(yield_per=50)


def _all_selects_stream(state: ORMExecuteState) -> None:
    if state.is_select:
        state.update_execution_options(yield_per=50)


def _cache(state: ORMExecuteState) -> Any:
    return state.invoke_statement() if state.is_select else None


def _reader(state: ORMExecuteState) -> None:
    state.local_execution_options.get("yield_per")


@pytest.mark.parametrize(
    "user",
    [_reader, _loaders_stream, _all_selects_stream, _cache],
    ids=["reads-options", "yield-per-on-loaders", "yield-per-on-all", "invoke"],
)
@pytest.mark.parametrize("option", ["yield_per", "autoflush"])
def test_a_listener_of_the_user_sees_what_it_sees_without_the_plugin(
    session: Session, user: Callable[[ORMExecuteState], Any], option: str
) -> None:
    # With a listener of the user's own the plugin changes nothing: that
    # listener triggers SQLAlchemy issue 13301 without the plugin as well.
    if option == "yield_per":
        run = _stream
    else:

        def run(s: Session) -> list[int]:
            s.add(Line(id=99999, parent_id=1))
            stmt = (
                select(Parent)
                .where(Parent.id == 1)
                .options(selectinload(Parent.lines))
                .execution_options(autoflush=False)
            )
            ids = sorted(line.id for line in s.scalars(stmt).one().lines)
            s.rollback()
            return ids

    without, after_user, before_user = _with_and_without_the_plugin(
        session.get_bind(), user, run
    )
    assert after_user == without
    assert before_user == without


def test_without_events_todo_nothing_is_changed() -> None:
    class State:
        local_execution_options = sqlalchemy.util.immutabledict(
            {"sa_top_level_orm_context": object(), "yield_per": 5}
        )

    state = State()
    before = state.local_execution_options
    listeners._on_orm_execute_heal(state)
    assert state.local_execution_options is before


def test_a_bug_in_the_heal_is_reported_not_raised() -> None:
    class Broken:
        @property
        def local_execution_options(self) -> Any:
            raise RuntimeError("boom")

    saved = listeners.swap_error(None)
    try:
        listeners._on_orm_execute_heal(Broken())
        assert isinstance(listeners._error, RuntimeError)
    finally:
        listeners.swap_error(saved)


def test_the_default_listeners_have_no_do_orm_execute_listener() -> None:
    listeners.install()
    assert not _own_orm_listeners()


def test_the_heal_listener_is_registered_only_where_needed(eager_check: None) -> None:
    registered = event.contains(
        Session, "do_orm_execute", listeners._on_orm_execute_heal
    )
    assert registered == LEAKS


def test_the_heal_listener_goes_with_the_eager_check() -> None:
    listeners.install()
    eager.enable()
    eager.disable()
    assert not event.contains(Session, "do_orm_execute", listeners._on_orm_execute_heal)


@pytest.mark.parametrize(
    ("version", "affected"),
    [
        ("2.0.13", False),
        ("2.0.19", False),
        ("2.0.20", True),
        ("2.0.49", True),
        ("2.0.50", False),
        ("2.0.54", False),
        ("2.1.0", False),
        ("2.1.0b1", True),
        ("2.1.0b2", True),
        ("2.1.0b3", False),
        ("2.1.0rc1", False),
        ("2.0.20rc1", True),
        ("garbage", False),
    ],
)
def test_affected_versions(
    monkeypatch: pytest.MonkeyPatch, version: str, affected: bool
) -> None:
    monkeypatch.setattr(sqlalchemy, "__version__", version)
    assert listeners._leaks_load_options() is affected


@pytest.mark.skipif(not LEAKS, reason="SQLAlchemy does not leak load options")
def test_without_the_heal_listener_sqlalchemy_still_raises(
    session: Session, eager_check: None
) -> None:
    # Proves the tests above exercise the bug: remove the heal listener and the
    # leak is back.
    event.remove(Session, "do_orm_execute", listeners._on_orm_execute_heal)
    try:
        with pytest.raises(sqlalchemy.exc.InvalidRequestError, match=UNIQUE_ERROR):
            _stream(session)
    finally:
        event.listen(Session, "do_orm_execute", listeners._on_orm_execute_heal)
