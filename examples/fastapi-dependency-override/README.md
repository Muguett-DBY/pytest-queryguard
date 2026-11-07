# FastAPI with dependency-overridden sessions

```console
$ pip install pytest-queryguard fastapi httpx aiosqlite "sqlalchemy[asyncio]"
$ pytest tests/test_orders.py
```

The application gets its session from a dependency (`get_session`,
`get_async_session` in `app/main.py`). The tests replace it with
`app.dependency_overrides` (`tests/conftest.py`) so that every request uses a
session on an in-memory database. Two tests call a broken endpoint, one
through FastAPI's `TestClient` (a sync test; the sync endpoint runs in a worker
thread) and one through `httpx.AsyncClient` in an `async def` test (run by
the anyio pytest plugin that ships with anyio, so nothing extra is installed).
Both fail, and each finding points to the line in the endpoint:

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

`tests/test_orders_fixed.py` calls the `-fixed` endpoints, which use
`joinedload` and `selectinload`, and passes:

```console
$ pytest tests/test_orders_fixed.py
..                                                                       [100%]
2 passed
```

`GET /orders/{order_id}` is a fourth endpoint, with no N+1 but four statements per
call; the [recipes](https://maciejziel.github.io/pytest-queryguard/recipes-budgets.html) use it for budgets.

The in-memory SQLite engine for the sync test is created with
`connect_args={"check_same_thread": False}` and `StaticPool`: a sync endpoint
runs in a worker thread, and without the argument SQLite refuses the
connection from it. The `filterwarnings` entry in `pytest.ini` hides the
warning of Starlette 1.2 and later about its test client finding `httpx`
without `httpx2`; it is not about pytest-queryguard. Installing `httpx2` as
well removes the warning; see the
[FastAPI recipe](https://maciejziel.github.io/pytest-queryguard/recipes-fastapi.html).
