# pytest-queryguard

[![CI](https://github.com/MaciejZiel/pytest-queryguard/actions/workflows/ci.yml/badge.svg)](https://github.com/MaciejZiel/pytest-queryguard/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pytest-queryguard)](https://pypi.org/project/pytest-queryguard/)
[![Python](https://img.shields.io/pypi/pyversions/pytest-queryguard)](https://pypi.org/project/pytest-queryguard/)
[![License](https://img.shields.io/pypi/l/pytest-queryguard)](https://github.com/MaciejZiel/pytest-queryguard/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-maciejziel.github.io-blue)](https://maciejziel.github.io/pytest-queryguard/)

A pytest plugin that fails a test when the SQLAlchemy 2 code it runs sends one
query per row (N+1), and enforces query budgets. Sync, async and FastAPI code;
nothing to import or configure. A test database holds a handful of rows, so
such code passes its tests and sends hundreds of queries in production. This
test passes without the plugin:

```python
def test_list_orders(session):
    assert len(list_orders(session)) == 50
```

With the plugin installed it fails, and points to the line to fix:

```
queryguard: N+1 detected

  Order.customer was lazy-loaded 50 times
  first at app/orders.py:10 in list_orders
    called from tests/test_orders.py:7 in test_list_orders
  parents loaded at app/orders.py:9 in list_orders
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
```

```bash
pip install pytest-queryguard
```

Python 3.10+, pytest 8+, SQLAlchemy 2.0.13+.

| Check | Default | Fails a test when |
|---|---|---|
| N+1 on a relationship | on | one relationship is lazy-loaded for 2 or more parent objects |
| Repeated statement | on | one `SELECT` runs 5 or more times: ORM, Core or `text()` |
| Column reloads | on | 5 or more objects reload a deferred or expired column one by one |
| Query and time budgets | per test | `@pytest.mark.max_queries(n)` or `max_db_time(seconds)` is exceeded |
| Row-by-row writes | opt-in | one `INSERT`, `UPDATE` or `DELETE` runs once per row from one line |
| Missing index, with the `CREATE INDEX` | opt-in | no index can serve a filter (PostgreSQL 16+, SQLite, MySQL, MariaDB) |
| Unused eager loads | experimental, off | a relationship is eager-loaded and never read |

Why not just `raiseload`? It blocks lazy loads where you add it to a model or
query; this plugin needs no code change and also reports what `raiseload` does
not cover. The two work together: see the [FAQ](https://maciejziel.github.io/pytest-queryguard/faq.html#how-does-this-relate-to-sqlalchemys-raiseload).

| Question | Answer | Details |
|---|---|---|
| What does it catch? | Lazy loads, plus loops of ORM, Core and `text()` statements, column reloads, row-by-row writes and missing indexes | [Comparison](https://maciejziel.github.io/pytest-queryguard/comparison.html): 45 scenarios against two other plugins |
| What does it cost? | Measured 2026-11-05 (0.5.0 against pytest-querycount 0.4.0 and queryspy 0.6.0; 300-test suite on file-based SQLite): about +2 % suite time; pytest-querycount +27 to +29 %, queryspy +40 to +43 % | [Performance](https://maciejziel.github.io/pytest-queryguard/performance.html#comparison-with-other-plugins) |
| Can a legacy suite adopt it? | Yes: warn first, record a baseline, then only new findings fail | [Legacy recipe](https://maciejziel.github.io/pytest-queryguard/recipes-legacy.html) |

For adoption: a [baseline](https://maciejziel.github.io/pytest-queryguard/baseline.html) so that only new
findings fail, [SARIF and a GitHub Action](https://maciejziel.github.io/pytest-queryguard/ci.html) for code
scanning, and per-finding [ignore patterns](https://maciejziel.github.io/pytest-queryguard/ignoring.html).
What it does not do: it sees only statements that go through SQLAlchemy, only
in the test body (not fixtures), and a finding says that a pattern happened,
not that production is slow. See [Limitations](https://maciejziel.github.io/pytest-queryguard/limitations.html).

Documentation: <https://maciejziel.github.io/pytest-queryguard/>, starting with
[Getting started](https://maciejziel.github.io/pytest-queryguard/getting-started.html) and
[A test failed: now what](https://maciejziel.github.io/pytest-queryguard/when-a-test-fails.html).

## 30-second example

The code behind that failure (`app/orders.py`):

```python
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order


def list_orders(session: Session) -> list[dict[str, object]]:
    rows = []
    for order in session.scalars(select(Order)).all():
        rows.append({"id": order.id, "customer": order.customer.name})
    return rows
```

With `joinedload(Order.customer)` in the query the same test passes. The whole
project (models, fixture, the fixed code, a loop of `session.get()` and a
query budget) is in [examples/sync-n-plus-one](https://github.com/MaciejZiel/pytest-queryguard/tree/main/examples/sync-n-plus-one), and a test
runs it and checks this output. For async code on SQLAlchemy 2.1 and newer,
also install `sqlalchemy[asyncio]`.

## What it finds

Every test is checked automatically, inside the test function:

- **relationship-load**: one relationship (`Order.customer`) is lazy-loaded
  for 2 or more different parent objects. The hint is `joinedload` for a
  many-to-one relationship and `selectinload` for a collection;
- **repeated-statement**: one `SELECT`, with its parameters and literals
  removed, runs 5 or more times. Core and `text()` statements count, not only
  ORM ones;
- **column-load**: 5 or more instances of one class each go back to the
  database for a deferred column (hint: `undefer`) or for attributes expired by
  `commit()` (hint: `expire_on_commit=False`);
- **write-loop** (opt-in): the same `INSERT`, `UPDATE` or `DELETE`, one row per
  statement, runs many times from one source line;
- **unused-eager-load** (experimental, off by default): a relationship is
  eager-loaded and never read;
- **unused-columns** (experimental, off by default): a query loads a wide row
  and the test reads few of its columns (hint: `load_only()`);
- **missing-index** (opt-in): see below.

All thresholds are options. A budget fails a test that runs too many
statements, and `max_db_time` one that spends too long in the database:

```python
@pytest.mark.max_queries(3)
def test_order_page(client):
    client.get("/orders/1")
```

The failure lists every statement with the line that issued it. FastAPI's
`TestClient` runs the application in another thread; queries from any thread
are counted. [How detection works](https://maciejziel.github.io/pytest-queryguard/how-detection-works.html) explains each
rule and why the thresholds are what they are.

## Missing indexes

A test database holds a few rows, so a query that reads the whole table is as
fast in CI as one that uses an index. With the index check on, every `SELECT`
in a test body is planned again with sequential scans switched off (PostgreSQL
16 and newer), explained with `EXPLAIN QUERY PLAN` (SQLite), or explained with `EXPLAIN` (MySQL and MariaDB). If the plan
still reads a whole table to apply a filter, no index can serve it, however
small the table:

```bash
pytest --queryguard-index-check
```

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

The index advisor suggests the `CREATE INDEX` and the Alembic call; with the
hypopg extension in the test database it also says whether the planner would
use the index and what the estimated cost would be before and after. The
check is off by default. See [PostgreSQL](https://maciejziel.github.io/pytest-queryguard/index-check.html),
[SQLite](https://maciejziel.github.io/pytest-queryguard/index-check-sqlite.html), [MySQL and MariaDB](https://maciejziel.github.io/pytest-queryguard/index-check-mysql.html) (tested in CI on MySQL 8.4 and MariaDB 11.8) and the
[index advisor](https://maciejziel.github.io/pytest-queryguard/index-advisor.html); runnable projects are in
[examples/postgres-missing-index](https://github.com/MaciejZiel/pytest-queryguard/tree/main/examples/postgres-missing-index) and
[examples/sqlite-index-check](https://github.com/MaciejZiel/pytest-queryguard/tree/main/examples/sqlite-index-check).

## Adopting it on an existing project

- Record today's findings once, and only new ones fail:
  `pytest --queryguard-baseline=queryguard-baseline.json --queryguard-update-baseline`.
  See [Baseline](https://maciejziel.github.io/pytest-queryguard/baseline.html).
- `pytest --queryguard-sarif=queryguard.sarif` writes the findings as SARIF
  for GitHub code scanning, and the action in [action](https://github.com/MaciejZiel/pytest-queryguard/tree/main/action) runs pytest and
  uploads it. See [CI and GitHub Action](https://maciejziel.github.io/pytest-queryguard/ci.html) and
  [SARIF](https://maciejziel.github.io/pytest-queryguard/sarif.html).
- Leave out single findings with `@pytest.mark.allow_nplusone("Order.customer")`
  or `queryguard_ignore`; make kinds warn with `queryguard_fail_on` or
  `queryguard_mode = warn`. See [Ignoring findings](https://maciejziel.github.io/pytest-queryguard/ignoring.html).
- Read the statements of a block in a test with `record_queries()`. See
  [Reading the queries](https://maciejziel.github.io/pytest-queryguard/python-api.html).

```toml
[tool.pytest.ini_options]
queryguard_mode = "warn"                  # fail (default), warn or off
queryguard_repeat_threshold = 10
# the file must exist: create it first with --queryguard-update-baseline
queryguard_baseline = "queryguard-baseline.json"
queryguard_index_check = true
queryguard_index_ignore_tables = ["countries", "lookup_*"]
```

Every option, flag and marker is in the
[configuration reference](https://maciejziel.github.io/pytest-queryguard/configuration.html).

## Documentation

| Page | What is in it |
|---|---|
| [Getting started](https://maciejziel.github.io/pytest-queryguard/getting-started.html) | Install, a first test, async and FastAPI |
| [A test failed: now what](https://maciejziel.github.io/pytest-queryguard/when-a-test-fails.html) | Read a failure, fix it or silence it |
| [How detection works](https://maciejziel.github.io/pytest-queryguard/how-detection-works.html) | The rules, the thresholds and why |
| [Configuration reference](https://maciejziel.github.io/pytest-queryguard/configuration.html) | Ini options, flags, markers |
| [Budgets and timing](https://maciejziel.github.io/pytest-queryguard/budgets.html) | `max_queries`, `max_db_time` |
| [In-application mode](https://maciejziel.github.io/pytest-queryguard/in-app.html) | Experimental: the same rules per request in a running FastAPI, Starlette or Flask app; a development panel |
| [Baseline](https://maciejziel.github.io/pytest-queryguard/baseline.html), [CI and Action](https://maciejziel.github.io/pytest-queryguard/ci.html), [SARIF](https://maciejziel.github.io/pytest-queryguard/sarif.html) | Adoption and code scanning |
| [Recipes](https://maciejziel.github.io/pytest-queryguard/recipes.html) | FastAPI, Flask-SQLAlchemy, SQLModel, Litestar, pytest-asyncio, budgets, legacy projects, CI |
| [Index check](https://maciejziel.github.io/pytest-queryguard/index-check.html), [SQLite](https://maciejziel.github.io/pytest-queryguard/index-check-sqlite.html), [MySQL and MariaDB](https://maciejziel.github.io/pytest-queryguard/index-check-mysql.html), [advisor](https://maciejziel.github.io/pytest-queryguard/index-advisor.html) | Missing indexes |
| [Unused eager loads](https://maciejziel.github.io/pytest-queryguard/unused-eager-load.html), [unused columns](https://maciejziel.github.io/pytest-queryguard/unused-columns.html) | The experimental checks |
| [FAQ](https://maciejziel.github.io/pytest-queryguard/faq.html), [Limitations](https://maciejziel.github.io/pytest-queryguard/limitations.html) | `raiseload`, false positives, xdist, drivers |
| [Comparison](https://maciejziel.github.io/pytest-queryguard/comparison.html), [Migrating from nplusone](https://maciejziel.github.io/pytest-queryguard/migrating-from-nplusone.html) | Other tools |
| [Stability and versioning](https://maciejziel.github.io/pytest-queryguard/stability.html) | What is public API, deprecations |
| [Changelog](https://github.com/MaciejZiel/pytest-queryguard/blob/main/CHANGELOG.md), [examples](https://github.com/MaciejZiel/pytest-queryguard/tree/main/examples) | |

The pages are Markdown in [docs](https://github.com/MaciejZiel/pytest-queryguard/tree/main/docs); `scripts/build_docs.py` builds them
into a static site (see Development).

## How it compares

Other tools find N+1 queries too: [queryspy](https://github.com/sqla-native/queryspy),
[pytest-querycount](https://github.com/ldavidsm/pytest-querycount),
[nplusone](https://github.com/jmcarp/nplusone) (last release 2018),
[django-zeal](https://github.com/taobojlen/django-zeal) for Django and others.
The [comparison page](https://maciejziel.github.io/pytest-queryguard/comparison.html) has a feature matrix (dated, with what
was run and what was only read) and the results of running 45 scenarios against
queryspy 0.5.0 and pytest-querycount 0.4.0 (re-checked on 2026-10-13: no newer
releases). In short, in my runs: pytest-queryguard fails on N+1 with no
configuration (the other two need a flag or a marker), reported a loop of Core
`select()` statements and a loop with literal values that queryspy missed, and
lists the statements in a budget failure (queryspy's marker printed only the
counts). queryspy and pytest-querycount also report runs of 2 to 4 identical
statements, which pytest-queryguard does only when you set
`queryguard_small_repeat_threshold`. pytest-querycount has a PostgreSQL
missing-index check of its own, queryspy has middleware and a request panel for
running apps, which pytest-queryguard does not, and django-zeal covers Django.
Moving from nplusone: [migration guide](https://maciejziel.github.io/pytest-queryguard/migrating-from-nplusone.html).

## Overhead

On in-memory SQLite, the measurement adds about 9 us per Core statement and
about 31 us per ORM statement (+22 % and +40 %). The percentages are high
because an in-memory statement costs almost nothing; on a generated suite of
300 tests against file-based SQLite and against PostgreSQL the difference was
within run-to-run noise. Method, numbers and the cost of the opt-in checks:
[Performance](https://maciejziel.github.io/pytest-queryguard/performance.html) and the
[FAQ](https://maciejziel.github.io/pytest-queryguard/faq.html#does-it-slow-my-tests-down).

## Limitations

- SQLAlchemy only; raw DB-API connections (`sqlite3` and the like) and other
  ORMs (Django ORM, Peewee) are not seen, and nothing says so.
- Only the test function body is measured, not fixtures. One measurement is
  active per process; under pytest-xdist each worker measures its own tests.
- The PostgreSQL index check needs PostgreSQL 16 or newer and a sync engine or
  asyncpg; the SQLite, MySQL and MariaDB checks judge less precisely. It says that no index can
  serve a filter, not that an existing index is a good one.
- Writes never count towards the repeated-statement rule; the opt-in
  `write-loop` kind and budgets cover them.

The full list is on the [Limitations](https://maciejziel.github.io/pytest-queryguard/limitations.html) page.

## Development

The project uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run coverage run -m pytest
uv run coverage report
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run --group docs python scripts/build_docs.py --strict   # the docs site, into site/
```

The PostgreSQL tests run when `QUERYGUARD_PG_URL` points to a server, and are
skipped otherwise:

```bash
docker run -d --rm --name qg-postgres -p 55432:5432 -e POSTGRES_PASSWORD=pg postgres:17
QUERYGUARD_PG_URL=postgresql+pg8000://postgres:pg@localhost:55432/postgres uv run pytest
docker stop qg-postgres
```

Coverage is measured with `coverage run` rather than `pytest --cov`, because
`--cov` starts after pytest has imported the plugin and misses its import-time
lines. Third-party licenses are listed in
[3rdparty_licenses.md](https://github.com/MaciejZiel/pytest-queryguard/blob/main/3rdparty_licenses.md).

## License

MIT, see the LICENSE file.
