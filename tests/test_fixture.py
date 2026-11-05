"""The ``queryguard`` fixture: budgets that count the test's own thread or task."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, create_engine, text

from pytest_queryguard import (
    NPlusOneDetected,
    QueryBudgetExceeded,
    QueryGuardFixture,
    QueryLog,
    max_queries,
)
from tests.models import Order, make_sync_session

# The outer plugin measures these tests too; some cause N+1 on purpose.
pytestmark = pytest.mark.allow_nplusone

MODELS = (Path(__file__).parent / "models.py").read_text()


@pytest.fixture
def conn() -> Iterator[Connection]:
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        yield connection
    engine.dispose()


def test_max_queries_passes_at_the_limit(
    queryguard: QueryGuardFixture, conn: Connection
) -> None:
    with queryguard.max_queries(2) as log:
        conn.execute(text("select 1"))
        conn.execute(text("select 2"))
    assert isinstance(log, QueryLog)
    assert [q.sql for q in log] == ["select 1", "select 2"]


def test_max_queries_fails_over_the_limit_with_the_statements(
    queryguard: QueryGuardFixture, conn: Connection
) -> None:
    with pytest.raises(QueryBudgetExceeded) as info, queryguard.max_queries(1):
        conn.execute(text("select 1"))
        conn.execute(text("select 2"))
    message = str(info.value)
    assert message.startswith("queryguard: expected at most 1 query, got 2\n")
    assert "1. select 1" in message
    assert "2. select 2" in message
    assert "test_fixture.py:" in message


def test_num_queries_is_exact(queryguard: QueryGuardFixture, conn: Connection) -> None:
    with queryguard.num_queries(1):
        conn.execute(text("select 1"))
    fewer = "expected exactly 2 queries, got 1"
    with pytest.raises(QueryBudgetExceeded, match=fewer), queryguard.num_queries(2):
        conn.execute(text("select 1"))
    more = "expected exactly 0 queries, got 1"
    with pytest.raises(QueryBudgetExceeded, match=more), queryguard.num_queries(0):
        conn.execute(text("select 1"))
    with queryguard.num_queries(0):
        pass


def test_num_queries_fails_with_none_without_a_blank_tail(
    queryguard: QueryGuardFixture,
) -> None:
    with pytest.raises(QueryBudgetExceeded) as info, queryguard.num_queries(1):
        pass
    assert str(info.value) == "queryguard: expected exactly 1 query, got 0"


@pytest.mark.parametrize("value", [-1, 1.5, True, "2", None])
def test_rejects_a_bad_count(queryguard: QueryGuardFixture, value: object) -> None:
    with pytest.raises(ValueError, match="limit must be a non-negative integer"):
        queryguard.max_queries(value)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="expected must be a non-negative integer"):
        queryguard.num_queries(value)  # type: ignore[arg-type]


def test_statements_of_another_thread_do_not_count(
    queryguard: QueryGuardFixture,
) -> None:
    engine = create_engine("sqlite://")

    def work() -> None:
        with engine.connect() as other:
            other.execute(text("select 'thread'"))

    with max_queries(10) as everything, queryguard.max_queries(1) as mine:
        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
        with engine.connect() as c:
            c.execute(text("select 'test'"))
    assert [q.sql for q in mine] == ["select 'test'"]
    assert {q.sql for q in everything} == {"select 'thread'", "select 'test'"}
    assert [q.sql for q in queryguard.queries] == ["select 'test'"]
    engine.dispose()


def test_nested_blocks_each_count_their_own(
    queryguard: QueryGuardFixture, conn: Connection
) -> None:
    with queryguard.max_queries(3) as outer:
        conn.execute(text("select 1"))
        with queryguard.num_queries(1) as inner:
            conn.execute(text("select 2"))
        conn.execute(text("select 3"))
    assert inner.count == 1
    assert outer.count == 3
    assert queryguard.count == 3


def test_an_exception_in_the_block_wins_and_the_block_ends(
    queryguard: QueryGuardFixture, conn: Connection
) -> None:
    with pytest.raises(KeyError), queryguard.max_queries(0) as log:
        conn.execute(text("select 1"))
        raise KeyError("x")
    conn.execute(text("select 2"))  # after the block: not in its log
    assert [q.sql for q in log] == ["select 1"]
    assert queryguard.count == 2


def test_queries_grow_through_the_test(
    queryguard: QueryGuardFixture, conn: Connection
) -> None:
    assert queryguard.count == 0
    assert queryguard.queries == ()
    conn.execute(text("select 1"))
    assert queryguard.count == 1
    conn.execute(text("select 2"))
    assert [q.sql for q in queryguard.queries] == ["select 1", "select 2"]
    assert repr(queryguard) == "<QueryGuardFixture 2 queries>"


def test_tasks_started_before_the_block_do_not_count(
    queryguard: QueryGuardFixture, conn: Connection
) -> None:
    async def background(go: asyncio.Event) -> None:
        await go.wait()
        conn.execute(text("select 'background'"))

    async def main() -> QueryLog:
        go = asyncio.Event()
        task = asyncio.create_task(background(go))
        await asyncio.sleep(0)
        with queryguard.max_queries(1) as log:
            go.set()
            await task  # runs the background statement inside the block
            conn.execute(text("select 'test'"))
        return log

    # asyncio.run gives its task a copy of the test's context: it counts
    log = asyncio.run(main())
    assert [q.sql for q in log] == ["select 'test'"]
    assert [q.sql for q in queryguard.queries] == [
        "select 'background'",
        "select 'test'",
    ]


def test_async_engine_statements_count(queryguard: QueryGuardFixture) -> None:
    pytest.importorskip("greenlet")
    pytest.importorskip("aiosqlite")
    from sqlalchemy.ext.asyncio import create_async_engine

    async def main() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.connect() as c:
            with queryguard.num_queries(2):
                await c.execute(text("select 1"))
                await c.execute(text("select 2"))
        await engine.dispose()

    asyncio.run(main())


def test_block_log_sees_lazy_loads(queryguard: QueryGuardFixture) -> None:
    session = make_sync_session()
    with queryguard.max_queries(10) as log:
        orders = session.query(Order).order_by(Order.id).all()
        for order in orders:
            assert order.customer.name
    assert {q.relationship for q in log} >= {"Order.customer"}
    with pytest.raises(NPlusOneDetected, match="Order.customer"):
        log.assert_no_nplusone()


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS)
    return pytester


def test_setup_and_teardown_statements_are_not_counted(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a="""
        import pytest
        from sqlalchemy import create_engine, text

        engine = create_engine("sqlite://")


        @pytest.fixture
        def seeded():
            with engine.connect() as c:
                c.execute(text("select 'setup'"))
            yield
            with engine.connect() as c:
                c.execute(text("select 'teardown'"))


        def test_it(queryguard, seeded):
            with engine.connect() as c:
                c.execute(text("select 'test'"))
            assert [q.sql for q in queryguard.queries] == ["select 'test'"]


        def test_listed_after(seeded, queryguard):
            assert queryguard.count == 0


        @pytest.fixture
        def after(queryguard):
            yield
            # torn down after seeded: its teardown statement has run
            assert queryguard.count == 1


        def test_teardown(after, queryguard, seeded):
            with engine.connect() as c:
                c.execute(text("select 'test'"))
        """
    )
    project.runpytest().assert_outcomes(passed=3)


def test_failure_report_and_mode_off(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        from sqlalchemy import create_engine, text


        def test_it(queryguard):
            engine = create_engine("sqlite://")
            with engine.connect() as c:
                with queryguard.max_queries(1):
                    c.execute(text("select 1"))
                    c.execute(text("select 2"))
        """
    )
    for args in ((), ("--queryguard-mode=off",)):
        result = project.runpytest(*args)
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines(
            [
                "*QueryBudgetExceeded: queryguard: expected at most 1 query, got 2",
                "*1. select 1  (test_a.py:8 in test_it)",
                "*2. select 2  (test_a.py:9 in test_it)",
            ]
        )


