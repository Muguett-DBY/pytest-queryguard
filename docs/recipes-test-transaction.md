# Recipe: a session per request in the test's transaction

A common way to isolate tests is to open one transaction per test, run
everything in it and roll it back at the end. The quickest way to get the
application into that transaction is to override its session dependency with
the test's own `Session`:

```python
app.dependency_overrides[get_session] = lambda: session
```

Then every request of the test shares one `Session`. For pytest-queryguard
the test is one unit of work: a test that calls an endpoint five times runs
the endpoint's lookups five times in one `Session`, and they are reported as
a loop. `queryguard_scope = session` (see
[Tests with several sessions or requests](several-sessions.md)) does not help,
because there is only one `Session` to count. The same override in a suite
with many such tests gives a repeated-statement finding for almost every
lookup an endpoint makes.

The fix is in the fixture, not in the application: give each request its own
`Session`, bound to the test's connection, with
`join_transaction_mode="create_savepoint"`. Each request's session then works
inside the outer transaction (its `commit()` releases a savepoint, the outer
transaction stays open), and the test still rolls everything back at the end.
With `queryguard_scope = session`, each request is counted on its own, so only
a real loop inside one request is reported.

The project below was run for this page with SQLAlchemy 2.0.13 and 2.1.1,
FastAPI 0.141 and httpx 0.28; a test builds it from the files of this page and
runs it. The layout is `pytest.ini`, `app/__init__.py` and `tests/__init__.py`
(both empty), `app/models.py`, `app/main.py`, `tests/conftest.py`,
`tests/test_shared_session.py` and `tests/test_session_per_request.py`. Install
the plugin with FastAPI and httpx:

```console
pip install pytest-queryguard fastapi httpx
```

## The application

`pytest.ini` counts each `Session` on its own. The `filterwarnings` line
hides the warning of Starlette 1.2 and later about its test client finding
`httpx` without `httpx2`; the [FastAPI recipe](recipes-fastapi.md) explains it
and its alternative, installing `httpx2`:

```ini
[pytest]
pythonpath = .
queryguard_scope = session
filterwarnings =
    ignore:Using `httpx` with `starlette.testclient` is deprecated:UserWarning
```

`app/models.py`:

```python
from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
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

`app/main.py` has one endpoint that loads one order and its customer. Each
request runs two statements, which is fine:

```python
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Order

app = FastAPI()
sessions = sessionmaker(create_engine("sqlite:///app.db"))


def get_session() -> Iterator[Session]:
    with sessions() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


@app.get("/orders/{order_id}")
def order_detail(order_id: int, session: SessionDep) -> dict[str, str]:
    order = session.scalars(select(Order).filter_by(id=order_id)).one()
    return {"customer": order.customer.name}
```

## The fixtures

`tests/conftest.py` opens one connection and one transaction per test, seeds
it and rolls it back at the end. `shared_client` hands every request the same
`Session`; `client` gives each request a new one in the same transaction:

```python
from collections.abc import Iterator

import pytest
from app.main import app, get_session
from app.models import Base, Customer, Order
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite only: let SQLAlchemy emit BEGIN, so that SAVEPOINT works
    @event.listens_for(engine, "connect")
    def _no_driver_transactions(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(connection):
        connection.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    """One transaction per test, rolled back at the end."""
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(connection, join_transaction_mode="create_savepoint") as seed:
            seed.add_all(Customer(id=i, name=f"customer {i}") for i in range(1, 6))
            seed.add_all(Order(id=i, customer_id=i) for i in range(1, 6))
            seed.commit()
        yield connection
        transaction.rollback()


@pytest.fixture
def shared_client(connection: Connection) -> Iterator[TestClient]:
    """Every request gets the same Session: what not to do."""
    with Session(connection, join_transaction_mode="create_savepoint") as session:
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as client:
            yield client
    app.dependency_overrides.clear()


@pytest.fixture
def client(connection: Connection) -> Iterator[TestClient]:
    """Every request gets its own Session, in the test's transaction."""

    def override() -> Iterator[Session]:
        with Session(connection, join_transaction_mode="create_savepoint") as session:
            yield session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
```

Notes on it:

- The two event listeners are for SQLite's Python driver only, which
  otherwise manages transactions itself and breaks `SAVEPOINT` (see
  SQLAlchemy's notes on "Serializable isolation / Savepoints / Transactional
  DDL" for pysqlite). Without them the rollback at the end of the test does
  not undo the seed, and the second test fails with `UNIQUE constraint
  failed`. Leave them out on PostgreSQL and MySQL.
- A sync endpoint runs in a worker thread, so an in-memory SQLite engine needs
  `check_same_thread=False` and `StaticPool`, as in the
  [FastAPI recipe](recipes-fastapi.md).
- The seed runs in the fixture, so it is not measured.
- All the requests' sessions use one `Connection`, so the requests must run
  one after another, as they do with `TestClient` calls in a test. Requests
  that overlap (`asyncio.gather` over an async client, threads) would use the
  connection at the same time, which a DBAPI connection does not allow, and
  pytest-queryguard cannot tell two `Session`s apart while they share a
  `Connection` at the same time (see [the limits](several-sessions.md#limits)).
- `AsyncSession` accepts the same `join_transaction_mode` argument, bound to
  an `AsyncConnection`; that variant is not run on this page.

## One shared session

`tests/test_shared_session.py` requests five orders through the shared
`Session`:

```python
def test_five_orders(shared_client):
    for order_id in range(1, 6):
        assert shared_client.get(f"/orders/{order_id}").status_code == 200
```

It fails, although the endpoint has no loop: all five requests ran in one
`Session`, so its two statements are counted five times each:

```
queryguard: N+1 detected

  Order.customer was lazy-loaded 5 times in one Session
  first at app/main.py:25 in order_detail
  parents loaded at app/main.py:24 in order_detail
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))

  the same statement ran 5 times in one Session or connection checkout
  first at app/main.py:24 in order_detail
  kind: repeated-statement  label: SELECT orders.id, orders.customer_id FROM orders WHERE orders.id = ?
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#repeated-statement
  fix: fetch all rows in one query (WHERE id IN (...)) instead of in a loop
```

## A session per request

`tests/test_session_per_request.py` runs the same requests with `client`:

```python
def test_five_orders(client):
    for order_id in range(1, 6):
        assert client.get(f"/orders/{order_id}").status_code == 200


def test_the_seed_of_the_last_test_was_rolled_back(client):
    # the fixture seeds the same primary keys again; it would fail if the
    # rows of the test before were still there
    assert client.get("/orders/1").json() == {"customer": "customer 1"}
```

Both pass: each request is its own scope, and none reaches a threshold. A loop
inside one request is still reported, because it runs in one `Session`.

## What this changes and what it does not

- The per-request `Session` is also closer to production, where each request
  has its own session: an identity map shared across requests can hide lazy loads
  and stale reads that the real application has.
- Without `queryguard_scope = session` the per-request version is reported
  too, with a note that the runs were spread over several Sessions: the
  default `test` scope adds up everything the test runs. See
  [Several requests in one test](recipes-fastapi.md#several-requests-in-one-test).
- `queryguard_scope = session` cannot see a real N+1 written as a loop that
  opens a new `Session` per round; see
  [the limits of the option](several-sessions.md#limits).
- If you cannot change the fixture, the other ways out are to make one request
  per test (`pytest.mark.parametrize` over the ids) or to leave the finding out
  for that test (see [Ignoring findings](ignoring.md)).
