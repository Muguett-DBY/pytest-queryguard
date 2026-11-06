# pytest-queryguard — design

> This is the original design for 0.1. Later releases changed some of what it
> says (for example how plugin errors are reported, and which statements the
> repeat rule counts); the [Changelog](../CHANGELOG.md) and the other pages
> describe the current behavior.

| | |
|---|---|
| Status | Accepted |
| Author | Maciej Zieliński |
| Date | 2026-10-11 |
| Target release | 0.1.0 |

## 1. Problem

ORM code that looks correct can send one query per row instead of one query
per page. This is the N+1 problem:

```python
orders = session.scalars(select(Order)).all()      # 1 query
for order in orders:
    print(order.customer.name)                      # +1 query per order
```

Test databases hold a handful of rows, so the test suite stays fast and green.
The problem shows up only in production, as a slow endpoint.

In the Python ecosystem, the tool that used to catch this, `nplusone`, still
gets about 442k downloads a month. Its last release was in 2018 and it does
not work with SQLAlchemy 2. Ruby solved the same problem with `bullet`, which
became a standard part of Rails projects. Python with SQLAlchemy 2 has no
maintained equivalent.

## 2. Goals

1. Fail a pytest test that triggers an N+1 query pattern through SQLAlchemy 2,
   in both sync and async code, without changes to application code.
2. Point to the line of application code that caused it and, where possible,
   to the fix (for example `selectinload(Order.customer)`).
3. Let a test declare a query budget and fail when it is exceeded.
4. Work with FastAPI's `TestClient` and with `AsyncSession`.
5. Install with `pip install pytest-queryguard` and work with no configuration.

## 3. Non-goals for 0.1.0

| Feature | Reason | Planned |
|---|---|---|
| Missing index detection (`EXPLAIN (GENERIC_PLAN)` with `enable_seqscan=off`) | Postgres-only, needs a real database in CI | 0.2.0, see [design-0.2.md](design-0.2.md) |
| Django ORM | Separate integration; Django already has `assertNumQueries` | Later, on demand |
| Unused eager load detection | Lower value, more false positives | Later |
| Query count snapshots across runs | Needs a storage format and update workflow | Later |
| Runtime use outside tests (production middleware) | Different audience and risk profile | No |

## 4. User-facing behaviour

### 4.1 Automatic N+1 detection

After installation the plugin is active. A test that triggers N+1 fails:

```
FAILED tests/test_orders.py::test_list_orders
queryguard: N+1 detected

  Order.customer was lazy-loaded 50 times
  first at app/api/orders.py:42 in list_orders
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(selectinload(Order.customer))
```

The `sql:` line shows the first lazy-load statement, shortened to 200
characters; it is left out when no statement was recorded for the
relationship.

When the cause is repeated SQL rather than a relationship load (the
statement is shortened to 200 characters in the same way):

```
queryguard: N+1 detected

  the same statement ran 50 times
  first at app/services/orders.py:18 in fetch_customers
  SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: fetch all rows in one query (WHERE id IN (...)) instead of in a loop
```

### 4.2 Query budgets

```python
import pytest
from pytest_queryguard import max_queries

@pytest.mark.max_queries(3)
def test_list_orders(client):
    client.get("/orders")

def test_checkout(service, cart):
    with max_queries(2):
        service.checkout(cart)
```

On failure the message lists the number of queries and every statement
with the code location that issued it.

### 4.3 Opting out

```python
@pytest.mark.allow_nplusone
def test_legacy_report(): ...
```

### 4.4 Configuration

In `pyproject.toml`, `pytest.ini` or `setup.cfg`:

| Option | Values | Default | Meaning |
|---|---|---|---|
| `queryguard_mode` | `fail`, `warn`, `off` | `fail` | `warn` reports N+1 as a pytest warning instead of failing |
| `queryguard_relationship_threshold` | integer ≥ 2 | `2` | Lazy loads of one relationship treated as N+1 |
| `queryguard_repeat_threshold` | integer ≥ 2 | `5` | Executions of one statement treated as N+1 |

Command-line options: `--queryguard-mode` overrides the ini value;
`--queryguard-report` prints, at the end of the session, the tests with the
most queries.

Query budgets (`max_queries`) always fail, regardless of `queryguard_mode`,
because the user asked for them explicitly.

## 5. Design

### 5.1 Components

```
src/pytest_queryguard/
  plugin.py      pytest hooks, options, markers, reporting
  collector.py   QueryCollector: records events for the current test
  detectors.py   pure functions: list of events -> list of findings
  listeners.py   SQLAlchemy event listeners, installed once per session
  callsite.py    finds the first stack frame in user code
  messages.py    formats findings into failure text
  api.py         max_queries() context manager
```

Detectors are pure functions over recorded events, so they can be tested
without a database or pytest.

