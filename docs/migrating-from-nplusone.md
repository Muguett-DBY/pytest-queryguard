# Migrating from nplusone

[nplusone](https://github.com/jmcarp/nplusone) detects N+1 queries for Django,
SQLAlchemy and Peewee. Its last release, 1.0.0, is from 2018-05-21. This page
is for projects that used it with SQLAlchemy and now move to SQLAlchemy 2 and
pytest-queryguard. If you use nplusone with Django, see
[Comparison](comparison.md) for tools that cover it; pytest-queryguard does not.

## Why a migration is needed

I installed nplusone 1.0.0 next to several SQLAlchemy versions (Python 3.13
for the first three rows, Python 3.9 for the last) and ran the same N+1 loop:

| SQLAlchemy | What happened |
|---|---|
| 2.1.4 | `import nplusone.ext.sqlalchemy` fails with `AttributeError: module 'sqlalchemy.orm.strategies' has no attribute 'LazyLoader'` |
| 2.0.54 | Imports. A loop over `session.query(Order)` fails with `AttributeError: 'Query' object has no attribute '_offset'`. A loop over `session.scalars(select(Order))` runs and reports nothing |
| 1.4.54 | The same two outcomes as 2.0.54 |
| 1.3.24 | Works: `NPlusOneError: Potential n+1 query detected on Order.customer` |

So moving to SQLAlchemy 2 means moving off nplusone. These are the results of
one small project on one date, not a full compatibility test of nplusone.

## Concept mapping

| nplusone | pytest-queryguard |
|---|---|
| `with profiler.Profiler():` around a test | Nothing: every test body is measured by default |
| `Profiler()` raises `NPlusOneError` | A failing test: `queryguard: N+1 detected` (`queryguard_mode = fail`, the default) |
| `NPLUSONE_RAISE = False` and logging | `queryguard_mode = warn`: a `QueryGuardWarning` and the test passes |
| Label `n_plus_one` | Finding kind `relationship-load` (also `repeated-statement`, `column-load`, `write-loop`) |
| Label `unused_eager_load` | Finding kind `unused-eager-load`, experimental, off by default: `queryguard_unused_eager_load = warn` |
| `NPLUSONE_WHITELIST = [{"label": ..., "model": "Order", "field": "customer"}]` | `queryguard_ignore = ["relationship-load Order.customer"]` |
| `signals.ignore(signals.lazy_load)` block | `with pytest_queryguard.ignore("relationship-load"):` ([the `ignore()` block](ignoring.md#the-ignore-block)), a marker, `@pytest.mark.allow_nplusone("Order.customer")`, or an ini pattern |
| Whitelist patterns with `fnmatch` | The same: `fnmatch` patterns in markers and `queryguard_ignore` |
| A profiler around one block | `record_queries()` and `log.assert_no_nplusone()` |
| Flask, Django, WSGI middleware | Not covered: pytest-queryguard runs in tests only |

## Before and after

The same project, the same test, in nplusone's world and in
pytest-queryguard's. The "before" run is Python 3.9, SQLAlchemy 1.3.24,
nplusone 1.0.0 and pytest 8.4.2. The "after" run is Python 3.13,
SQLAlchemy 2.1.4 and pytest 9.1.1.

### Before

`conftest.py` turns nplusone on around every test:

```python
import pytest
from nplusone.core import profiler
import nplusone.ext.sqlalchemy  # noqa: F401  (patches SQLAlchemy)


@pytest.fixture(autouse=True)
def nplusone_profiler():
    with profiler.Profiler():
        yield
```

```python
# tests/test_orders.py
from models import Order


def test_customer_names(session):
    names = [order.customer.name for order in session.query(Order)]
    assert names == ["c1", "c2", "c3"]
```

```
E   nplusone.core.exceptions.NPlusOneError: Potential n+1 query detected on `Order.customer`
=========================== short test summary info ============================
FAILED test_orders.py::test_customer_names - nplusone.core.exceptions.NPlusOn...
1 failed in 0.09s
```

### After

Delete the fixture and the nplusone import. Move the model to SQLAlchemy 2 and
the query to `select()`:

```python
# tests/test_orders.py
from sqlalchemy import select

from models import Order


def test_customer_names(session):
    names = [order.customer.name for order in session.scalars(select(Order))]
    assert names == ["c1", "c2", "c3"]
```

```
queryguard: N+1 detected

  Order.customer was lazy-loaded 3 times
  first at test_orders.py:7 in test_customer_names
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
=========================== short test summary info ============================
FAILED test_orders.py::test_customer_names - Failed: queryguard: N+1 detected
1 failed in 0.05s
```

The message names the line in the test and what to change. With
`select(Order).options(joinedload(Order.customer))` the test passes.

### Whitelist

Before, in `conftest.py`:

```python
with profiler.Profiler(
    whitelist=[{"label": "n_plus_one", "model": "Order", "field": "customer"}]
):
    yield
```

(the test passes: `1 passed`). After, either in the test:

```python
@pytest.mark.allow_nplusone("Order.customer")
def test_customer_names(session): ...
```

or for the whole suite, in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
queryguard_ignore = ["relationship-load Order.customer"]
```

Both give `1 passed`.

### Warn first

nplusone logged by default and raised only with `NPLUSONE_RAISE`. To start
with warnings:

```console
$ pytest --queryguard-mode=warn
test_orders.py::test_customer_names
  .../test_orders.py:6: QueryGuardWarning: queryguard: N+1 detected
  ...
1 passed, 1 warning in 0.03s
```

For a project that already has many findings, a [baseline](baseline.md) lets
you turn failing on for new findings only.

### A block instead of a profiler

```python
from pytest_queryguard import record_queries


def test_customer_names_in_a_block(session):
    with record_queries() as log:
        [order.customer.name for order in session.scalars(select(Order))]
    log.assert_no_nplusone()
```

`assert_no_nplusone()` raises `NPlusOneDetected` with the same message. It does
not read the ini options; see [Reading the queries](python-api.md).

## Things that behave differently

- nplusone's `Profiler` measures whatever runs inside its `with` block; with
  an autouse fixture that is the whole test including other fixtures'
  setup. pytest-queryguard measures only the test function body.
- A lazy load of a many-to-one relationship for an object that is already in
  the session's identity map sends no SQL and is not a finding. In both tools
  the test data has to be loaded fresh (my examples call `session.expunge_all()`
  before the loop).
- The default threshold is 2 different parents, and repeated statements need
  5 runs; both can be changed. See [How detection works](how-detection-works.md).
- Only what I ran is covered here: nplusone's generic `Profiler` on
  SQLAlchemy 1.3.24. I did not run its Flask, Django or Peewee integrations.