def test_strict_mode_raises_inside_a_block(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        from qgmodels import Order, make_sync_session


        def test_it(queryguard):
            session = make_sync_session()
            with queryguard.max_queries(100):
                for order in session.query(Order).all():
                    order.customer.name
        """
    )
    result = project.runpytest("--queryguard-strict")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*NPlusOneDetected*"])
    result.stdout.no_fnmatch_line("*QueryBudgetExceeded*")


def test_block_log_sees_lazy_loads_in_off_mode(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        import pytest
        from pytest_queryguard import NPlusOneDetected
        from qgmodels import Order, make_sync_session


        def test_it(queryguard):
            session = make_sync_session()
            with queryguard.max_queries(10) as log:
                for order in session.query(Order).all():
                    order.customer.name
            assert "Order.customer" in {q.relationship for q in log}
            with pytest.raises(NPlusOneDetected):
                log.assert_no_nplusone()
        """
    )
    project.runpytest("--queryguard-mode=off").assert_outcomes(passed=1)


def test_requested_from_the_test_function(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        from sqlalchemy import create_engine, text


        def test_it(request):
            engine = create_engine("sqlite://")
            with engine.connect() as c:
                c.execute(text("select 'before'"))
                guard = request.getfixturevalue("queryguard")
                c.execute(text("select 'after'"))
            assert [q.sql for q in guard.queries] == ["select 'after'"]
        """
    )
    project.runpytest().assert_outcomes(passed=1)


def test_exact_budget_gives_no_verdict_after_an_internal_error(
    queryguard: QueryGuardFixture, conn: Connection
) -> None:
    from pytest_queryguard import listeners

    previous = listeners.swap_error(RuntimeError("boom"))
    try:
        with queryguard.num_queries(3):
            conn.execute(text("select 1"))  # not recorded
    finally:
        listeners.swap_error(previous)
