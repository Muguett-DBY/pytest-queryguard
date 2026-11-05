# Getting started

## Install

```bash
pip install pytest-queryguard
```

It needs Python 3.10 or newer, pytest 8 or newer and SQLAlchemy 2.0.13 or
newer; pip installs pytest and SQLAlchemy with it. The plugin registers itself
with pytest: there is nothing to import or configure. `pytest -VV` lists it
(`pytest-queryguard-0.6.0`).

!!! note "Free-threaded Python"
    The free-threaded builds (CPython 3.13t and 3.14t, no GIL) are not in the
    package classifiers, so they are not a supported configuration. CI runs
    only the in-app mode's tests (`tests/inapp`) on 3.14t, not the whole
    suite. The whole suite was run once on CPython 3.14.4t with SQLAlchemy
    2.1.1 and passed; 3.13t was not tried. On those builds the
    counters of a measurement (statements, loads and database time) are
    updated under a lock, so statements from several threads are all counted
    and budgets hold; on a build with the GIL no lock is taken. The stored
    events are list appends and the call-site caches are dictionaries, which
    CPython keeps consistent without the GIL; the sequence numbers that order
    Sessions and executions (`itertools.count`) stayed unique from 8 threads
    on 3.14.4t. The missing-index check and the unused-eager-load tracker
    check and then update shared sets, so with several threads they may do
    one step twice; those were not examined further. Every listener fails
    open, so a race there costs accuracy, not the test run.

## A first test

