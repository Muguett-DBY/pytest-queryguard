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
    # --- many tasks and threads, each with its own Session
    "gather_of_async_sessions": """
from sqlalchemy.ext.asyncio import AsyncSession

from pytest_queryguard import NPlusOneDetected


async def _seeded(path):
    engine = await _file_engine(path)
    async with AsyncSession(engine) as session:
        session.add_all([Customer(id=c, name=str(c)) for c in range(1, 4)])
        session.add_all([Order(id=o, customer_id=1 + o % 3) for o in range(6)])
        await session.commit()
    return engine


async def _unit(engine):
    async with AsyncSession(engine) as session:
        orders = (await session.scalars(select(Order))).all()
        for order in orders:  # a lazy load per order, in the greenlet
            await session.run_sync(lambda _, order=order: order.customer)


def test_it(tmp_path):
    async def main():
        engine = await _seeded(tmp_path / "db.sqlite")
        with record_queries() as log:
            await asyncio.gather(*(_unit(engine) for _ in range(40)))
        await engine.dispose()
        return log

    log = asyncio.run(main())
    # one SELECT of the orders and three lazy loads (one per customer) each
    assert log.count == 40 * 4
    with pytest.raises(NPlusOneDetected, match="lazy-loaded 3 times in one Session"):
        log.assert_no_nplusone(scope="session")
    with pytest.raises(NPlusOneDetected, match="120 times across 40 different"):
        log.assert_no_nplusone()
    assert listeners.internal_error() is None
""",
    "tasks_and_ignore_blocks": """
from sqlalchemy.ext.asyncio import AsyncSession

from pytest_queryguard import NPlusOneDetected, ignore


async def _seeded(path):
    engine = await _file_engine(path)
    async with AsyncSession(engine) as session:
        session.add_all([Customer(id=c, name=str(c)) for c in range(1, 4)])
        session.add_all([Order(id=o, customer_id=1 + o % 3) for o in range(6)])
        await session.commit()
    return engine


async def _unit(engine, go):
    await go.wait()
    async with AsyncSession(engine) as session:
        for order in (await session.scalars(select(Order))).all():
            await session.run_sync(lambda _, order=order: order.customer)


def test_it(tmp_path):
    async def main():
        engine = await _seeded(tmp_path / "db.sqlite")
        go = asyncio.Event()
        before = asyncio.create_task(_unit(engine, go))
        await asyncio.sleep(0)  # it runs up to the wait, outside the block
        with record_queries() as early:
            with ignore("relationship-load"):
                go.set()
                await before
        go = asyncio.Event()
        with ignore("relationship-load"):
            inside = asyncio.create_task(_unit(engine, go))
        with record_queries() as late:
            go.set()  # after the block: the task keeps its copy of the context
            await inside
        await engine.dispose()
        return early, late

    early, late = asyncio.run(main())
    # created before the block: its context never had the block
    with pytest.raises(NPlusOneDetected, match="Order.customer"):
        early.assert_no_nplusone()
    late.assert_no_nplusone()  # created inside it: covered until it ends
""",
    "record_queries_in_many_threads": """
def test_it():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    lock = threading.Lock()  # one sqlite connection: one statement at a time

    def work(i):
        counts = []
        for j in range(50):
            with record_queries() as log:
                with lock, engine.connect() as conn:
                    conn.execute(text(f"SELECT {i} + {j}"))
            counts.append(log.count)
        return counts

    with record_queries() as outer:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(work, range(8)))
    # each block saw its own statement, and any others that ran meanwhile
    assert all(count >= 1 for counts in results for count in counts)
    assert outer.count == 8 * 50
    from pytest_queryguard import collector

    assert outer._collector not in collector.active()
    assert len(collector.active()) == 1  # only the test's own measurement is left
    assert listeners.internal_error() is None
""",
    "threaded_wsgi_server": """
import urllib.request
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server


class _Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def url(tmp_path_factory):
    path = tmp_path_factory.mktemp("srv") / "db.sqlite"
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Customer(id=c, name=str(c)) for c in range(1, 4)])
        session.add_all([Order(id=o, customer_id=1 + o % 3) for o in range(6)])
        session.commit()

    def app(environ, start_response):
        with Session(engine) as session:
            names = [o.customer.name for o in session.scalars(select(Order))]
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [",".join(names).encode()]

    server = make_server("127.0.0.1", 0, app, server_class=_Server, handler_class=_Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    thread.join(10)
    engine.dispose()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read()


def test_it(url):
    with record_queries() as log:
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert len(set(pool.map(lambda _: _get(url), range(16)))) == 1
    # the server's request threads count, each request in its own Session
    assert log.count == 16 * 4
    with pytest.raises(Exception, match="48 times across 16 different Sessions"):
        log.assert_no_nplusone()
    with pytest.raises(Exception, match="3 times in one Session"):
        log.assert_no_nplusone(scope="session")
""",
    "static_pool_sessions_in_threads": """
def test_it():
    engine = make_sync_session().get_bind()  # StaticPool: one shared connection
    lock = threading.Lock()  # sqlite: one thread at a time on it

    def work(_):
        with lock, Session(engine) as session:
            for order in session.scalars(select(Order)).all():
                _ = order.customer

    with record_queries() as log:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(work, range(12)))
    loads = [q for q in log if q.relationship == "Order.customer"]
    assert len(loads) == 12 * 3
    # each Session is its own scope although they share one connection
    with pytest.raises(Exception, match="3 times in one Session"):
        log.assert_no_nplusone(scope="session")
""",
    "queryguard_fixture_with_threads": """
import contextvars


def test_it(queryguard):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    lock = threading.Lock()

    def work(i):
        with lock, engine.connect() as conn:
            conn.execute(text(f"SELECT {i}"))

    # one worker, started outside the block: no thread is created inside it
    # (on 3.14t a new thread would inherit the block's context)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(work, -1).result()
        with queryguard.num_queries(0):
            list(pool.map(work, range(10)))  # their context is not the block's
        with queryguard.num_queries(10):
            for future in [
                pool.submit(contextvars.copy_context().run, work, i)
                for i in range(10)
            ]:
                future.result()

    async def to_thread():
        await asyncio.gather(*(asyncio.to_thread(work, i) for i in range(5)))

    with queryguard.num_queries(5):
        asyncio.run(to_thread())
    assert queryguard.count == 1 + 10 + 10 + 5
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
        "gather_of_async_sessions",
        "tasks_and_ignore_blocks",
        "record_queries_in_many_threads",
        "threaded_wsgi_server",
        "static_pool_sessions_in_threads",
        "queryguard_fixture_with_threads",
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


# ------------------------------------- another measurement starts mid-test

ORIGINS = """
import threading

