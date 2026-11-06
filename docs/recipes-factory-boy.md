# Recipe: Factory Boy and seeding in the test body

Many suites create their rows with Factory Boy, or another factory helper,
inside the test function: `OrderFactory.create_batch(10)` on the first line,
then the call under test. pytest-queryguard measures the test body, so it sees
those `INSERT` statements as part of the test. Two things follow:

- With the opt-in write-loop rule on (`queryguard_write_loop_threshold`), a
  factory that flushes each object it creates reads as a row-by-row write loop:
  one `INSERT` per row from one line. The rule cannot tell seeding from a loop
  in the code under test (see [Limitations](limitations.md)).
- A factory that commits each object (`sqlalchemy_session_persistence =
  "commit"`) expires it, so reading the created objects back in the test
  reloads them one by one. That is reported by the default rules as
  `column-load` and `relationship-load`.

This page shows two ways out: seed in fixtures, which are not measured, or
leave out the seeding findings with `queryguard_ignore`. It was run with
Factory Boy 3.3.3, SQLAlchemy 2.1.1, pytest 9.1 and Python 3.13; pytest-queryguard
0.5.0rc3.

pytest-django suites that use the Django ORM are not affected: pytest-queryguard
sees only statements that go through SQLAlchemy, so it reports nothing for
them. What follows applies to SQLAlchemy suites written in the same style.

## The project

Install Factory Boy next to the plugin:

```console
pip install pytest-queryguard factory-boy
```

The layout is `app/__init__.py` (an empty file), `app/models.py`,
`app/orders.py`, `tests/factories.py`, `tests/conftest.py` and
`tests/test_orders.py`.

`pytest.ini` turns the write-loop rule on, and leaves out write loops whose
call site is in the tests (the second way, explained below):

```ini
[pytest]
pythonpath = .
queryguard_write_loop_threshold = 5
queryguard_ignore =
    write-loop * file=tests/*
```

In `pyproject.toml`, the same under `[tool.pytest.ini_options]`:
`queryguard_write_loop_threshold = 5` and
`queryguard_ignore = ["write-loop * file=tests/*"]`.

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

`app/orders.py`. `customer_names` is the code under test; `import_customers`
writes one row at a time on purpose, to show that the rule still reports a
write loop in the application:

```python
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Customer, Order


def customer_names(session: Session) -> list[str]:
    stmt = select(Order).options(joinedload(Order.customer)).order_by(Order.id)
    return [order.customer.name for order in session.scalars(stmt)]


def import_customers(session: Session, names: list[str]) -> None:
    for name in names:
        session.add(Customer(name=name))
        session.flush()
```

`tests/factories.py`. The factories share one `scoped_session`, as the Factory
Boy documentation suggests. `"flush"` sends each object's `INSERT` right away,
so that it has its primary key, and does not commit:

```python
import factory
from factory.alchemy import SQLAlchemyModelFactory
from sqlalchemy.orm import scoped_session, sessionmaker

from app.models import Customer, Order

Session = scoped_session(sessionmaker())


class CustomerFactory(SQLAlchemyModelFactory):
    class Meta:
        model = Customer
        sqlalchemy_session = Session
        sqlalchemy_session_persistence = "flush"

    name = factory.Sequence(lambda n: f"customer {n}")


class OrderFactory(SQLAlchemyModelFactory):
    class Meta:
        model = Order
        sqlalchemy_session = Session
        sqlalchemy_session_persistence = "flush"

    customer = factory.SubFactory(CustomerFactory)
```

## 1. Seed in a fixture

Only the test function body is measured, not its fixtures (see
[the FAQ](faq.md#the-code-i-want-to-check-runs-in-a-fixture)). Rows created in
a fixture are not part of any finding, whatever the factory does.

`tests/conftest.py`:

```python
import pytest
from sqlalchemy import create_engine

from app.models import Base
from tests.factories import OrderFactory, Session


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session.configure(bind=engine)
    yield Session()
    Session.remove()
    engine.dispose()


@pytest.fixture
def orders(session):
    return OrderFactory.create_batch(10)
```

`tests/test_orders.py`:

```python
from app.orders import customer_names, import_customers
from tests.factories import OrderFactory


def test_seeded_in_a_fixture(session, orders):
    assert len(customer_names(session)) == 10


def test_seeded_in_the_body(session):
    OrderFactory.create_batch(10)
    assert len(customer_names(session)) == 10


def test_import_customers(session):
    import_customers(session, [f"new {i}" for i in range(10)])
```

`test_seeded_in_a_fixture` passes with or without the ignore pattern.

Keep `"flush"` (or Factory Boy's default, which only adds the objects to the
session) when the test reads the created objects. With `"commit"`, each commit
expires the objects, and the test reloads every one of them, which the default
rules report even when the rows came from a fixture: with both factories set
to `"commit"`, a test that reads `order.customer.name` for each of the ten
orders of the `orders` fixture fails with two `column-load` findings,
`Order (expired)` and `Customer (expired)`. If the factories must commit,
create the session with `sessionmaker(expire_on_commit=False)`; the same test
then passes.

## 2. Leave out the seeding findings

Where moving every factory call into fixtures is too much work, leave the
findings out. A finding's call site is the first line outside SQLAlchemy,
pytest, the standard library and `site-packages`. Factory Boy is installed in
`site-packages`, so the call site of a factory's `INSERT` is the line in the
test that called the factory, not `tests/factories.py`. Without the ignore
pattern, `test_seeded_in_the_body` fails like this:

```
queryguard: N+1 detected

  the same INSERT ran 10 times, one row at a time
  first at tests/test_orders.py:10 in test_seeded_in_the_body
  kind: write-loop  label: INSERT INTO customer (name) VALUES (?)
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#write-loop
  fix: write the rows together: session.add_all(rows) and one flush, or
       session.execute(insert(Model), [{...}, {...}]) for a bulk insert

  the same INSERT ran 10 times, one row at a time
  first at tests/test_orders.py:10 in test_seeded_in_the_body
  kind: write-loop  label: INSERT INTO orders (customer_id) VALUES (?)
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#write-loop
  fix: write the rows together: session.add_all(rows) and one flush, or
       session.execute(insert(Model), [{...}, {...}]) for a bulk insert
```

So the pattern names the tests, not the factory module:
`write-loop * file=tests/*` leaves out every write loop whose call site is in
`tests/`, and keeps the ones in the application. With it,
`test_seeded_in_the_body` passes, and `test_import_customers` still fails:

```
queryguard: N+1 detected

  the same INSERT ran 10 times, one row at a time
  first at app/orders.py:15 in import_customers
    called from tests/test_orders.py:15 in test_import_customers
  kind: write-loop  label: INSERT INTO customer (name) VALUES (?)
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#write-loop
  fix: write the rows together: session.add_all(rows) and one flush, or
       session.execute(insert(Model), [{...}, {...}]) for a bulk insert
```

`write-loop * file=tests/factories.py` matches nothing here, and the run lists
it under "queryguard_ignore patterns that matched no finding". It would match
only if `tests/factories.py` itself sent the statements, for example a factory
whose own `_create` calls `session.flush()`.

The pattern syntax, and the per-test marker
(`@pytest.mark.allow_nplusone("*", kind="write-loop")`), are in
[Ignoring findings](ignoring.md). Keep the kind in the pattern: `* * file=tests/*`
would also leave out the N+1 findings of a test that reads data in a loop
itself.
