# Budgets and timing

Budgets fail a test that runs too many statements or spends too long in the database. They do not depend on N+1 detection and always fail, whatever `queryguard_mode` is set to.

## Query budgets with the `max_queries` marker

```python
import pytest


@pytest.mark.max_queries(3)
def test_order_page(client):
    client.get("/orders/1")


@pytest.mark.max_queries(limit=3)  # the same, with a keyword
def test_order_page_again(client):
    client.get("/orders/1")
```

The keyword is `limit`, as in the function below. Releases before 0.4 called
it `n`; `n=3` keeps working, without a warning. It may be removed in a future
minor release, but only after a deprecation period with a warning (see
[Stability](stability.md)).

The test fails when its body runs more SQL statements than the limit. The
failure lists every statement with the line that issued it. Budgets count
all statements, writes included, and they always fail, whatever
`queryguard_mode` is set to. If the test already fails on its own, an
exceeded budget is not reported, because the original failure wins.

## A default budget for every test

```ini
[pytest]
queryguard_max_queries = 20
```

Every test without a `max_queries` marker (and without an entry in the
budget file below) gets this budget. A test above it fails with the usual
budget report and the line
`(the limit is queryguard_max_queries, the default budget of tests without one)`.
Empty, the default, means no default budget.

## Lock today's counts: the budget file

A suite that already exists rarely has a sensible number per test. Record
the numbers it has today, commit them, and every test that starts running
more queries fails from then on:

```bash
pytest --queryguard-budgets=queryguard-budgets.json --queryguard-update-budgets
git add queryguard-budgets.json
```

```ini
[pytest]
queryguard_budgets = queryguard-budgets.json
```

```json
{
  "format": 1,
  "budgets": {
    "tests/test_orders.py::test_list": 3,
    "tests/test_orders.py::test_detail": 2
  }
}
```

- A failure says where its limit comes from:
  `(the limit is the budget of this test in queryguard-budgets.json; if the new count is right, update it with --queryguard-update-budgets)`.
- The order is: a `max_queries` marker, then the file, then
  `queryguard_max_queries`.
- A test that runs fewer queries passes. Run the update again to lower its
  number (the summary says how many entries were added, removed, raised and
  lowered), so the file only tightens unless someone raises a number on
  purpose.
- An update on part of the suite (`-k`, a path, `--lf`) keeps the entries of
  the tests that did not run; an update on the whole suite drops the entries
  of tests that no longer exist. An update that stops early (`-x`, Ctrl-C, a
  collection error) does not write the file. It works the same with
  pytest-xdist.
- While the file is being updated its old numbers are not applied.
- The file has node ids, so a renamed or reparametrized test loses its entry
  until the next update.

## Query budgets with the `max_queries()` context manager

```python
from pytest_queryguard import max_queries


def test_checkout(service, cart):
    with max_queries(2):
        service.checkout(cart)
```

The block yields the same `QueryLog` as
[`record_queries()`](python-api.md), so the statements can be read after the block:

```python
def test_checkout_statements(service, cart):
    with max_queries(2) as log:
        service.checkout(cart)
    assert log.count == 2
    assert "INSERT" in log.queries[-1].sql
```

Only the statements inside the `with` block count. Above the limit it raises
`QueryBudgetExceeded`, a subclass of `AssertionError`. The context manager
does not depend on pytest, so it also works in a script or a benchmark. It
checks the budget only; N+1 detection is done by the plugin around the
whole test.

## Time budgets with `max_db_time`

```python
import pytest
from pytest_queryguard import max_db_time


@pytest.mark.max_db_time(0.25)  # seconds
def test_order_page(client):
    client.get("/orders/1")


def test_checkout(service, cart):
    with max_db_time(0.1):
        service.checkout(cart)
```

The limit is the time the database and the driver took for the statements,
measured around each cursor call, so slow Python code in the test does not
count. The failure shows the total and the slowest statements with the line
that issued them. A `max_queries` failure shows the total database time and
its three slowest statements too. Like `max_queries`, the budget fails
whatever `queryguard_mode` is set to. In `record_queries()`, each `Query` has
a `duration` (seconds), and the log has `db_time` and `slowest(n)`.

