# Recipe: pytest-asyncio configuration pitfalls

pytest-queryguard measures the body of a test, and it does not care which
plugin runs an `async def` test: pytest-asyncio, anyio and others all work.
The failures people hit are configuration errors of the async plugin, and they
look like problems of the database layer. Run with pytest-asyncio 1.4.0, pytest
9.1.1, aiosqlite, and asyncpg 0.31 against PostgreSQL 17 for the last pitfall.
The working setup needs:

```console
pip install pytest-queryguard pytest-asyncio aiosqlite "sqlalchemy[asyncio]"
```

## A working setup

The layout is `app/__init__.py` (an empty file), `app/models.py`,
`app/orders.py`, `tests/conftest.py` and `tests/test_orders.py`.

`pytest.ini`. `pythonpath = .` puts the project directory on `sys.path`, so
that the tests can `import app` (see [getting started](getting-started.md)):

```ini
[pytest]
pythonpath = .
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
```

`app/models.py` uses SQLAlchemy's `AsyncAttrs` mixin, which gives
`awaitable_attrs` for the lazy load below:

```python
from sqlalchemy import ForeignKey
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customer"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    customer: Mapped[Customer] = relationship()
```

`tests/conftest.py` has an async fixture for the session:

```python
import pytest_asyncio
from app.models import Base, Customer, Order
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all(Customer(id=i, name=f"customer {i}") for i in range(1, 6))
        session.add_all(Order(id=i, customer_id=i) for i in range(1, 6))
        await session.commit()
        yield session
    await engine.dispose()
```

`app/orders.py` has an async function with an N+1 and a fixed one:

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Order


async def customer_names(session: AsyncSession) -> list[str]:
    names = []
    for order in (await session.scalars(select(Order))).all():
        names.append((await order.awaitable_attrs.customer).name)
    return names


async def customer_names_fixed(session: AsyncSession) -> list[str]:
    stmt = select(Order).options(selectinload(Order.customer))
    return [order.customer.name for order in (await session.scalars(stmt)).all()]
```

`tests/test_orders.py`:

```python
from app.orders import customer_names, customer_names_fixed


async def test_customer_names(session):
    assert len(await customer_names(session)) == 5


async def test_customer_names_fixed(session):
    assert len(await customer_names_fixed(session)) == 5
```

```
F.                                                                       [100%]
=================================== FAILURES ===================================
_____________________________ test_customer_names ______________________________
queryguard: N+1 detected

  Order.customer was lazy-loaded 5 times
  first at app/orders.py:11 in customer_names
    called from tests/test_orders.py:5 in test_customer_names
  parents loaded at app/orders.py:10 in customer_names
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
=========================== short test summary info ============================
FAILED tests/test_orders.py::test_customer_names - Failed: queryguard: N+1 de...
1 failed, 1 passed in 0.06s
```

Only the test body is measured. Queries in the async fixture (the schema and
the rows) are not counted, as with sync fixtures.

## Pitfall 1: strict mode and `@pytest.fixture`

pytest-asyncio's default mode is `strict`: async tests need
`@pytest.mark.asyncio`, and async fixtures need `@pytest_asyncio.fixture`.
With `asyncio_mode = strict` and the `session` fixture declared with plain
`@pytest.fixture`, pytest reports (output shortened to the relevant lines):

```
'test_customer_names' requested an async fixture 'session', with no plugin or hook that handled it. This is an error, as pytest does not natively support it.
ERROR tests/test_orders.py::test_customer_names - Failed: 'test_customer_name...
ERROR tests/test_orders.py::test_customer_names_fixed - AssertionError
2 errors in 0.10s
```

With the fixture fixed but the tests left without `@pytest.mark.asyncio`:

```
async def functions are not natively supported.
async def functions are not natively supported.
FAILED tests/test_orders.py::test_customer_names - Failed: async def function...
FAILED tests/test_orders.py::test_customer_names_fixed - Failed: async def fu...
2 failed in 0.04s
```

Neither is a queryguard finding. No query ran, so there is nothing to measure.
Use `asyncio_mode = auto`, or mark every test and use
`@pytest_asyncio.fixture`.

## Pitfall 2: an engine shared between event loops

An asyncpg connection belongs to the event loop that opened it. A
session-scoped engine fixture with a function-scoped test loop (pytest-asyncio's
default) hands the tests a connection from a different loop. With the engine
fixture declared as `@pytest_asyncio.fixture(scope="session", loop_scope="session")`
and the tests on the default function loop:

```python
import pytest_asyncio
from app.models import Base, Customer, Order
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

URL = "postgresql+asyncpg://postgres:pg@localhost:55433/postgres"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine():
    engine = create_async_engine(URL)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(Customer.__table__.insert(), [{"id": i, "name": f"c{i}"} for i in range(1, 6)])
        await connection.execute(Order.__table__.insert(), [{"id": i, "customer_id": i} for i in range(1, 6)])
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine):
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
```

the tests fail on PostgreSQL with errors from asyncpg, not with findings:

```
E   RuntimeError: Task <Task pending name='Task-4' coro=<test_customer_names() running at tests/test_orders.py:5> [...]> got Future <Future pending cb=[BaseProtocol._on_waiter_completed()]> attached to a different loop
E   sqlalchemy.exc.InterfaceError: (sqlalchemy.dialects.postgresql.asyncpg.InterfaceError) cannot perform operation: another operation is in progress
FAILED tests/test_orders.py::test_customer_names - RuntimeError: Task <Task p...
FAILED tests/test_orders.py::test_customer_names_fixed - sqlalchemy.exc.Inter...
2 failed in 0.10s
```

Run the tests on the same loop as the fixture:

```ini
[pytest]
pythonpath = .
asyncio_mode = auto
asyncio_default_fixture_loop_scope = session
asyncio_default_test_loop_scope = session
```

With that, the same tests run and the N+1 test fails with its finding:

```
F.                                                                       [100%]
=================================== FAILURES ===================================
_____________________________ test_customer_names ______________________________
queryguard: N+1 detected

  Order.customer was lazy-loaded 5 times
  first at app/orders.py:11 in customer_names
    called from tests/test_orders.py:5 in test_customer_names
  parents loaded at app/orders.py:10 in customer_names
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = $1::INTEGER
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
=========================== short test summary info ============================
FAILED tests/test_orders.py::test_customer_names - Failed: queryguard: N+1 de...
1 failed, 1 passed in 0.10s
```

Without the `loop_scope="session"` argument on the engine fixture pytest-asyncio
stops earlier, with a `ScopeMismatch` error. The same setup with an
aiosqlite engine passed in a run for this page, so the problem can first show
up when a project moves its async tests from SQLite to PostgreSQL.

## Pitfall 3: using the sync `TestClient` in an async test

Starlette's `TestClient` is a sync client. In
an `async def` test, call the app with `httpx.AsyncClient` and
`ASGITransport` instead, as in [the FastAPI recipe](recipes-fastapi.md). Both
styles are measured.
