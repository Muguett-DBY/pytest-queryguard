# Reading the queries in a test

`record_queries()` records the SQL statements a block sends and lets the
test read them back:

```python
from pytest_queryguard import record_queries

from app.customers import fetch_customers
from app.orders_fixed import list_orders


def test_list_orders_loads_customers_with_the_orders(session):
    with record_queries() as log:
        list_orders(session)
    assert log.count == 1
    assert "JOIN customer" in log.queries[0].sql
    assert all(query.relationship is None for query in log)


def test_fetch_customers(session):
    with record_queries() as log:
        fetch_customers(session, [1, 2, 3])
    log.assert_no_nplusone(repeat_threshold=2)
```

The two tests import from different modules of
[the first test](getting-started.md#a-first-test) project: `list_orders` from
`app/orders_fixed.py`, which loads the customers with the orders, and
`fetch_customers` from `app/customers.py`, the version with the loop, which is
the one that fails the second test. Against the N+1 `app.orders` the first test
fails on `log.count == 1`.

`log.queries` is a tuple of `Query` objects, in the order the statements
ran. The log is also sized, iterable and indexable (`len(log)`,
`for query in log`, `log[i]` and `log[a:b]`) but not a
`collections.abc.Sequence`, because `log.count` is a number and not the
`count(value)` method of a sequence. An empty log is falsy. Each `Query` has:

- `sql`: the statement as SQLAlchemy sent it to the driver;
- `callsite`: a `CallSite` with `filename`, `lineno` and `function`, the
  first frame outside SQLAlchemy, pytest, the standard library,
  `site-packages` and the plugin, or `None`;
- `relationship`: the relationship the statement loaded, lazily or as the
  extra query of `selectinload`, such as `"Order.customer"`, or `None`;
- `duration`: seconds spent in the database and driver, measured around the
  cursor call, or `None` if the statement is still running or raised an error.

`log.count` is the number of statements. Both grow inside the block and
stay fixed after it. Statements from other threads are recorded too.

`log.db_time` is the total recorded database time. It grows as statements
finish inside the block and stays fixed after it, like the query details.
A statement still running when the block ends keeps `duration=None` in that
log; finishing later does not change the log's total. Join worker threads
inside the block to include their completed timings.

`QueryLog` is not constructible by users: `record_queries()` is the only way
to get one, and its constructor may change in any release. Use the class for
type hints only.

`log.assert_no_nplusone()` applies the plugin's N+1 rules to the block:
relationship loads, repeated statements and column loads, plus small repeats
and write loops when asked for. It raises `NPlusOneDetected`, a subclass of
`AssertionError`, with the plugin's message, with paths relative to pytest's
rootdir (outside pytest, to the working directory). The thresholds are
keyword-only arguments, with the same defaults as the ini options:

| Argument | Default | Ini option | Rule |
|---|---|---|---|
| `relationship_threshold` | `2` | `queryguard_relationship_threshold` | One relationship lazy-loaded for this many different parent objects. |
| `repeat_threshold` | `5` | `queryguard_repeat_threshold` | One `SELECT` that ran this many times. |
| `column_load_threshold` | `5` | `queryguard_column_load_threshold` | This many instances of one class each loading deferred or expired columns on their own (kind `column-load`). |
| `small_repeat_threshold` | `None` (off) | `queryguard_small_repeat_threshold` | An integer of at least 2 and below `repeat_threshold`: a statement that ran this many times from one line, each time with other parameters, counts too. |
| `write_loop_threshold` | `None` (off) | `queryguard_write_loop_threshold` | An integer of at least 2: this many single-row `INSERT`, `UPDATE` or `DELETE` statements of one shape from one line (kind `write-loop`). |
| `repeat_scope` | `"test"` | `queryguard_repeat_scope` | See below. |

An ini option set to `0` for "off" is `None` here.

`repeat_scope="session"` counts each `Session` on its own, like
`queryguard_repeat_scope = session` (see [Tests with several sessions or
requests](several-sessions.md)); the default is `"test"`.

It does not read the ini options, `queryguard_mode` or the `allow_nplusone`
marker; calling it is an explicit request. A project that sets
`queryguard_repeat_threshold = 10` in its configuration therefore has to pass
`repeat_threshold=10` here as well, or the helper keeps using 5 (the same for
`queryguard_relationship_threshold` and `relationship_threshold`). With the
`fetch_customers` loop from [the first test](getting-started.md#a-first-test), the
plugin accepts 3 lookups, because its threshold is 5, while the stricter
call above does not (`pytest --tb=short`, Python 3.13, SQLAlchemy 2.1):

```
tests/test_queries.py .F                                                 [100%]

=================================== FAILURES ===================================
_____________________________ test_fetch_customers _____________________________
tests/test_queries.py:18: in test_fetch_customers
    log.assert_no_nplusone(repeat_threshold=2)
E   pytest_queryguard.NPlusOneDetected: queryguard: N+1 detected
E   
E     the same statement ran 3 times
E     first at app/customers.py:9 in fetch_customers
E     kind: repeated-statement  label: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
E     fix: fetch all rows in one query (WHERE id IN (...)) instead of in a loop
=========================== short test summary info ============================
FAILED tests/test_queries.py::test_fetch_customers - pytest_queryguard.NPlusO...
========================= 1 failed, 1 passed in 0.05s ==========================
```

If pytest-queryguard hits an internal error, it stops recording and issues a
`QueryGuardInternalWarning` that names the listener that failed; the
session summary repeats it with the error and the line that raised it. A `record_queries()` block that ends after such
an error then warns that its log may be incomplete (the warning class is
exported from `pytest_queryguard`), and `log.assert_no_nplusone()` raises
`RuntimeError` instead of passing on a log that may be empty. This also holds
outside pytest and with `--queryguard-mode=off`. The warning is issued when
the block ends normally; a block left by an exception does not warn, so the
warning never replaces the exception, but its log is still fixed at that
point and `assert_no_nplusone()` still refuses it. An "error" warnings filter does not
turn this warning into an exception either.

What is public API, and what is not, is defined on the
[Stability and versioning](stability.md) page. In short: the names in
`pytest_queryguard.__all__` and the pytest options, markers and finding kinds
are public; the constructors of `QueryLog`, `Query` and `CallSite` are not.
`record_queries()` reads the queries; `max_queries()` and `max_db_time()` yield
the same `QueryLog`, so `with max_queries(3) as log:` also gives `log.count`,
`log.queries` and `log.slowest()`.

The `queryguard` fixture gives a `QueryLog` from
`queryguard.max_queries(n)` and `queryguard.num_queries(n)` blocks that
counts only the statements of the test's own thread or asyncio task, and
without `assert_no_nplusone()`, and the statements of the whole test as
`queryguard.queries` (see
[Budgets in a test with the `queryguard` fixture](budgets.md#budgets-in-a-test-with-the-queryguard-fixture)).

Recording changes nothing else. The plugin's N+1 detection, budgets and
index check see the same statements. Like `max_queries()`, `record_queries()`
also works outside pytest.

