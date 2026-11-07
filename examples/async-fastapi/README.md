# Async FastAPI endpoint

```console
$ pip install pytest-queryguard fastapi httpx aiosqlite "sqlalchemy[asyncio]"
$ pytest tests/test_orders.py
```

`app/main.py` has an async endpoint that reads `awaitable_attrs.customer` for
each order. FastAPI's `TestClient` runs the application in another thread;
queries from any thread are counted, so the test that calls the endpoint fails,
and the finding points to the `await` in the endpoint:

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

`/orders-fixed` uses `selectinload`, and `tests/test_orders_fixed.py` passes:

```console
$ pytest tests/test_orders_fixed.py
.                                                                        [100%]
1 passed
```

The `filterwarnings` entry in `pytest.ini` hides the warning of Starlette 1.2
and later about its test client finding `httpx` without `httpx2`; it is not
about pytest-queryguard. Installing `httpx2` as well removes the warning; see
the [FastAPI recipe](https://maciejziel.github.io/pytest-queryguard/recipes-fastapi.html).