import pytest
from sqlalchemy import select, text
from pytest_queryguard import max_queries, record_queries
from qgmodels import Order, make_sync_session


def _parents():
    session = make_sync_session()
    return session, session.scalars(select(Order)).all()  # the parents' query


def test_block_in_another_thread():
    session, orders = _parents()

    def other():
        with record_queries():
            pass

    thread = threading.Thread(target=other)
    thread.start()
    thread.join(10)
    for order in orders:
        _ = order.customer.name  # the access


def test_after_a_block():
    session, orders = _parents()
    with record_queries():
        session.execute(text("select 1"))
    for order in orders:
        _ = order.customer.name  # the access


def test_inside_a_budget_block():
    session, orders = _parents()
    with max_queries(100):
        for order in orders:
            _ = order.customer.name  # the access


def test_reread_inside_a_block():
    session, orders = _parents()
    session.expire_all()
    with max_queries(100):
        for order in session.scalars(select(Order)).all():  # the block's read
            _ = order.customer.name  # the access


@pytest.fixture(scope="module")
def shared():
    with record_queries():  # a block open across the tests of the module
        yield _parents()


def test_shared_first(shared):
    pass


def test_shared_second(shared):
    session, _ = shared
    session.expire_all()
    for order in session.scalars(select(Order)).all():  # this test's read
        _ = order.customer.name  # the access


