# Recipe: Flask-SQLAlchemy

Flask-SQLAlchemy sits on SQLAlchemy 2, so the plugin needs no setup. The
queries of a request made with `app.test_client()` run on `db.session` and are
counted. The code and output on this page were run with Flask 3.1.3 and
Flask-SQLAlchemy 3.1.1.

The layout is `app/__init__.py`, `tests/conftest.py` and `tests/test_orders.py`.

`pytest.ini` puts the project directory on `sys.path`, so that the tests can
`import app` (see [getting started](getting-started.md)); without it, pytest
stops with `ModuleNotFoundError: No module named 'app'`:

```ini
[pytest]
pythonpath = .
```

`app/__init__.py`, an application factory with a broken and a fixed endpoint:

```python
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import ForeignKey, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    joinedload,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)


class Customer(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class Order(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    customer: Mapped[Customer] = relationship()


def create_app(database_uri: str = "sqlite:///app.db") -> Flask:
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_uri
    db.init_app(app)

    @app.get("/orders")
    def list_orders() -> list[str]:
        return [order.customer.name for order in db.session.scalars(select(Order))]

    @app.get("/orders-fixed")
    def list_orders_fixed() -> list[str]:
        stmt = select(Order).options(joinedload(Order.customer))
        return [order.customer.name for order in db.session.scalars(stmt)]

    return app
```

`tests/conftest.py` builds a fresh in-memory database for each test.
Flask-SQLAlchemy 3.1 uses a `StaticPool` for in-memory SQLite, so the
test client's requests see the same database:

```python
import pytest
from app import Customer, Order, create_app, db


@pytest.fixture
def app():
    app = create_app("sqlite://")  # a fresh in-memory database for each test
    with app.app_context():
        db.create_all()
        db.session.add_all(Customer(id=i, name=f"customer {i}") for i in range(1, 6))
        db.session.add_all(Order(id=i, customer_id=i) for i in range(1, 6))
        db.session.commit()
    yield app
    with app.app_context():
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()
```

`tests/test_orders.py`:

```python
def test_orders(client):
    response = client.get("/orders")
    assert response.status_code == 200
    assert len(response.json) == 5


def test_orders_fixed(client):
    response = client.get("/orders-fixed")
    assert response.status_code == 200
    assert len(response.json) == 5
```

```
F.                                                                       [100%]
=================================== FAILURES ===================================
_________________________________ test_orders __________________________________
queryguard: N+1 detected

  Order.customer was lazy-loaded 5 times
  first at app/__init__.py:38 in list_orders
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
=========================== short test summary info ============================
FAILED tests/test_orders.py::test_orders - Failed: queryguard: N+1 detected
1 failed, 1 passed in 0.04s
```

The finding points at the line in the view function. Notes:

- Rows created in a fixture (here, `db.session.add_all()` and `commit()` in
  `app`) are not measured; only the test function body is, and the request
  made in it.
- `db.session` is a scoped session. Its scope is the application context, so
  each request gets its own session, as in production.
