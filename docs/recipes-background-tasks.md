# Recipe: background tasks in tests

In production a background task runs after the response, or in a worker. In a
test it often runs in the test itself: FastAPI's and Starlette's `TestClient`
runs `BackgroundTasks` before the request call returns, and Celery with
`task_always_eager` runs a task inline. pytest-queryguard measures everything
the test body runs, so the task's statements count for the test, and an N+1 in
the task fails the test of the endpoint that scheduled it.

That is usually what you want: the N+1 is real, and the report points at the
task. This page shows what the report looks like, how to test the task on its
own, and how to leave the task out of an endpoint's test when another test
covers it. The project was run for this page with SQLAlchemy 2.0.13 and 2.1.1,
FastAPI 0.141 and httpx 0.28; a test builds it from the files of this page and
runs it. Celery was not run; with `task_always_eager` its tasks run inline in
the same way.

The layout is `pytest.ini`, `app/__init__.py` and `tests/__init__.py` (both
empty), `app/models.py`, `app/tasks.py`, `app/main.py`, `tests/conftest.py`,
`tests/test_endpoint.py` and `tests/test_tasks.py`.

## The application

`pytest.ini`:

```ini
[pytest]
pythonpath = .
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
    email: Mapped[str]


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    customer: Mapped[Customer] = relationship()
```

`app/tasks.py` has the task twice, with and without the N+1. A task opens its
own session from a session factory: the request's session is closed by the
time the task runs.

```python
from sqlalchemy import select
from sqlalchemy.orm import selectinload, sessionmaker

from app.models import Order

OUTBOX: list[str] = []


def send_reminders(sessions: sessionmaker) -> None:
    with sessions() as session:
        for order in session.scalars(select(Order)):
            OUTBOX.append(order.customer.email)


def send_reminders_fixed(sessions: sessionmaker) -> None:
    stmt = select(Order).options(selectinload(Order.customer))
    with sessions() as session:
        for order in session.scalars(stmt):
            OUTBOX.append(order.customer.email)
```

`app/main.py` schedules the task after closing the day. The session factory
comes from a dependency, so that the tests can replace it:

```python
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.tasks import send_reminders

app = FastAPI()
_sessions = sessionmaker(create_engine("sqlite:///app.db"))


def get_sessions() -> sessionmaker:
    return _sessions


@app.post("/close-day")
def close_day(
    tasks: BackgroundTasks, sessions: Annotated[sessionmaker, Depends(get_sessions)]
) -> dict[str, str]:
    tasks.add_task(send_reminders, sessions)
    return {"status": "closed"}
```

## The tests

`tests/conftest.py` seeds five orders of five customers on an in-memory
database, in fixtures, so the seeding is not measured, and empties the
outbox:

```python
from collections.abc import Iterator

import pytest
from app.main import app, get_sessions
from app.models import Base, Customer, Order
from app.tasks import OUTBOX
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def sessions() -> Iterator[sessionmaker]:
    OUTBOX.clear()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        session.add_all(Customer(id=i, email=f"c{i}@example.com") for i in range(1, 6))
        session.add_all(Order(id=i, customer_id=i) for i in range(1, 6))
        session.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def client(sessions: sessionmaker) -> Iterator[TestClient]:
    app.dependency_overrides[get_sessions] = lambda: sessions
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
```

`tests/test_endpoint.py` calls the endpoint twice, once plainly and once with
the task's finding left out by a marker:

```python
import pytest
from app.tasks import OUTBOX


def test_close_day(client):
    assert client.post("/close-day").json() == {"status": "closed"}
    assert len(OUTBOX) == 5  # the task ran before post() returned


@pytest.mark.queryguard_ignore("Order.customer", function="send_reminders")
def test_close_day_without_the_task(client):
    assert client.post("/close-day").json() == {"status": "closed"}
```

The first test fails with the task's N+1, at the line of the task. The task ran
in the test client's worker thread, whose stack does not reach the test, so
there is no `called from` line:

```
queryguard: N+1 detected

  Order.customer was lazy-loaded 5 times
  first at app/tasks.py:12 in send_reminders
  parents loaded at app/tasks.py:11 in send_reminders
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.email FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
```

The second test passes: the marker leaves out that one finding, by its label
and the function it is reported in, and anything else the endpoint does is
still checked. Use it when the task has its own test, as below; a marker or a
pattern that no longer matches is listed as a stale ignore once the task is
fixed.

`tests/test_tasks.py` calls the tasks directly, as plain functions, with the
test's session factory. Each opens its own session, as it would in a worker:

```python
from app.tasks import send_reminders, send_reminders_fixed


def test_send_reminders(sessions):
    send_reminders(sessions)


def test_send_reminders_fixed(sessions):
    send_reminders_fixed(sessions)
```

`test_send_reminders` fails with the same finding, now with a `called from`
line for the test; `test_send_reminders_fixed` passes.

## Other ways

- A task that runs statements the request also runs (the same lookup in the
  request and in the task) adds to the request's counts. With
  `queryguard_scope = session` (see
  [Tests with several sessions or requests](several-sessions.md)), the task's
  own session is counted on its own.
- An [`ignore()` block](ignoring.md#the-ignore-block) can wrap the task's
  body in the application code, as a decorator (`@ignore("relationship-load")`
  on the task function). It applies to every caller, the task's own test
  included, so prefer the marker above when the task should be checked
  somewhere.
- Outside tests, in a worker, the [in-application mode](in-app.md#celery)
  measures each Celery task as a unit of its own.
