"""Threads, event loops and cancellation inside a measured test (S3).

Every scenario runs in a pytest session of its own, with the index check on,
and must neither raise an internal error nor lose or double-count statements.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# pytester runs sessions in-process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone

pytest.importorskip("aiosqlite")
pytest.importorskip("greenlet")

MODELS = (Path(__file__).parent / "models.py").read_text()

HEAD = """
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from pytest_queryguard import listeners, record_queries
from qgmodels import Base, Customer, Order, make_sync_session


def lazy_loop():
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name
    session.close()


async def _file_engine(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine
"""

SCENARIOS = {
    # --- threads
    "thread_pool_counts_every_statement": """
def test_it():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    def work(i):
        with engine.connect() as conn:
            for j in range(25):
                conn.execute(text(f"SELECT {i} + {j}"))

    with record_queries() as log:
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(work, range(64)))
    assert log.count == 64 * 25
    assert listeners.internal_error() is None
""",
    "nplusone_from_worker_threads_is_found": """
@pytest.mark.max_queries(1000)
def test_it():
    threads = [threading.Thread(target=lazy_loop) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
""",
    "thread_outlives_the_test": """
STOP = threading.Event()


def test_first():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    def keep_querying():
        with engine.connect() as conn:
            while not STOP.is_set():
                conn.execute(text("SELECT 1"))
                time.sleep(0.001)

    threading.Thread(target=keep_querying, daemon=True).start()
    time.sleep(0.05)


@pytest.mark.max_queries(1000000)
def test_second():
    time.sleep(0.05)  # the thread of test_first runs into this test
    STOP.set()
    time.sleep(0.01)
    assert listeners.internal_error() is None
""",
    # --- async
    "cancelled_mid_statement": """
def test_it(tmp_path):
    async def main():
        engine = await _file_engine(tmp_path / "a.sqlite")
        async with engine.connect() as conn:
            raw = await conn.get_raw_connection()
            await raw.driver_connection.create_function(
                "pause", 1, lambda s: time.sleep(s) or 1
            )
            task = asyncio.create_task(conn.execute(text("SELECT pause(0.5)")))
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        # a fresh connection is measured as usual
        async with engine.connect() as conn:
            with record_queries() as log:
                await conn.execute(select(Customer))
        await engine.dispose()
        return log

    log = asyncio.run(main())
    assert log.count == 1
    assert listeners.internal_error() is None
""",
    "two_event_loops_in_a_row_and_in_threads": """
def test_it(tmp_path):
    async def run(name):
        engine = await _file_engine(tmp_path / f"{name}.sqlite")
        async with engine.connect() as conn:
            for _ in range(10):
                await conn.execute(select(Customer))
        await engine.dispose()

    with record_queries() as log:
        asyncio.run(run("first"))
        asyncio.run(run("second"))
        threads = [
            threading.Thread(target=asyncio.run, args=(run(f"t{i}"),)) for i in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    selects = [q for q in log if q.sql.startswith("SELECT customers")]
    assert len(selects) == 6 * 10
    assert listeners.internal_error() is None
""",
    "anyio_backend": """
@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_it(tmp_path):
    engine = await _file_engine(tmp_path / "anyio.sqlite")
    with record_queries() as log:
        async with engine.connect() as conn:
            await conn.execute(select(Customer))
    await engine.dispose()
    assert log.count == 1
""",
}


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS)
    return pytester


def _run(project: pytest.Pytester, name: str, *args: str) -> pytest.RunResult:
    project.makepyfile(**{f"test_{name}": HEAD + SCENARIOS[name]})
    # warn mode: the scenarios repeat statements on purpose
    result = project.runpytest_subprocess(
        "-p",
        "no:randomly",
        "--queryguard-index-check",
        "--queryguard-mode=warn",
        *args,
        timeout=300,
    )
    output = result.stdout.str() + result.stderr.str()
    assert "INTERNALERROR" not in output
    assert "measurement stopped" not in output, output[-3000:]
    assert "QueryGuardInternalWarning" not in output, output[-3000:]
    return result


@pytest.mark.parametrize(
    "name",
    [
        "thread_pool_counts_every_statement",
        "thread_outlives_the_test",
        "cancelled_mid_statement",
        "two_event_loops_in_a_row_and_in_threads",
        "anyio_backend",
    ],
)
def test_concurrent_statements_are_measured_without_errors(
    project: pytest.Pytester, name: str
) -> None:
    result = _run(project, name)
    outcomes = result.parseoutcomes()
    assert outcomes.get("failed", 0) == 0 and outcomes.get("passed", 0) >= 1, (
        result.stdout.str()[-3000:]
    )


def test_an_nplusone_in_worker_threads_is_reported(project: pytest.Pytester) -> None:
    result = _run(
        project, "nplusone_from_worker_threads_is_found", "--queryguard-mode=fail"
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Order.customer was lazy-loaded 24 times*"])


# ------------------------------------------------- pytest-asyncio (plugins job)

ASYNCIO_TESTS = """
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from qgmodels import Base, Customer, Order


@pytest_asyncio.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as session:
        session.add_all([Customer(id=i, name=str(i)) for i in range(1, 4)])
        session.add_all([Order(id=i, customer_id=1 + i % 3) for i in range(6)])
        await session.commit()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


{marker}
async def test_nplusone(session):
    for order in (await session.scalars(select(Order))).all():
        await order.awaitable_attrs.customer


{marker}
async def test_clean(session):
    await session.get(Customer, 1)
"""


@pytest.mark.parametrize("mode", ["strict", "auto"])
def test_pytest_asyncio_modes(project: pytest.Pytester, mode: str) -> None:
    pytest.importorskip("pytest_asyncio")
    project.makepyfile(
        qgasync_models=MODELS.replace(
            "class Base(DeclarativeBase)", "class Base(AsyncAttrs, DeclarativeBase)"
        ).replace(
            "from sqlalchemy.orm import (",
            "from sqlalchemy.ext.asyncio import AsyncAttrs\nfrom sqlalchemy.orm import (",
        )
    )
    marker = "@pytest.mark.asyncio" if mode == "strict" else ""
    project.makepyfile(
        test_async=ASYNCIO_TESTS.replace("{marker}", marker).replace(
            "from qgmodels import", "from qgasync_models import"
        )
    )
    project.makeini(
        f"[pytest]\nasyncio_mode = {mode}\nasyncio_default_fixture_loop_scope = function\n"
    )
    result = project.runpytest_subprocess("-p", "no:randomly", timeout=300)
    output = result.stdout.str()
    assert "QueryGuardInternalWarning" not in output, output[-3000:]
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*test_nplusone*", "*N+1 detected*"])


def test_queryguard_fixture_in_pytest_asyncio_tests(project: pytest.Pytester) -> None:
    """The test's task carries the fixture's count; an async fixture's does not."""
    pytest.importorskip("pytest_asyncio")
    project.makepyfile(
        test_async="""
        import pytest
        import pytest_asyncio
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine


        @pytest_asyncio.fixture
        async def conn():
            engine = create_async_engine("sqlite+aiosqlite://")
            async with engine.connect() as c:
                await c.execute(text("select 'setup'"))
                yield c
            await engine.dispose()


        @pytest.mark.asyncio
        async def test_it(queryguard, conn):
            with queryguard.num_queries(1) as log:
                await conn.execute(text("select 'block'"))
            await conn.execute(text("select 'after'"))
            assert [q.sql for q in log] == ["select 'block'"]
            assert [q.sql for q in queryguard.queries] == [
                "select 'block'",
                "select 'after'",
            ]


        @pytest.mark.asyncio
        async def test_over(queryguard, conn):
            with queryguard.max_queries(0):
                await conn.execute(text("select 'block'"))
        """
    )
    project.makeini(
        "[pytest]\nasyncio_mode = strict\nasyncio_default_fixture_loop_scope = function\n"
    )
    result = project.runpytest_subprocess("-p", "no:randomly", timeout=300)
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*expected at most 0 queries, got 1*"])
