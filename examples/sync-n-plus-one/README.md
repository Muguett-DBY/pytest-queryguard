# Sync SQLAlchemy: N+1, a lookup loop and a budget

```console
$ pip install pytest-queryguard
$ pytest --queryguard-report tests/test_orders.py
```

`tests/test_orders.py` has three tests that all pass without the plugin:

- `list_orders` reads `order.customer` for 50 orders (a lazy-load N+1);
- `fetch_customers` calls `session.get()` in a loop;
- `rename_customers` has a `max_queries(2)` budget and runs 4 statements.

Output (Python 3.13, SQLAlchemy 2.1, pytest 9.1):

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
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
_____________________________ test_fetch_customers _____________________________
queryguard: N+1 detected

  the same statement ran 50 times
  first at app/customers.py:9 in fetch_customers
    called from tests/test_orders.py:11 in test_fetch_customers
  kind: repeated-statement  label: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
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

(The timing numbers differ from run to run, and SQLAlchemy 2.0 renders the
SQL with column aliases, `customer.id AS customer_id`.) `app/orders_fixed.py` and
`app/customers_fixed.py` load the customers with `joinedload` and one `IN`
query. `tests/test_orders_fixed.py` runs the same tests against them:

```console
$ pytest tests/test_orders_fixed.py
tests/test_orders_fixed.py ...                                           [100%]
3 passed
```
