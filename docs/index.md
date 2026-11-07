# pytest-queryguard

A pytest plugin that fails tests with N+1 queries and enforces query budgets
for SQLAlchemy 2, in sync, async and FastAPI code. On PostgreSQL 16 and newer,
SQLite, MySQL and MariaDB it can also fail tests whose queries have no index
to use.

ORM code that looks correct can send one query per row instead of one query
per page. The tests pass, because a test database holds a handful of rows. In
production the same endpoint sends hundreds of queries. pytest-queryguard
watches the SQL each test sends, and when it sees the pattern it fails the
test and points to the line that caused it.

```
queryguard: N+1 detected

  Order.customer was lazy-loaded 50 times
  first at app/orders.py:10 in list_orders
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

It is on by default: there is nothing to import or configure. These pages
describe 0.8.0, see [Getting started](getting-started.md#install). They are
published from `main`, so an option added since 0.8.0 may already be here:
the [changelog](../CHANGELOG.md) lists those under Unreleased.

## Why this one

- **Zero configuration.** Every test is checked once it is installed; the
  other SQLAlchemy tools in the [comparison](comparison.md) need a marker,
  an option or a fixture before a test fails.
- **More than lazy loads.** Repeated statements from `session.get()` loops,
  Core and `text()`, deferred and expired column loads, and query budgets
  that list every statement with the line that sent it.
- **Missing indexes** on PostgreSQL, SQLite, MySQL and MariaDB, with a
  `CREATE INDEX` to add.
- **Adoption on a large suite:** a [baseline](baseline.md) so only new
  findings fail, and [SARIF](sarif.md) for GitHub code scanning.

What it does not do (Django, other ORMs) and where other tools are ahead:
[Comparison with other tools](comparison.md) and [Limitations](limitations.md).

## Where to go

The path most projects take, one page each:

1. [Getting started](getting-started.md): install, a first failing test and
   its fix, async code and FastAPI.
2. [A test failed: now what](when-a-test-fails.md): read the report, check the
   finding, fix it or leave it out on purpose.
3. [CI and GitHub Action](ci.md): findings as pull request annotations.
4. [Baseline](baseline.md): adopt it on an existing project; only new
   findings fail.
5. [Index check](index-check.md) and [index advisor](index-advisor.md):
   queries no index can serve, on [PostgreSQL](index-check.md),
   [SQLite](index-check-sqlite.md) and [MySQL and MariaDB](index-check-mysql.md).

Reference: [How detection works](how-detection-works.md),
[Configuration](configuration.md), [Budgets and timing](budgets.md),
[Ignoring findings](ignoring.md). Before you adopt it:
[FAQ](faq.md) and [Stability and versioning](stability.md).

The source and the issue tracker are on
[GitHub](https://github.com/MaciejZiel/pytest-queryguard).
