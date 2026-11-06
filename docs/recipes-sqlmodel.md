# Recipe: SQLModel

SQLModel models are SQLAlchemy models, and `Session.exec()` runs SQLAlchemy
statements, so lazy loads and repeated statements are seen like in plain
SQLAlchemy. Run with SQLModel 0.0.48, installed with:

```console
pip install pytest-queryguard sqlmodel
```

The layout is `app/__init__.py` (an empty file), `app/models.py`,
`app/orders.py`, `tests/conftest.py` and `tests/test_orders.py`.

`pytest.ini` puts the project directory on `sys.path`, so that the tests can
`import app` (see [getting started](getting-started.md)):

```ini
[pytest]
pythonpath = .
```

`app/models.py`:

```python
from sqlmodel import Field, Relationship, SQLModel


class Customer(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str


class Order(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    customer_id: int = Field(foreign_key="customer.id")
    customer: Customer = Relationship()
```

`app/orders.py`. The broken function reads `order.customer` for every order;
the fixed one passes `selectinload`, which SQLModel does not wrap, so it comes
from `sqlalchemy.orm`:

```python
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.models import Order


def customer_names(session: Session) -> list[str]:
    return [order.customer.name for order in session.exec(select(Order))]


def customer_names_fixed(session: Session) -> list[str]:
    statement = select(Order).options(selectinload(Order.customer))
    return [order.customer.name for order in session.exec(statement)]
```

`tests/conftest.py`:

```python
import pytest
from app.models import Customer, Order
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


@pytest.fixture
def session():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(Customer(id=i, name=f"customer {i}") for i in range(1, 6))
        session.add_all(Order(id=i, customer_id=i) for i in range(1, 6))
        session.commit()
        yield session
```

`tests/test_orders.py`:

```python
from app.orders import customer_names, customer_names_fixed


def test_customer_names(session):
    assert len(customer_names(session)) == 5


def test_customer_names_fixed(session):
    assert len(customer_names_fixed(session)) == 5
```

```
F.                                                                       [100%]
=================================== FAILURES ===================================
_____________________________ test_customer_names ______________________________
queryguard: N+1 detected

  Order.customer was lazy-loaded 5 times
  first at app/orders.py:8 in customer_names
    called from tests/test_orders.py:5 in test_customer_names
  parents loaded at app/orders.py:8 in customer_names
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
=========================== short test summary info ============================
FAILED tests/test_orders.py::test_customer_names - Failed: queryguard: N+1 de...
1 failed, 1 passed in 0.04s
```

The suggested `joinedload` in the hint works with SQLModel relationships, as
does `selectinload`. Relationships declared with SQLModel's `Relationship()`
use the default lazy loading, so they are reported like any other. SQLModel's
async session (`sqlmodel.ext.asyncio.session.AsyncSession`) was not run for
this page.
