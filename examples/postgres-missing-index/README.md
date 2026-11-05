# Missing index on PostgreSQL

```console
$ pip install pytest-queryguard pg8000
$ docker compose up -d --wait
$ pytest tests/test_orders.py
```

`pytest.ini` turns the index check on (`queryguard_index_check = true`). The
check needs PostgreSQL 16 or newer; `compose.yaml` starts 17. The tests read the
URL from `QUERYGUARD_PG_URL` (default
`postgresql+pg8000://postgres:pg@localhost:5432/postgres`).

`orders_of` filters `orders` by `customer_id`, which has no index. The table
holds 6 rows, and the check still fails, because PostgreSQL chooses a
sequential scan even when sequential scans are switched off:

```
queryguard: missing index

  orders is read in full: Seq Scan with filter (orders.customer_id = $1)
  first at app/orders.py:11 in orders_of
  kind: missing-index  label: orders
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#missing-index
  sql: SELECT orders.id, orders.customer_id FROM orders WHERE orders.customer_id = %s::INTEGER
  fix: CREATE INDEX ON orders (customer_id)
  alembic: op.create_index("ix_orders_customer_id", "orders", ["customer_id"])

  a sequential scan is fine on a small lookup table:
  list it in queryguard_index_ignore_tables
```

`IndexedOrder` is the same table with `index=True`, and
`tests/test_orders_fixed.py` passes. With the hypopg extension installed in the
database, remove `queryguard_hypopg = off` from `pytest.ini` and the finding
also says whether the planner would use the suggested index and the estimated
cost before and after. Stop the database with `docker compose down`.
