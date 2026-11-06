# Recipe: FastAPI

FastAPI applications get their session from a dependency. Tests replace it
with `app.dependency_overrides`, so each request runs on a session bound to a
test database, and pytest-queryguard watches the SQL of that session. The
project below is in
[examples/fastapi-dependency-override](https://github.com/MaciejZiel/pytest-queryguard/tree/main/examples/fastapi-dependency-override);
a test runs it and checks that its README shows the output you see here
(FastAPI 0.143, httpx 0.28).

## The application

`app/main.py` has two endpoints per style, one with an N+1 and one fixed:

```python
from collections.abc import AsyncIterator, Iterator
from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db import async_sessions, sessions
from app.models import Customer, Order

app = FastAPI()


def get_session() -> Iterator[Session]:
    with sessions() as session:
        yield session


async def get_async_session() -> AsyncIterator[AsyncSession]:
    async with async_sessions() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
AsyncSessionDep = Annotated[AsyncSession, Depends(get_async_session)]


@app.get("/orders")
def list_orders(session: SessionDep) -> list[str]:
    names = []
    for order in session.scalars(select(Order)):
        names.append(order.customer.name)
    return names


@app.get("/orders-fixed")
def list_orders_fixed(session: SessionDep) -> list[str]:
    stmt = select(Order).options(joinedload(Order.customer))
    return [order.customer.name for order in session.scalars(stmt)]


@app.get("/async-orders")
async def list_async_orders(
    session: AsyncSessionDep,
) -> list[str]:
    names = []
    for order in (await session.scalars(select(Order))).all():
        names.append((await order.awaitable_attrs.customer).name)
    return names


@app.get("/async-orders-fixed")
async def list_async_orders_fixed(
    session: AsyncSessionDep,
) -> list[str]:
    stmt = select(Order).options(selectinload(Order.customer))
    return [order.customer.name for order in (await session.scalars(stmt)).all()]


@app.get("/orders/{order_id}")
def order_detail(order_id: int, session: SessionDep) -> dict[str, object]:
    order = session.get_one(Order, order_id)
    customer = session.get_one(Customer, order.customer_id)
    siblings = session.scalar(
        select(func.count()).select_from(Order).where(Order.customer_id == customer.id)
    )
    audit = session.scalar(select(func.count()).select_from(Order))
    return {"customer": customer.name, "orders": siblings, "total": audit}
```

## The tests

`tests/conftest.py` replaces `get_session` and `get_async_session`. Both use
an in-memory SQLite database:

```python
from collections.abc import AsyncIterator, Iterator

import pytest
from app.main import app, get_async_session, get_session
from app.models import Base, Customer, Order
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def seed(session: Session) -> None:
    session.add_all(Customer(id=i, name=f"customer {i}") for i in range(1, 6))
    session.add_all(Order(id=i, customer_id=i) for i in range(1, 6))
    session.commit()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """The sync dependency replaced by a session on an in-memory database."""
    # sync endpoints run in a worker thread, so SQLite must allow other threads
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as session:
        seed(session)

    def override() -> Iterator[Session]:
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture
async def async_app() -> AsyncIterator[None]:
    """The async dependency replaced by a session on an in-memory database."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add_all(Customer(id=i, name=f"customer {i}") for i in range(1, 6))
        session.add_all(Order(id=i, customer_id=i) for i in range(1, 6))
        await session.commit()

    async def override() -> AsyncIterator[object]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_async_session] = override
    yield
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.fixture
def anyio_backend() -> str:
    # aiosqlite needs asyncio; without this anyio also tries trio when installed
    return "asyncio"
```

Two things in it are easy to get wrong:

- A sync endpoint runs in a worker thread, so an in-memory SQLite engine needs
  `connect_args={"check_same_thread": False}` together with `StaticPool`.
  Without it SQLite raises `ProgrammingError: SQLite objects created in a
  thread can only be used in that same thread`, and the test fails with that
  error and not with a finding.
- The `anyio_backend` fixture pins anyio's pytest plugin to asyncio, because
  aiosqlite does not run on trio.

`tests/test_orders.py` calls the broken endpoints, one with the sync
`TestClient` and one with `httpx.AsyncClient` in an `async def` test:

```python
import pytest
from app.main import app
from httpx import ASGITransport, AsyncClient


def test_orders_with_test_client(client):
    response = client.get("/orders")
    assert response.status_code == 200
    assert len(response.json()) == 5


@pytest.mark.anyio
async def test_orders_with_async_client(async_app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/async-orders")
    assert response.status_code == 200
    assert len(response.json()) == 5
```

Both fail. Queries are counted from any thread, so the `TestClient` worker
thread and the event loop of the async client are both seen. The findings
point at the line in the endpoint, not at the test:

```
queryguard: N+1 detected

  Order.customer was lazy-loaded 5 times
  first at app/main.py:33 in list_orders
  parents loaded at app/main.py:32 in list_orders
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
```

```
queryguard: N+1 detected

  Order.customer was lazy-loaded 5 times
  first at app/main.py:49 in list_async_orders
  parents loaded at app/main.py:48 in list_async_orders
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
```

`tests/test_orders_fixed.py` calls `/orders-fixed` and `/async-orders-fixed`,
which use `joinedload` and `selectinload`, and passes.

## Several requests in one test

By default the repeated-statement rule counts a statement across the whole
test body, not per request. A test that calls a fine endpoint in a loop is
therefore reported, although each request sends only a few queries. With a
detail endpoint that sends four queries per call (each request has its own
session, from the dependency), a test like this, in
`tests/test_loop.py`:

```python
def test_every_order_page_loads(client):
    for order_id in range(1, 6):
        assert client.get(f"/orders/{order_id}").status_code == 200
    assert client.get("/orders/1").status_code == 200
```

fails with one finding for each of the four statements, six runs each:

```
F                                                                        [100%]
=================================== FAILURES ===================================
_________________________ test_every_order_page_loads __________________________
queryguard: N+1 detected

  the same statement ran 6 times across 6 different Sessions or connection checkouts
  first at app/main.py:63 in order_detail
  kind: repeated-statement  label: SELECT orders.id, orders.customer_id FROM orders WHERE orders.id = ?
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#repeated-statement
  note: if each belongs to a separate request or unit of work, this is not a
  loop; set queryguard_scope = session to count each Session on its
  own (QueryLog.assert_no_nplusone: scope='session')
  fix: fetch all rows in one query (WHERE id IN (...)) instead of in a loop

  the same statement ran 6 times across 6 different Sessions or connection checkouts
  first at app/main.py:64 in order_detail
  kind: repeated-statement  label: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#repeated-statement
  note: if each belongs to a separate request or unit of work, this is not a
  loop; set queryguard_scope = session to count each Session on its
  own (QueryLog.assert_no_nplusone: scope='session')
  fix: fetch all rows in one query (WHERE id IN (...)) instead of in a loop

  the same statement ran 6 times across 6 different Sessions or connection checkouts
  first at app/main.py:65 in order_detail
  kind: repeated-statement  label: SELECT count(*) AS count_1 FROM orders WHERE orders.customer_id = ?
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#repeated-statement
  note: if each belongs to a separate request or unit of work, this is not a
  loop; set queryguard_scope = session to count each Session on its
  own (QueryLog.assert_no_nplusone: scope='session')
  fix: fetch all rows in one query (WHERE id IN (...)) instead of in a loop

  the same statement ran 6 times across 6 different Sessions or connection checkouts
  first at app/main.py:68 in order_detail
  kind: repeated-statement  label: SELECT count(*) AS count_1 FROM orders
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#repeated-statement
  note: if each belongs to a separate request or unit of work, this is not a
  loop; set queryguard_scope = session to count each Session on its
  own (QueryLog.assert_no_nplusone: scope='session')
  fix: fetch all rows in one query (WHERE id IN (...)) instead of in a loop
=========================== short test summary info ============================
FAILED tests/test_loop.py::test_every_order_page_loads - Failed: queryguard: ...
1 failed in 0.09s
```

The first call site is the endpoint, which is where the statement is written,
and the report says that the runs were spread over six sessions. Because every
request used its own `Session`, this is not a loop in the endpoint. Setting
`queryguard_scope = session` makes the rules count each session on its
own, and the same test then passes (run for this page; see
[Several sessions or requests](several-sessions.md) for what the option does
and does not change). Other ways out: write one request per test with
`pytest.mark.parametrize` over the ids, or mark the test with
`@pytest.mark.queryguard_ignore` (see [Ignoring findings](ignoring.md)). A
`with max_queries(4):` around each request does not help: it checks its own
block, and the repeated-statement rule still looks at the whole test.
