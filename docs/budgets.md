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
it `n`; since 0.8.0 `n=3` still works but emits a `QueryGuardDeprecationWarning`,
and it will be removed in 2.0 (see [Stability](stability.md#deprecation-policy)).
Write `max_queries(3)` or `max_queries(limit=3)` instead.

`@pytest.mark.queryguard_max_queries(3)` (since 0.8.0) is the same marker under
a name only this plugin reads. As with any marker, the closest one counts (the
function's over the class's or the module's); when one place has both
spellings, `queryguard_max_queries` counts. Use it when another plugin that reads `max_queries` is installed too,
see [below](#pytest-querycount-and-the-max_queries-marker).

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
`(the limit is the queryguard_max_queries ini option, the default budget of tests without a marker)`.
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
  of tests that no longer exist. A test that was skipped, failed, or errored
  in setup keeps its entry: a test that stopped halfway would otherwise get a
  number below what it runs when it passes. An update that stops early
  (`-x`, Ctrl-C, a collection error) does not write the file. It works the
  same with pytest-xdist.
- A test with its own `max_queries` marker has no entry: the marker is the
  budget.
- While the file is being updated neither its old numbers nor
  `queryguard_max_queries` are applied, so the update passes.
- An update replaces a file it cannot read (a merge conflict) only when it
  covers the whole suite, and says so; a partial update leaves such a file
  alone.
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

## Budgets in a test with the `queryguard` fixture

```python
def test_checkout(queryguard, service, cart):
    with queryguard.max_queries(2):
        service.checkout(cart)


def test_order_page(queryguard, client):
    with queryguard.num_queries(3) as log:  # exactly 3
        client.get("/orders/1")
    assert "JOIN" in log.queries[0].sql
    assert queryguard.count >= 3  # every statement of the test so far
```

Its blocks count only the statements of the test's own thread, or of its
asyncio task, and of work run in a copy of their context: statements that a
thread already running runs at the same time, such as a background worker
or a server thread, do not count (a `TestClient` request does, see below). That
makes them the equivalent of pytest-django's `django_assert_max_num_queries`
and `django_assert_num_queries`:

- `queryguard.max_queries(limit)` fails when the block runs more than
  `limit` statements.
- `queryguard.num_queries(expected)` fails unless it runs exactly
  `expected`; fewer fail too.
- Both raise `QueryBudgetExceeded` with every statement of the block and the
  line that issued it, and yield a `QueryLog` of the block (see
  [Reading the queries](python-api.md)). Blocks can be nested, each counts
  its own statements, and an exception raised in the block wins over the
  budget.
- The blocks count statements only. `log.assert_no_nplusone()` raises
  `RuntimeError` on their log; use `record_queries()` for that. A
  `Query.relationship` is filled in while the plugin measures the test, so
  not with `queryguard_mode = off`.
- Tasks created inside a block, threads started with `asyncio.to_thread()`
  or `contextvars.copy_context().run()`, and, on a free-threaded build of
  Python 3.14, every thread started inside it, run in a copy of the block's
  context and count in it until the block ends. A task or thread that was
  running before the block does not count. `AsyncSession` and
  `create_async_engine` statements count in the task that awaits them.
- `queryguard.queries` is a tuple of `Query` objects, the statements the
  test function has run so far, and `queryguard.count` their number. Unlike
  the blocks, they count every thread, as the plugin and `record_queries()`
  do, and so work with any async test runner (pytest-asyncio, anyio).
  Statements of fixtures, in their setup and teardown, are not in it, and
  reading them before the test function starts (in a fixture's setup)
  raises `RuntimeError`.

A FastAPI or Starlette `TestClient`, such as the `client` above, runs each
request in another thread but with a copy of the test's context, so the
endpoint's statements count in the block, whether the client was opened in a
fixture or in the test (checked by hand with anyio 3.7 and 4). A thread that was
already running and does not copy the context, such as a live server or a
worker pool, does not count: to count every thread in a block, use the
`max_queries()` function or the marker above. Like them, the
fixture fails whatever `queryguard_mode` is set to; with `queryguard_strict`
an N+1 inside a block raises `NPlusOneDetected` at the access first, with
the budget failure under it when the block had already gone over. After
an internal error of the plugin, a block and the fixture warn with
`QueryGuardInternalWarning` that their statements may be incomplete, and
`num_queries()` no longer fails on too few statements. In type hints the
fixture is `pytest_queryguard.QueryGuardFixture`.

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
`@pytest.mark.queryguard_max_db_time(0.25)` (since 0.8.0) is the same marker
under the plugin's own prefix, chosen the same way as `queryguard_max_queries`.

## pytest-querycount and the `max_queries` marker

[pytest-querycount](https://github.com/ldavidsm/pytest-querycount) registers a
`max_queries` marker of its own. With both plugins installed (checked with
pytest-querycount 0.4.0), each reads every `max_queries` marker:

- `@pytest.mark.max_queries(3)` is enforced twice, by each plugin with its own
  count. When pytest-querycount fails the test first, its failure is the one
  shown, since the original failure wins (see above).
- `@pytest.mark.max_queries(limit=3)` stops the whole run at collection:
  pytest-querycount takes only a positional limit or `n=` and reports
  `@pytest.mark.max_queries needs a number`.
- `pytest --querycount-write-budgets` writes `max_queries` markers into your
  test files, and pytest-queryguard then enforces them too.

`max_db_time` and the other markers of pytest-queryguard do not clash. To keep
the budgets of the two plugins apart, use the prefixed markers for
pytest-queryguard, which pytest-querycount does not read:

```python
@pytest.mark.queryguard_max_queries(3)  # pytest-queryguard only
@pytest.mark.max_queries(5)  # pytest-querycount (and nothing else)
def test_order_page(client):
    client.get("/orders/1")
```

With both markers on a test, pytest-queryguard uses `queryguard_max_queries`
and ignores `max_queries`. While pytest-querycount is installed,
`max_queries(n=3)`, its own spelling, does not emit the deprecation warning of
`n=`. Or turn one plugin off for a run with
`-p no:querycount` or `-p no:queryguard`.

