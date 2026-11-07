# Recipe: budgets for hot endpoints

N+1 detection finds a pattern. A budget protects a number: this endpoint
sends at most N statements. Put budgets on the few endpoints where a
regression costs most, such as the page every user loads, and let the
detection cover the rest. See [Budgets and timing](budgets.md) for the full
reference.

The endpoint below (`/orders/{order_id}` in
[examples/fastapi-dependency-override](https://github.com/MaciejZiel/pytest-queryguard/tree/main/examples/fastapi-dependency-override))
has no N+1, so detection stays quiet, but it sends four statements, two of
them counts:

```python
@app.get("/orders/{order_id}")
def order_detail(order_id: int, session: SessionDep) -> dict[str, object]:
    order = session.scalars(select(Order).filter_by(id=order_id)).one()
    customer = session.scalars(select(Customer).filter_by(id=order.customer_id)).one()
    siblings = session.scalar(
        select(func.count()).select_from(Order).where(Order.customer_id == customer.id)
    )
    audit = session.scalar(select(func.count()).select_from(Order))
    return {"customer": customer.name, "orders": siblings, "total": audit}
```

A budget of two statements, set with the marker on the test that calls it:

```python
import pytest


@pytest.mark.max_queries(2)
def test_order_detail_budget(client):
    response = client.get("/orders/1")
    assert response.status_code == 200
```

```
F                                                                        [100%]
=================================== FAILURES ===================================
___________________________ test_order_detail_budget ___________________________
queryguard: expected at most 2 queries, got 4

  1. SELECT orders.id, orders.customer_id FROM orders WHERE orders.id = ?  (app/main.py:63 in order_detail)
  2. SELECT customer.id, customer.name FROM customer WHERE customer.id = ?  (app/main.py:64 in order_detail)
  3. SELECT count(*) AS count_1 FROM orders WHERE orders.customer_id = ?  (app/main.py:65 in order_detail)
  4. SELECT count(*) AS count_1 FROM orders  (app/main.py:68 in order_detail)

  database time: 77 us
  slowest:
        24 us  SELECT count(*) AS count_1 FROM orders WHERE orders.customer_id = ?  (app/main.py:65 in order_detail)
        24 us  SELECT orders.id, orders.customer_id FROM orders WHERE orders.id = ?  (app/main.py:63 in order_detail)
        15 us  SELECT customer.id, customer.name FROM customer WHERE customer.id = ?  (app/main.py:64 in order_detail)
=========================== short test summary info ============================
FAILED tests/test_budget.py::test_order_detail_budget - Failed: queryguard: e...
1 failed in 0.06s
```

The report lists every statement with the line that sent it, so the cost is
visible: the two counts are what to remove or merge. The timings vary from run
to run; the statements and their lines do not.

## Choosing the number

- Run the test once with a generous budget, read what the endpoint sends, and
  set the budget to that count. A budget above what the code needs lets a
  regression of the same size pass.
- Put the number next to the endpoint's contract in the test, with a comment
  that says why it is what it is. The next person who needs a third query
  should have to decide that on purpose.
- Budgets count every statement, writes included, and they fail in every
  `queryguard_mode`. Fixtures are not counted, so seed data costs nothing.
- Use `with max_queries(limit):` around the one call when a test does more than
  call the endpoint. Only the block counts.

## Time budgets

`@pytest.mark.max_db_time(seconds)` limits the time spent in the database
and driver for the test. Timing on a shared CI runner is noisy, and an
in-memory SQLite test measures microseconds. Use a time budget only against a
real server, with a limit several times what you see locally, and prefer a
statement budget, which does not flake.
