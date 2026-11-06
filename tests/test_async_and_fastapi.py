from __future__ import annotations

from pathlib import Path

import pytest

# pytester drops modules first imported by an inner run from sys.modules when
# that run ends. SQLAlchemy 2.1 imports greenlet lazily, inside the first async
# run, and the call site lookup only uses greenlet when sys.modules has it, so
# later async runs lost their call sites. Importing it here keeps it loaded.
greenlet = pytest.importorskip("greenlet")

# pytester runs sessions in-process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone

MODELS = (Path(__file__).parent / "models.py").read_text()

ASYNC_SETUP = """
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import StaticPool

from qgmodels import Base, Customer, Order


@asynccontextmanager
async def make_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session = async_sessionmaker(engine, expire_on_commit=False)()
        for c in range(1, 4):
            customer = Customer(id=c, name=f"customer {c}")
            customer.orders = [Order(id=c * 10 + o) for o in range(2)]
            session.add(customer)
        await session.commit()
        session.expunge_all()
        try:
            yield session
        finally:
            await session.close()
    finally:
        await engine.dispose()
"""

# AsyncAttrs must be on the mapped classes, so the async scenarios use their own models.
ASYNC_MODELS = """
from sqlalchemy import ForeignKey
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    customer: Mapped[Customer] = relationship(back_populates="orders")
"""


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS, qgasync=ASYNC_MODELS)
    return pytester


def _async_scenario(project: pytest.Pytester, body: str) -> Path:
    setup = ASYNC_SETUP.replace("from qgmodels import", "from qgasync import")
    return project.makepyfile(test_a=setup + "\n\n" + body)


def _marked_line(path: Path) -> int:
    """1-based line of the single `# QG_LINE` marker in a scenario file."""
    lines = [
        n
        for n, text in enumerate(path.read_text().splitlines(), start=1)
        if "# QG_LINE" in text
    ]
    assert len(lines) == 1
    return lines[0]


def test_async_session_get_in_loop_fails(project: pytest.Pytester) -> None:
    path = _async_scenario(
        project,
        """
async def main():
    async with make_session() as session:
        for i in range(5):
            await session.get(Customer, i + 1)  # QG_LINE


def test_loop():
    asyncio.run(main())
""",
    )
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*N+1 detected*",
            "*the same statement ran *",
            f"*first at test_a.py:{_marked_line(path)} in main",
        ]
    )


def test_async_in_query_passes(project: pytest.Pytester) -> None:
    _async_scenario(
        project,
        """
async def main():
    async with make_session() as session:
        stmt = select(Customer).where(Customer.id.in_([1, 2, 3]))
        assert len((await session.scalars(stmt)).all()) == 3


def test_ok():
    asyncio.run(main())
""",
    )
    project.runpytest().assert_outcomes(passed=1)


def test_async_awaitable_attrs_relationship_fails(project: pytest.Pytester) -> None:
    path = _async_scenario(
        project,
        """
async def main():
    async with make_session() as session:
        orders = (await session.scalars(select(Order))).all()
        for order in orders:
            customer = await order.awaitable_attrs.customer  # QG_LINE
            assert customer.name


def test_loop():
    asyncio.run(main())
""",
    )
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*N+1 detected*",
            "*Order.customer was lazy-loaded*",
            f"*first at test_a.py:{_marked_line(path)} in main",
        ]
    )


def test_async_selectinload_passes(project: pytest.Pytester) -> None:
    _async_scenario(
        project,
        """
async def main():
    async with make_session() as session:
        stmt = select(Order).options(selectinload(Order.customer))
        for order in (await session.scalars(stmt)).all():
            customer = await order.awaitable_attrs.customer
            assert customer.name


def test_ok():
    asyncio.run(main())
""",
    )
    project.runpytest().assert_outcomes(passed=1)


