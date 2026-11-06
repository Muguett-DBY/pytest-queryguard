# Missing-index check (PostgreSQL)

A test database holds a few rows, so a query that reads the whole table is
as fast in CI as one that uses an index. The missing index shows up in
production, once the table has grown. With the index check on, every
`SELECT` in a test body is planned again by PostgreSQL with sequential scans
switched off. If the plan still reads a whole table, or a whole index, to
apply the statement's filter, no index can serve that filter, however small
the table, and the test fails.

The check is off by default. Turn it on for a run:

```bash
pytest --queryguard-index-check
```

or for the project, with `queryguard_index_check = true` (see
[Configuration](configuration.md)).

The example below uses a real PostgreSQL 17 server through pg8000. All
output is copied from a run with Python 3.13, SQLAlchemy 2.1 and pytest 9.1, with
`queryguard_hypopg = off` (without it, one notice at the end of the session says how
to install the hypopg extension, see the [index advisor](index-advisor.md)).

`app/models.py`:

```python
class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
```

`app/orders.py`:

```python
def orders_of(session: Session, customer_id: int) -> Sequence[Order]:
    stmt = select(Order).where(Order.customer_id == customer_id)
    return session.scalars(stmt).all()
```

`tests/test_orders.py` (the `session` fixture creates the tables in
PostgreSQL and adds 6 orders):

```python
def test_orders_of(session):
    assert len(orders_of(session, 1)) == 3
```

`pytest --queryguard-index-check`:

```
tests/test_orders.py F                                                   [100%]

=================================== FAILURES ===================================
________________________________ test_orders_of ________________________________
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
=========================== short test summary info ============================
FAILED tests/test_orders.py::test_orders_of - Failed: queryguard: missing index
============================== 1 failed in 0.27s ===============================
```

With `mapped_column(ForeignKey("customer.id"), index=True)` the same test
passes:

```
tests/test_orders.py .                                                   [100%]

============================== 1 passed in 0.19s ===============================
```

Async engines with asyncpg are checked the same way, on the test's own
connection and transaction. Rows and tables the test has not committed
are visible to the check. The same model, with the query in async code:

```python
async def orders_of(session: AsyncSession, customer_id: int) -> Sequence[Order]:
    stmt = select(Order).where(Order.customer_id == customer_id)
    return (await session.scalars(stmt)).all()
```

With a test that adds 6 orders in an uncommitted `AsyncSession` on
`postgresql+asyncpg://` and calls `orders_of(session, 1)` through
`asyncio.run`, `pytest --queryguard-index-check` prints (Python 3.13,
SQLAlchemy 2.1, asyncpg 0.31, PostgreSQL 17):

```
________________________________ test_orders_of ________________________________
queryguard: missing index

  orders is read in full: Seq Scan with filter (orders.customer_id = $1)
  first at app/orders.py:11 in orders_of
  kind: missing-index  label: orders
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#missing-index
  sql: SELECT orders.id, orders.customer_id FROM orders WHERE orders.customer_id = $1::INTEGER
  fix: CREATE INDEX ON orders (customer_id)
  alembic: op.create_index("ix_orders_customer_id", "orders", ["customer_id"])

  a sequential scan is fine on a small lookup table:
  list it in queryguard_index_ignore_tables
```

Other async drivers, such as psycopg's async mode, are not checked; the
session summary says so.

What is reported:

- a sequential scan with a filter: the `WHERE` condition has no index.
  Correlated subqueries and `EXISTS` show up here too, through the filter
  of the subquery's scan;
- a scan of a whole index with a filter that is an equality (`=` or
  `IN`) on one column and no index condition. The planner picks it for
  `WHERE customer_id = ? ORDER BY id LIMIT 10` to avoid the sequential scan.
  Pagination with a range or `IS NULL` filter (`WHERE total > 0 ORDER BY id
  LIMIT 20`) walks the primary key on purpose and is not reported. A whole
  index read as a bitmap, which PostgreSQL may choose before `ANALYZE` for
  an `INCLUDE` index or a partial index (`... WHERE deleted_at IS NULL`),
  counts the same way. A bitmap that combines several indexes (`OR`, or
  `AND` of indexed columns) is not reported;
- an index condition, an equality on one column, that leaves out the first
  column of a multicolumn btree index: with only an index on
  `(tenant_id, status)`, `WHERE status = ?` reads the whole index on
  PostgreSQL 16 and 17. PostgreSQL 18 can skip-scan such an index, so this
  case is not checked there. BRIN and GIN indexes serve any of their
  columns and are not judged this way.

The last two cases walk an index that exists, so the finding does not say the
table is read in full. It reads `rw_link scans the whole index rw_link_pkey:
the index condition (rw_link.hero_id = $1) does not match the start of any
index` (or `the filter`), as the SQLite check words it, and the fix is still
the `CREATE INDEX` for the filtered column. When every finding of a test is of
this kind, the closing note speaks of a whole-index scan instead of a
sequential scan.