### 5.2 Capturing queries

Two global listeners are installed when the plugin is configured:

- `before_cursor_execute` on the `Engine` class. Every SQL statement is
  recorded with its text, a normalized form (literals and parameters
  removed) and the call site. For an ORM `SELECT` the listener reads the
  ORM's options of that execution (what `ORMExecuteState` reads in a
  `do_orm_execute` listener): a relationship load's path gives the
  relationship (for example `Order.customer`), recorded with the identity of
  the parent object (`_lazy_loaded_from`); a column load's object comes from
  `_refresh_state`.
- `after_cursor_execute` on the `Engine` class times the statement.

A `do_orm_execute` listener would see the same, but any listener of that
event makes SQLAlchemy prepare each ORM statement twice.

Class-level listeners cover every session and engine, including the sync
engine behind `AsyncEngine`, so users do not register anything.

Both listeners do nothing unless a collector is active.

### 5.3 Scope of a measurement

A collector is active only around the test function body
(`pytest_runtest_call`). Queries from fixture setup and teardown are not
counted, so a fixture that inserts 100 rows does not trigger N+1.

The active collector is a process-wide variable, not thread-local or
context-local, because FastAPI's `TestClient` runs the application in a
separate thread. Tests in one process run sequentially, so there is one
active collector at a time. `pytest-xdist` workers are separate processes
and need no special handling for detection and budgets. The
`--queryguard-report` summary needs the counts in a single process, so
under xdist the controller prints
`queryguard report: not available with pytest-xdist` instead.

### 5.4 Detection rules

- Relationship N+1: the same relationship (`Order.customer`) lazy-loaded
  for at least `queryguard_relationship_threshold` distinct parent
  objects within one measurement.
- Repeated SQL: the same normalized statement executed at least
  `queryguard_repeat_threshold` times within one measurement, excluding statements already
  reported as relationship N+1 and excluding every `INSERT`, `UPDATE` and
  `DELETE`. The cursor-level event cannot tell a unit-of-work flush from a
  write the application issued itself, so all writes are left out, not only
  those of the unit of work. Write loops are caught by budgets, not as N+1.

### 5.5 Call sites

The call site is the first stack frame whose file is not inside SQLAlchemy,
pytest, the standard library, `site-packages`, or this plugin. Stack
inspection runs only while a collector is active.

### 5.6 Errors in the plugin

Any exception raised inside a listener is caught, reported once as a pytest
warning, and disables the plugin for the rest of the session. The plugin
must never break a test suite because of its own bug.

## 6. Testing strategy

- Unit tests for detectors, normalization, call sites and messages, with no
  database.
- Integration tests with `pytester` (pytest's official harness for testing
  plugins): each scenario is a pair, the N+1 version must fail and the fixed
  version (`selectinload`, `joinedload`, `IN` query) must pass.
- Async tests with `AsyncSession` and `aiosqlite`.
- An end-to-end test with a small FastAPI app and `TestClient`.
- SQLite in CI, so no database service is required.
- CI matrix: Python 3.10 to 3.14, SQLAlchemy 2.0.x and latest 2.x; plus
  `ruff` and `mypy --strict`.
- Coverage target: 90% of lines, measured with `coverage run -m pytest`.
  `pytest --cov` starts measuring after pytest has imported the
  entry-point plugin, so it misses the plugin's import-time lines.

## 7. Packaging and release

- Name `pytest-queryguard` (free on PyPI as of 2026-10-11), import name
  `pytest_queryguard`, registered through the `pytest11` entry point.
- Runtime dependencies: `pytest>=8`, `sqlalchemy>=2.0.13`. Test dependencies:
  `aiosqlite`, `fastapi`, `httpx`. All are permissively licensed and listed
  in `3rdparty_licenses.md`.
- MIT license, `uv` for development, semantic versioning, `CHANGELOG.md` in
  Keep a Changelog format.
- Releases from GitHub Actions through PyPI Trusted Publishing, triggered by
  a version tag.
- The repository stays private until 0.1.0 is ready.

## 8. Risks

| Risk | Mitigation |
|---|---|
| False positives on legitimate loops | Thresholds, `allow_nplusone` marker, `warn` mode |
| Stack inspection slows large suites | Only while a collector is active; about 15 µs per statement, measured on 1500 tests × 20 statements during review; a benchmark test is planned for 0.2 |
| SQLAlchemy changes internal attributes | Only public, documented event APIs are used; CI tests the latest 2.x |
| Repeated-SQL rule misreads batch jobs | Writes are excluded from N+1; budgets cover them |

## 9. Success criteria for 0.1.0

- Catches N+1 in sync, async and FastAPI examples, with a fix hint for
  relationship loads.
- Zero findings on the fixed versions of the same examples.
- No configuration needed for the default behaviour.
- Green CI on the full matrix and published on PyPI.