APP = """
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from qgmodels import Order, make_sync_session

app = FastAPI()
session = make_sync_session()


@app.get("/orders")
def orders_nplusone():
    result = []
    for order in session.query(Order).all():
        result.append(order.customer.name)  # QG_LINE
    return result


@app.get("/fixed")
def orders_fixed():
    stmt = select(Order).options(selectinload(Order.customer))
    return [o.customer.name for o in session.scalars(stmt)]
"""


# Starlette 1.x warns on import of its TestClient when httpx (rather than
# httpx2) is installed. The warning is about the test client, not about the
# plugin, so the inner runs ignore exactly that message. Matching it by text
# and by its base class keeps this working on Starlette versions that do not
# have StarletteDeprecationWarning.
STARLETTE_HTTPX_FILTER = (
    "[pytest]\n"
    "filterwarnings =\n"
    "    ignore:Using `httpx` with `starlette.testclient` is deprecated:UserWarning\n"
)


@pytest.fixture
def fastapi_project(project: pytest.Pytester) -> pytest.Pytester:
    project.makeini(STARLETTE_HTTPX_FILTER)
    return project


def test_fastapi_testclient_endpoint_nplusone_fails(
    fastapi_project: pytest.Pytester,
) -> None:
    project = fastapi_project
    app_path = project.makepyfile(app_module=APP)
    project.makepyfile(
        test_a="""
from fastapi.testclient import TestClient

from app_module import app


def test_endpoint():
    assert TestClient(app).get("/orders").status_code == 200
""",
    )
    result = project.runpytest()
    result.assert_outcomes(failed=1, warnings=0)
    result.stdout.fnmatch_lines(
        [
            "*N+1 detected*",
            "*Order.customer was lazy-loaded*",
            f"*first at app_module.py:{_marked_line(app_path)} in orders_nplusone",
        ]
    )


def test_fastapi_fixed_endpoint_passes(fastapi_project: pytest.Pytester) -> None:
    project = fastapi_project
    project.makepyfile(
        app_module=APP,
        test_a="""
from fastapi.testclient import TestClient

from app_module import app


def test_endpoint():
    assert TestClient(app).get("/fixed").status_code == 200
""",
    )
    project.runpytest().assert_outcomes(passed=1, warnings=0)


def test_fixture_block_counts_testclient_requests(
    fastapi_project: pytest.Pytester,
) -> None:
    # docs/budgets.md: a TestClient request runs in another thread with a copy
    # of the test's context, so it counts in a block of the queryguard fixture,
    # also when the client was opened in a fixture before the block
    project = fastapi_project
    project.makepyfile(
        app_module=APP,
        test_a="""
import pytest
from fastapi.testclient import TestClient

from app_module import app


@pytest.fixture
def client():
    with TestClient(app) as client:
        client.get("/fixed")  # the session's first statements, outside the test
        yield client


def test_opened_in_fixture(queryguard, client):
    with queryguard.num_queries(2):
        assert client.get("/fixed").status_code == 200


def test_opened_in_test(queryguard):
    with queryguard.num_queries(2):
        assert TestClient(app).get("/fixed").status_code == 200
""",
    )
    project.runpytest().assert_outcomes(passed=2, warnings=0)


def test_plain_thread_queries_are_collected(project: pytest.Pytester) -> None:
    path = project.makepyfile(
        test_a="""
import threading

from qgmodels import Order, make_sync_session


def test_thread():
    session = make_sync_session()

    def work():
        for order in session.query(Order).all():
            _ = order.customer.name  # QG_LINE

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
"""
    )
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*Order.customer was lazy-loaded*",
            f"*first at test_a.py:{_marked_line(path)} in work",
        ]
    )


def test_plain_thread_fixed_passes(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
import threading

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from qgmodels import Order, make_sync_session


def test_thread():
    session = make_sync_session()

    def work():
        stmt = select(Order).options(selectinload(Order.customer))
        for order in session.scalars(stmt):
            _ = order.customer.name

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
"""
    )
    project.runpytest().assert_outcomes(passed=1)