@pytest.mark.allow_nplusone
def test_a_block_has_only_its_own_origins():
    session, orders = _parents()
    with record_queries() as log:
        for order in orders:
            for other in order.customer.orders:  # loaded through a parent
                _ = other.customer
        session.expunge_all()
        for order in session.scalars(select(Order)).all():
            _ = order.customer.name
    sites = [
        (load.relationship, load.parent is not None and load.parent.callsite is not None)
        for load in log._collector.relationship_loads
    ]
    # neither the orders read before the block, nor the customers lazy-loaded
    # from them (whose site would come from before the block), have one there
    assert sorted(sites[:6]) == [("Customer.orders", False)] * 3 + [
        ("Order.customer", False)
    ] * 3
    assert sites[6:] == [("Order.customer", True)] * 3
"""


@pytest.mark.parametrize(
    "name", ["block_in_another_thread", "after_a_block", "inside_a_budget_block"]
)
def test_a_measurement_started_elsewhere_keeps_the_tests_parent_sites(
    project: pytest.Pytester, name: str
) -> None:
    """The parents' origins stay the test's while a block starts or ends.

    A record_queries() or max_queries() block, in the test or in any other
    thread, used to invalidate every origin noted before it, so strict mode
    no longer raised at the access and the report lost "parents loaded at".
    """
    project.makepyfile(test_origins=ORIGINS)
    result = project.runpytest_subprocess(
        "-p", "no:randomly", "--queryguard-strict", "-k", name, timeout=300
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            ">*_ = order.customer.name  # the access",
            "E*NPlusOneDetected: queryguard strict: Order.customer*",
            "E*parents loaded at test_origins.py:* in _parents",
        ]
    )


@pytest.mark.parametrize(
    ("name", "where"),
    [
        ("reread_inside_a_block", "test_reread_inside_a_block"),
        ("shared_second", "test_shared_second"),
    ],
)
def test_a_read_after_an_earlier_origin_is_the_parents_origin(
    project: pytest.Pytester, name: str, where: str
) -> None:
    """A new read of objects loaded earlier (before a block, or in an earlier
    test while a longer block is open) is where their parents come from now."""
    project.makepyfile(test_origins=ORIGINS)
    result = project.runpytest_subprocess(
        "-p", "no:randomly", "--queryguard-strict", "-k", name, timeout=300
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "E*NPlusOneDetected: queryguard strict: Order.customer*",
            f"E*parents loaded at test_origins.py:* in {where}",
        ]
    )


def test_a_block_does_not_use_the_origins_noted_before_it(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(test_origins=ORIGINS)
    result = project.runpytest_subprocess(
        "-p", "no:randomly", "-k", "own_origins", timeout=300
    )
    result.assert_outcomes(passed=1)


STRICT_IN_THREADS = """
import threading

import pytest
from sqlalchemy import select
from pytest_queryguard import NPlusOneDetected
from qgmodels import Order, make_sync_session


def _loop(session):
    for order in session.scalars(select(Order)).all():
        _ = order.customer.name  # the access


def test_swallowed_by_the_thread():
    errors = []

    def work():
        try:
            _loop(make_sync_session())
        except NPlusOneDetected as exc:
            errors.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(10)
    assert len(errors) == 1  # raised in the thread that made the access


def test_one_raise_per_thread():
    errors = []

    def work():
        try:
            _loop(make_sync_session())
        except NPlusOneDetected as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(errors) == 8
"""


def test_strict_mode_raises_in_the_thread_and_the_report_still_fails_the_test(
    project: pytest.Pytester,
) -> None:
    """A thread that swallows the strict error does not hide the N+1."""
    project.makepyfile(test_strict_threads=STRICT_IN_THREADS)
    result = project.runpytest_subprocess(
        "-p", "no:randomly", "--queryguard-strict", timeout=300
    )
    # the in-test asserts pass; each test then fails with the report
    result.assert_outcomes(failed=2)
    text = result.stdout.str()
    assert "AssertionError" not in text, text[-3000:]
    assert text.count("queryguard: N+1 detected") == 2, text[-3000:]