Every test is checked automatically; you do not write anything to get N+1
detection. The project below is
[examples/sync-n-plus-one](https://github.com/MaciejZiel/pytest-queryguard/tree/main/examples/sync-n-plus-one),
where a test runs it and checks that its README shows the output you see here.
To follow along, make an empty directory and create the files of this page in
it; every file is shown.

`pytest.ini` puts the project directory on `sys.path`, so that the tests can
`import app` (in `pyproject.toml` the same is
`[tool.pytest.ini_options]` with `pythonpath = ["."]`):

```ini
[pytest]
pythonpath = .
```

`app/__init__.py` and `tests/__init__.py` are empty files. The models are an
order that belongs to a customer.

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

The `session` fixture creates 50 customers with one order each, in an
in-memory SQLite database. It runs before the test body, so its own
statements are not counted.

`tests/conftest.py`:

```python
from collections.abc import Iterator

import pytest
from app.models import Base, Customer, Order
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(Customer(id=i, name=f"customer {i}") for i in range(1, 51))
        session.add_all(Order(id=i, customer_id=i) for i in range(1, 51))
        session.commit()
        yield session
```

The code under test lists the orders with their customer's name.

`app/orders.py`:

```python
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order


def list_orders(session: Session) -> list[dict[str, object]]:
    rows = []
    for order in session.scalars(select(Order)).all():
        rows.append({"id": order.id, "customer": order.customer.name})
    return rows
```

`app/customers.py`:

```python
from sqlalchemy.orm import Session

from app.models import Customer


def fetch_customers(session: Session, ids: list[int]) -> list[Customer]:
    customers = []
    for customer_id in ids:
        customers.append(session.get(Customer, customer_id))
    return customers


def rename_customers(session: Session, names: dict[int, str]) -> None:
    for customer_id, name in names.items():
        session.get(Customer, customer_id).name = name
        session.flush()
```

`tests/test_orders.py` (the `session` fixture in `tests/conftest.py` creates
50 customers with one order each):

```python
import pytest
from app.customers import fetch_customers, rename_customers
from app.orders import list_orders


def test_list_orders(session):
    assert len(list_orders(session)) == 50


def test_fetch_customers(session):
    assert len(fetch_customers(session, list(range(1, 51)))) == 50


@pytest.mark.max_queries(2)
def test_rename_customers(session):
    rename_customers(session, {1: "Ada", 2: "Grace"})
```

All three tests pass without the plugin. With it installed,
`pytest --queryguard-report` gives the output below (Python 3.13, SQLAlchemy
2.1). Older versions print slightly different SQL, for example column aliases
(`customer.id AS customer_id`) on SQLAlchemy 2.0, and the timing numbers differ
from run to run.

```
FFF                                                                      [100%]
=================================== FAILURES ===================================
_______________________________ test_list_orders _______________________________
queryguard: N+1 detected

  Order.customer was lazy-loaded 50 times
  first at app/orders.py:10 in list_orders
    called from tests/test_orders.py:7 in test_list_orders
  parents loaded at app/orders.py:9 in list_orders
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
_____________________________ test_fetch_customers _____________________________
queryguard: N+1 detected

  the same statement ran 50 times
  first at app/customers.py:9 in fetch_customers
    called from tests/test_orders.py:11 in test_fetch_customers
  kind: repeated-statement  label: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#repeated-statement
  fix: fetch all rows in one query (WHERE id IN (...)) instead of in a loop
____________________________ test_rename_customers _____________________________
queryguard: expected at most 2 queries, got 4

  1. SELECT customer.id, customer.name FROM customer WHERE customer.id = ?  (app/customers.py:15 in rename_customers)
  2. UPDATE customer SET name=? WHERE customer.id = ?  (app/customers.py:16 in rename_customers)
  3. SELECT customer.id, customer.name FROM customer WHERE customer.id = ?  (app/customers.py:15 in rename_customers)
  4. UPDATE customer SET name=? WHERE customer.id = ?  (app/customers.py:16 in rename_customers)

  database time: 154 us
  slowest:
        71 us  UPDATE customer SET name=? WHERE customer.id = ?  (app/customers.py:16 in rename_customers)
        37 us  UPDATE customer SET name=? WHERE customer.id = ?  (app/customers.py:16 in rename_customers)
        27 us  SELECT customer.id, customer.name FROM customer WHERE customer.id = ?  (app/customers.py:15 in rename_customers)
============================== queryguard report ===============================
   51  tests/test_orders.py::test_list_orders
   50  tests/test_orders.py::test_fetch_customers
    4  tests/test_orders.py::test_rename_customers
=========================== short test summary info ============================
FAILED tests/test_orders.py::test_list_orders - Failed: queryguard: N+1 detected
FAILED tests/test_orders.py::test_fetch_customers - Failed: queryguard: N+1 d...
FAILED tests/test_orders.py::test_rename_customers - Failed: queryguard: expe...
3 failed in 0.08s
```


The fixed code loads the related rows with the parent query and fetches many
rows with one `IN` query.

`app/orders_fixed.py`:

```python
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Order


def list_orders(session: Session) -> list[dict[str, object]]:
    rows = []
    for order in session.scalars(select(Order).options(joinedload(Order.customer))):
        rows.append({"id": order.id, "customer": order.customer.name})
    return rows
```

`app/customers_fixed.py`:

```python
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Customer


def fetch_customers(session: Session, ids: list[int]) -> Sequence[Customer]:
    return session.scalars(select(Customer).where(Customer.id.in_(ids))).all()


def rename_customers(session: Session, names: dict[int, str]) -> None:
    customers = session.scalars(select(Customer).where(Customer.id.in_(names)))
    for customer in customers:
        customer.name = names[customer.id]
    session.flush()
```

`tests/test_orders_fixed.py` is the same three tests, importing the fixed
modules:

```python
import pytest
from app.customers_fixed import fetch_customers, rename_customers
from app.orders_fixed import list_orders


def test_list_orders(session):
    assert len(list_orders(session)) == 50


def test_fetch_customers(session):
    assert len(fetch_customers(session, list(range(1, 51)))) == 50


@pytest.mark.max_queries(2)
def test_rename_customers(session):
    rename_customers(session, {1: "Ada", 2: "Grace"})
```

They pass:

```
tests/test_orders_fixed.py ...                                           [100%]
3 passed
```

## Async and FastAPI

Nothing changes for async code. `AsyncSession` and `create_async_engine` run
on a sync engine underneath, which the plugin already watches. On SQLAlchemy
2.1 and newer, async support needs `pip install "sqlalchemy[asyncio]"`, which
provides greenlet. The failure points to the `await` in your code, not to
SQLAlchemy internals. This endpoint (from
[examples/async-fastapi](https://github.com/MaciejZiel/pytest-queryguard/tree/main/examples/async-fastapi);
the models use SQLAlchemy's `AsyncAttrs` mixin) reads a relationship for each
order:

```python
@app.get("/orders")
async def list_orders() -> list[str]:
    async with sessions() as session:
        orders = (await session.scalars(select(Order))).all()
        names = []
        for order in orders:
            customer = await order.awaitable_attrs.customer
            names.append(customer.name)
        return names
```

FastAPI's `TestClient` runs the application in another thread. Queries from
any thread are counted, so an N+1 inside an endpoint fails the test that
called it:

```python
def test_orders_endpoint():
    with TestClient(app) as client:
        response = client.get("/orders")
    assert response.status_code == 200
```

```
queryguard: N+1 detected

  Order.customer was lazy-loaded 5 times
  first at app/main.py:37 in list_orders
  parents loaded at app/main.py:34 in list_orders
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
```

pytest cannot run `async def` tests by itself. With pytest-asyncio or anyio you
can write a test as `async def` and use an `AsyncSession` directly.

An application that gets its session from a dependency (`Depends(get_session)`)
is tested by overriding the dependency with a session on the test database;
the [FastAPI recipe](recipes-fastapi.md) shows it for `TestClient` and
`httpx.AsyncClient`.

## What next

The path from here, in the order most projects need it:

1. [A test failed: now what](when-a-test-fails.md): read the report, check that
   the finding is real, fix it, or silence it on purpose.
2. [CI and GitHub Action](ci.md): nothing to add for a plain pytest job; the
   action also shows the findings in pull requests.
3. [Baseline](baseline.md): on an existing project, record today's findings
   so that only new ones fail. [Adopting on a legacy
   codebase](recipes-legacy.md) walks through it.
4. [Index check](index-check.md) and [index advisor](index-advisor.md): fail
   tests whose queries no index can serve, with the `CREATE INDEX` to add.

[How detection works](how-detection-works.md) explains the rules and their
thresholds, and the [configuration reference](configuration.md) lists every
option.
