# The missing-index check on MySQL and MariaDB

The index check (`--queryguard-index-check` or `queryguard_index_check = true`)
also works on MySQL and MariaDB. One switch covers every database; the dialect
of the connection decides how a statement is checked. PostgreSQL and SQLite
behave as described on their own pages.

**Status: experimental (see [Stability](stability.md#experimental)). This
check is tested against MySQL 8.4 and MariaDB 11.8 on a developer machine, not
in CI.** The project's CI runs no MySQL or MariaDB
server: both servers are GPL-licensed software, and adding one to the CI
workflow needs a written approval that has not been given. The unit tests on
recorded `EXPLAIN` output run everywhere; the tests that need a live server
skip themselves when none is reachable (see "Running the live tests").

## What it does

For each distinct `SELECT`, `UPDATE` and `DELETE` (also `WITH ... SELECT`) in a
test body, once per statement shape per test, queryguard runs a plain `EXPLAIN`
with the statement's real bound parameters on a separate cursor of the test's
own connection, and reads the plan. `INSERT` and `executemany` calls are not
explained. `ANALYZE` is never used: it executes the statement.

`EXPLAIN` plans `SELECT`, `UPDATE` and `DELETE` without running them. This was
checked on MySQL 8.4 and MariaDB 11.8 (`tests/test_mysql_index_check.py`) with
the data, the transaction state (`SERVER_STATUS_IN_TRANS`,
`information_schema.innodb_trx`), a trigger on the table, row locks held by
another connection, and `LAST_INSERT_ID()`: none changes with the check on, in
autocommit mode and inside an open transaction, and the check never starts a
transaction. The tabular `EXPLAIN` is used because its columns are the same on
MySQL and MariaDB; `EXPLAIN FORMAT=JSON` was captured next to it as a fixture
and carries no signal the check needs (MariaDB's JSON differs in shape).

Two limits of "does not disturb":

- The check runs its own catalog queries after `EXPLAIN`. `SHOW WARNINGS` and
  `ROW_COUNT()` issued by your code after a statement describe the last
  statement of the connection, which is then one of these. SQLAlchemy and the
  drivers read the row count and warnings of the statement itself, so they are
  not affected. `FOUND_ROWS()` is: statements containing `SQL_CALC_FOUND_ROWS`
  are therefore never explained.
- A statement whose cursor is unbuffered (`SSCursor`, `yield_per` on a
  server-side cursor) still has rows to read, and another statement on the
  connection would discard them. Such statements are skipped.

## What is reported

```
queryguard: missing index

  orders is read in full: full table scan (type ALL) with filter orders.customer_id = ?
  first at app/orders.py:11 in orders_of
  kind: missing-index  label: orders
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#missing-index
  sql: SELECT orders.id, orders.customer_id FROM orders WHERE orders.customer_id = ?
  fix: CREATE INDEX ix_orders_customer_id ON orders (customer_id)
  alembic: op.create_index("ix_orders_customer_id", "orders", ["customer_id"])
```

Findings are `missing-index` findings: they follow `queryguard_mode`,
`queryguard_fail_on`, `queryguard_ignore`, the baseline, SARIF,
`@pytest.mark.allow_missing_index` and `queryguard_index_ignore_tables` like the
other databases'.

| Plan row (`type`) | Reported when |
|---|---|
| `ALL` (full table scan) | the statement compares a column of the table in WHERE or ON (equality, `IN`, `IS`, a range, `BETWEEN`, or a function of the column, which gets "an expression index may be needed"), and no usable index of the table leads with that column. A join without an index shows `Using join buffer` (hash join or block nested loop); the note is added to the message |
| `index` (full index scan) | an equality filter on a column that does not lead any index: the whole index is walked. A range filter or no filter is not reported (`ORDER BY ... LIMIT` walks an index on purpose) |
| `ref`, `eq_ref`, `const`, `system`, `range`, `index_merge`, `fulltext`, subquery lookups | never |

The fix is `CREATE INDEX ix_<table>_<columns> ON <table> (<columns>)` with the
equality columns first and at most one range column, plus the Alembic call
(`queryguard_index_advisor = false` keeps only the `CREATE INDEX`). It is only
given when the columns are certain. Otherwise the text is "add an index that
matches this condition" (an `OR` over several unindexed columns, two uses of
one table, a function of the column). **The suggestion is not verified**:
neither MySQL nor MariaDB has hypothetical indexes like PostgreSQL's hypopg, so
the index is derived from the statement and the existing indexes and is never
tried before it is suggested. A note to that effect is printed once at the end
of a session that had a suggestion.

## What is left alone (false-positive traps)

These are covered by tests in `tests/test_mysql_index.py`, on plans recorded
from both servers.

- A scan with nothing to narrow it: `SELECT * FROM t`, `COUNT(*)`, and
  `ORDER BY` without a filter. `Using filesort` is not a scan and is never
  judged: a sort is reported only when a filter would also have been served.
- Primary key and unique lookups, and any index that leads with the filtered
  column (`ref`, `const`, `eq_ref`, `range`).
- A scan although a filtered column already leads an index. That is the
  optimizer's choice (a low-selectivity condition such as `status IN (...)`
  listing every value, a small table, stale statistics), not a missing index,
  and `CREATE INDEX` would duplicate the existing one. An invisible (MySQL) or
  ignored (MariaDB) index, a `FULLTEXT` or `SPATIAL` index and a functional
  index do not count as leading; a prefix index does.
- A comparison the optimizer cannot use an index for although one exists
  (a string column compared with a number): the column leads an index, so
  nothing is reported; the type mismatch is the problem and is not detected.
- `LIKE` (a leading wildcard cannot use a btree index, and a prefix pattern is
  not judged), `<>`, `!=`, `IS NOT NULL` and arithmetic on the column.
- The table a join starts from: its join column is no filter. Two columns of
  the same table compared to each other.
- `MATCH ... AGAINST` (plan type `fulltext`).
- Views: the plan names the table behind the view, the SQL names the view, so
  the table is not judged. The same for tables the SQL names in another
  database (`other_db.t`) and temporary tables: the catalog read is the current
  database's.
- Subquery and derived-table placeholders (`<derived2>`, `<subquery2>`),
  `UNION RESULT`, `Impossible WHERE` and any plan row that lacks a column the
  check reads, or has an access type it does not know: no finding and no
  failure.
- Index hints (`USE`, `FORCE`, `IGNORE INDEX`) are read past; `STRAIGHT_JOIN`
  is read as `JOIN`.

## Differences from the PostgreSQL and SQLite checks

- Precision depends on the data. The MySQL optimizer prefers a scan on a small
  table, and its statistics decide the rest. The check does not rely on the
  optimizer's choice when an index is absent: a column that leads no index and
  is filtered is reported whether the table has three rows or three million,
  so a small lookup table is reported too. List it in
  `queryguard_index_ignore_tables`. In the other direction, an index that
  exists but is not chosen is never reported (see above).
- MySQL 8 may answer an `EXPLAIN` with an index skip scan (`range`, "Using
  index for skip scan") where MariaDB reads the whole index; the same covering
  query is then not reported on MySQL (`covering_scan_filter` fixture).
- `UPDATE` and `DELETE` without an index on the filter column show
  `type: index` on the primary key (a scan of the clustered index), and are
  reported as a full index scan.
- Which `WHERE` and `ON` comparisons belong to a table is read from the
  statement text with a small tokenizer (the one of the SQLite check, with
  MySQL's comments, quoting and `%s` placeholders), so statements it cannot
  follow give no finding rather than a guess.
- Only the first execution of a statement shape in a test is explained, with
  its bound values (MySQL plans with the real values).

## Configuration

Nothing new: `queryguard_index_check`, `queryguard_index_advisor`,
`queryguard_index_ignore_tables` and the markers work as described in the
[configuration reference](configuration.md). The check is off by default.

Drivers tested: PyMySQL (sync) and aiomysql (async, through SQLAlchemy's
`mysql+pymysql` and `mysql+aiomysql`). Other drivers that report the `mysql` or
`mariadb` dialect should work if the cursor accepts `EXPLAIN` with the
statement's parameters. mysqlclient and mysql-connector-python are not used by
this project's tests (they are GPL-licensed).

## Cost

One `EXPLAIN` per distinct statement shape per test, plus up to three catalog
queries (`information_schema.tables`, `information_schema.columns` and
`SHOW INDEX`) per table, only when a scan needs judging. Measured with
`scripts/bench_mysql_index.py --statements 3000 --runs 9` against MySQL 8.4 and
MariaDB 11.8 in local containers over loopback (SQLAlchemy 2.1, PyMySQL,
median microseconds per statement, one machine, noisy):

| loop | server | listeners on | plus index check | index check alone |
|---|---|---|---|---|
| a statement shape repeated | MySQL 8.4 | 159 | 218 | +59 |
| | MariaDB 11.8 | 164 | 174 | +11 |
| a new shape every time | MySQL 8.4 | 351 | 632 | +281 |
| | MariaDB 11.8 | 360 | 573 | +212 |

A new shape costs one more round trip to the server (about 0.2 ms here);
a repeated shape costs a text normalization and a set lookup, so the small and
noisy figures of the first rows are mostly the one `EXPLAIN` of the run spread
over the statements. Over a real network a round trip costs more.

## Running the live tests

`tests/test_mysql_index_check.py` skips without a server. To run it, start a
server yourself (for example `docker run -d -e MYSQL_ROOT_PASSWORD=secret -p
127.0.0.1:33306:3306 mysql:8 --mysql-native-password=ON
--authentication-policy=mysql_native_password`; PyMySQL needs the `cryptography`
package for the default `caching_sha2_password` plugin over a plain
connection, which this project does not install) and set

```bash
QUERYGUARD_MYSQL_URL=mysql+pymysql://root:secret@127.0.0.1:33306/mysql \
QUERYGUARD_MARIADB_URL=mysql+pymysql://root:secret@127.0.0.1:33307/mysql \
    uv run --group dev pytest tests/test_mysql_index_check.py
```

The user must be able to create and drop databases; the tests work in a scratch
database named `qg_<random>` and drop it (a small `qg_other` database stays).
`scripts/capture_mysql_plans.py` rewrites the plan fixtures from a live server.
