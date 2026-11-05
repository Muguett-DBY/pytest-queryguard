# Recipe: Litestar

Litestar's `TestClient` runs the application on an anyio portal in another
thread, as FastAPI's does; the queries of the request are counted. This page
uses a plain async session dependency (`Provide`), not Litestar's SQLAlchemy
plugin from advanced-alchemy, which was not run. Versions: Litestar 2.24.0,
SQLAlchemy 2.1.4, aiosqlite.

The layout is `app/__init__.py` (an empty file), `app/models.py`,
`app/main.py`, `tests/conftest.py` and `tests/test_orders.py`.

`pytest.ini` puts the project directory on `sys.path`, so that the tests can
`import app` (see [getting started](getting-started.md)):

```ini
[pytest]
pythonpath = .
```

`app/models.py`. The models use SQLAlchemy's `AsyncAttrs` mixin, which gives
`awaitable_attrs` for the lazy load in the broken handler:

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

`app/main.py`; `create_app()` takes the session provider so that tests can
pass their own:

```python
from collections.abc import AsyncGenerator

from litestar import Litestar, get
from litestar.di import NamedDependency, Provide
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.models import Order

engine = create_async_engine("sqlite+aiosqlite:///app.db")
sessions = async_sessionmaker(engine, expire_on_commit=False)


async def provide_session() -> AsyncGenerator[AsyncSession, None]:
    async with sessions() as session:
        yield session


SessionDep = NamedDependency[AsyncSession]


@get("/orders")
async def list_orders(session: SessionDep) -> list[str]:
    names = []
    for order in (await session.scalars(select(Order))).all():
        names.append((await order.awaitable_attrs.customer).name)
    return names


@get("/orders-fixed")
async def list_orders_fixed(session: SessionDep) -> list[str]:
    stmt = select(Order).options(selectinload(Order.customer))
    return [order.customer.name for order in (await session.scalars(stmt)).all()]


def create_app(provide=provide_session) -> Litestar:
    return Litestar(
        [list_orders, list_orders_fixed], dependencies={"session": Provide(provide)}
    )


app = create_app()
```

`tests/conftest.py` passes a provider bound to an in-memory database, and
creates the tables in an `on_startup` hook that runs inside the test client's
event loop:

```python
import pytest
from app.main import create_app
from app.models import Base, Customer, Order
from litestar.testing import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture
def client():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def provide_session():
        async with sessions() as session:
            yield session

    async def on_startup() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add_all(Customer(id=i, name=f"customer {i}") for i in range(1, 6))
            session.add_all(Order(id=i, customer_id=i) for i in range(1, 6))
            await session.commit()

    app = create_app(provide_session)
    app.on_startup.append(on_startup)
    with TestClient(app) as test_client:
        yield test_client
```

`tests/test_orders.py`:

```python
def test_orders(client):
    response = client.get("/orders")
    assert response.status_code == 200
    assert len(response.json()) == 5


def test_orders_fixed(client):
    response = client.get("/orders-fixed")
    assert response.status_code == 200
    assert len(response.json()) == 5
```

```
F.                                                                       [100%]
=================================== FAILURES ===================================
_________________________________ test_orders __________________________________
queryguard: N+1 detected

  Order.customer was lazy-loaded 5 times
  first at app/main.py:27 in list_orders
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
=========================== short test summary info ============================
FAILED tests/test_orders.py::test_orders - Failed: queryguard: N+1 detected
1 failed, 1 passed in 0.08s
```

Pytest also shows the captured log lines of Litestar's test client; they are
left out above. `NamedDependency[AsyncSession]` marks a handler argument as coming from
`dependencies`. In Litestar 2.24 the unmarked form (`session: AsyncSession`)
also worked here, but emitted a `LitestarDeprecationWarning`.