A table with a partial index is judged with one exception. The check
plans with a generic plan, where bound values are parameters, so it cannot
tell whether `... (x) WHERE status = 'open'` serves `status = $1 AND x = $2`
for the value your test binds. When every column of such an index's
predicate is filtered by the statement and one of them is compared with a
parameter, the statement is not reported and gets no advice. The session
summary counts those statements:
`N statements skipped: a partial index on <table> may serve them; the generic plan cannot tell`.
A predicate that the
statement fixes with a literal (`deleted_at IS NULL`) is decided by the
generic plan and is judged as before.

A partitioned table is reported once, under its own name, although the plan
reads its partitions: `CREATE INDEX ON events (kind)` creates the index on
every partition, also on those attached later, while an index on one
partition leaves the others without it. The condition keeps the alias the
plan gives the partition (`events_1.kind = $1`). A statement that names a
partition itself (`SELECT * FROM events_2025 WHERE ...`) is reported under the
partitioned table too, with the partition as the alias in the condition
(`events_2025.kind = $1`). Partitions at every level of a multi-level
partitioned table, and the default partition, are reported under the table at
the top. `queryguard_index_ignore_tables` matches the partitioned table's name.
PostgreSQL cannot run `CREATE INDEX CONCURRENTLY` on a partitioned table; on a
large production table, create the index `ON ONLY` the partitioned table,
create it `CONCURRENTLY` on each partition and attach those with
`ALTER INDEX ... ATTACH PARTITION`.

This changed in the release after 0.4.0rc2: earlier versions reported each
partition under its own name. An entry that names a partition stops matching,
and the finding appears under the partitioned table: in
`queryguard_index_ignore_tables`, in `allow_missing_index`, in an
`queryguard_ignore` marker or pattern with the `missing-index` kind,
and in a baseline file. Replace the partition's name with the partitioned
table's, for example `allow_missing_index("events_2025", "events_2026")` with
`allow_missing_index("events")`, and record the baseline again with
`--queryguard-update-baseline` (see [Baseline](baseline.md)). A partition that
the plan reads for another condition is still its own finding, named after
the partitioned table. Table inheritance (`INHERITS`) is
different: an index on the parent does not cover its children, so the parent
and each child read in full are separate findings.

Not reported: a sequential scan without a filter, such as `SELECT * FROM t`,
`SELECT count(*) FROM t` or a join without a `WHERE`, because the statement
reads the whole table anyway; a sequential scan whose filter is only
not-equal comparisons against parameters or other columns (`<> $1`,
`NOT IN ($1, $2)`), because no index serves them, whether the column is
indexed or not; scans of PostgreSQL's own catalogs. One more condition an
index could serve, as in `a = $1 AND b <> $2`, makes the scan reportable
again. A not-equal against a constant written into the SQL, such as
`status <> 'done'`, stays reported: a partial index such as
`CREATE INDEX ON orders (id) WHERE status <> 'done'` serves it, and pays off
when the excluded value is the common one. Whether a join reads
its unindexed side in full depends on table statistics, so join conditions on
their own are never reported.

The `fix:` line names a column only when the condition compares one plain
column with `=`, `<`, `<=`, `>` or `>=`. PostgreSQL prints a `::text` cast
for `varchar` columns compared with text, which an index on the column
still serves, so the check reads the column types from the catalog and
accepts that cast for `varchar` columns only. A column inside a function or
another cast, such as `total::int = ?`, or `id::text = ?` on an integer,
enum or `char(n)` column, gets
`add an index that matches this condition (an expression index may be needed)`,
because an index on the plain column would not help there.

Each distinct statement is explained once per test (again after DDL
changes the catalog), inside a savepoint on
the connection that ran it, so the check sees tables and rows the test has
not committed yet and leaves the test's transaction as it was. On an
AUTOCOMMIT connection the check also tries the savepoint first, so a
transaction the test opened with its own `BEGIN` is left open; only a
connection with no transaction open gets a `BEGIN` ... `ROLLBACK` of the
check's own. That first savepoint fails on such a connection, so the server
log shows `SAVEPOINT can only be used in transaction blocks` once per
explained statement; it is expected and changes nothing. Statements
from fixtures are not checked, like for N+1 detection. Statements that
start with comments, such as sqlcommenter tags, are checked too.

`EXPLAIN` does not run the statement, but the planner evaluates calls to
`IMMUTABLE` functions with constant arguments while it plans. Such a function
runs again for each plan the check (and the [advisor](index-advisor.md), when
it verifies a suggestion) asks for, and a side effect that is not
transactional, such as `nextval()`, is not undone by the savepoint. That only
concerns functions that are marked `IMMUTABLE` but have side effects; see
[Limitations](limitations.md).

