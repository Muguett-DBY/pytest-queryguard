# Missing index on SQLite

```console
$ pip install pytest-queryguard
$ pytest tests/test_orders.py
```

The index check works on SQLite too, so this example needs no server.
`queryguard_index_check = true` in `pytest.ini` turns it on. SQLite has no
cost model to switch off, so the check runs `EXPLAIN QUERY PLAN` for each
distinct statement and reports a table that is scanned although the statement
filters on one of its columns:

```
queryguard: missing index

  orders is read in full: SCAN with filter orders.customer_id = ?
  first at app/orders.py:11 in orders_of
  kind: missing-index  label: orders
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#missing-index
  sql: SELECT orders.id, orders.customer_id FROM orders WHERE orders.customer_id = ?
  fix: CREATE INDEX ix_orders_customer_id ON orders (customer_id)
  alembic: op.create_index("ix_orders_customer_id", "orders", ["customer_id"])

  a sequential scan is fine on a small lookup table:
  list it in queryguard_index_ignore_tables
```

`tests/test_orders_fixed.py` uses `IndexedOrder`, the same table with
`index=True`, and passes. See the [SQLite page](../../docs/index-check-sqlite.md)
for what the check reports and what it leaves alone.