The check costs a few database round trips per distinct statement per test:
open the savepoint, set the planner settings, `EXPLAIN` and roll back. The
first column of the btree indexes a plan uses and the `varchar` columns of
tables cast to text are read from the catalog once per engine for the whole
session. Any statement that may change them, such as `CREATE INDEX`,
`DROP TABLE` or `ALTER TABLE`, clears that cache, in a test or a fixture,
and so does the end of a transaction that ran one. A suite that runs
`create_all` and `drop_all` around every test therefore clears the cache
every test and gets no speed-up from it. With pg8000 against
PostgreSQL 17 in a local Docker container, a suite of 100 tests that each
run the same 10 indexed statements on a session-scoped engine
([scripts/bench_index_check.py](https://github.com/MaciejZiel/pytest-queryguard/blob/main/scripts/bench_index_check.py)) took
1.2–1.8 s without the check and 2.0–2.9 s with it, over 12 runs: about
1 ms per statement. Most of that is round-trip time, so it is lower with a
faster connection to the database.

A sequential scan is the right plan for a small lookup table, also in
production. Exclude such tables with `queryguard_index_ignore_tables`, one
test with `@pytest.mark.allow_missing_index`, or only some tables in one
test with `@pytest.mark.allow_missing_index("countries", "currencies")`.
Findings follow `queryguard_mode`: `warn` turns them into warnings and
`off` turns the check off. The `queryguard_ignore` marker without arguments does not
affect the index check, but with patterns it matches
missing-index findings the same way the `queryguard_ignore` option does
(`@pytest.mark.queryguard_ignore("audit_log", kind="missing-index")`).

## PostgreSQL versions

| PostgreSQL | index check | trailing-column rule | notice in the session summary |
|---|---|---|---|
| 13 and older | not run | - | `PostgreSQL 13.x statements were not checked: the index check needs PostgreSQL 16 or newer` |
| 14, 15 | not run: `EXPLAIN (GENERIC_PLAN)` is new in 16 | - | the same, with the version |
| 16, 17 | run | on | none |
| 18 | run | off: 18 can skip-scan the leading column | none |
| 19 and newer | run | off | `PostgreSQL 19.0 is newer than the versions the index check is tested on (16 to 18): its statements are checked, but a finding may be wrong or missing; please report one that is` |

Each notice is printed once per session, not per statement, and never fails a
test. N+1 detection, budgets and the other rules do not depend on the server
version.

What was tested, on 2026-10-13, in local Docker containers from the official
`postgres` images with the PGDG package `postgresql-<major>-hypopg` (hypopg
1.4.3): PostgreSQL 14.24, 15.19, 16.15, 17.11 and 18.6, with pg8000 (sync) and
asyncpg (async). CI runs the test suite on the latest release of 14, 15, 16, 17
and 18 with pg8000 and asyncpg. A server newer than 18 was not tested; the
test suite simulates one by changing the version SQLAlchemy read from the
server.

## SQLite

The same switch checks SQLite (the `sqlite3` module and aiosqlite). SQLite has
no PostgreSQL-style cost model to switch off, so queryguard runs
`EXPLAIN QUERY PLAN` with the statement's real parameters for each distinct
`SELECT`, `UPDATE` and `DELETE` in a test body and reports a table that is
scanned although the statement filters or joins on one of its columns, or for
which SQLite had to build an automatic index:

```
  orders is read in full: SCAN with filter orders.customer_id = ?
  first at app/orders.py:11 in orders_of
  kind: missing-index  label: orders
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#missing-index
  fix: CREATE INDEX ix_orders_customer_id ON orders (customer_id)
```

`EXPLAIN QUERY PLAN` does not run the statement, so the data and the open
transaction are untouched. The check is less precise than PostgreSQL's: it reads
the plan SQLite chose instead of asking whether any index could serve, it
judges only scans (not an unselective lookup by key), and it leaves `LIKE`,
views and plans it does not recognize alone. See
[the SQLite page](index-check-sqlite.md) for the rules, the
false-positive traps and the differences.

MySQL and MariaDB are checked as well, from their `EXPLAIN` output; see
[the MySQL and MariaDB page](index-check-mysql.md).

Statements that cannot be checked do not fail the test. They are summed up
once at the end of the session, for example with SQL Server, which the check
does not support:

```
============================ queryguard index check ============================
mssql statements were not checked: the index check supports PostgreSQL 16 or newer, SQLite, MySQL (tested on 8.4) and MariaDB (tested on 11.8)
```

The same summary names a PostgreSQL server older than 16, async drivers
other than asyncpg, and the number of statements PostgreSQL could not
plan, with the first error and its SQL.

