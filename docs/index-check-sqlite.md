# The missing-index check on SQLite

The index check (`--queryguard-index-check` or `queryguard_index_check = true`)
works on SQLite as well as on PostgreSQL 16 and newer. One switch covers both;
the dialect of the connection decides how a statement is checked. The
PostgreSQL check is described in `docs/design-0.2.md` and `docs/design-0.3.md`.

## What it does

For each distinct statement shape in a test body (once per test, the same rule
as on PostgreSQL), queryguard runs `EXPLAIN QUERY PLAN` with the statement's
real bound parameters on a fresh cursor of the test's own connection, and
reads the plan rows (`id`, `parent`, `notused`, `detail`).

`EXPLAIN QUERY PLAN` prepares the statement but never runs it. It writes
nothing, fires no trigger and starts no transaction. This was checked with
`sqlite3` in all three legacy `isolation_level` modes (`""`, `"DEFERRED"`,
`None`), with the `autocommit` attribute of Python 3.12 and later, and with
aiosqlite: `Connection.in_transaction` and the data are the same with the
check on and off (`tests/test_sqlite_index_check.py`).

Statements checked: `SELECT`, `WITH` (also `WITH ... UPDATE` and `DELETE`),
`UPDATE` and `DELETE`. `INSERT` and `executemany` calls are not explained.

## What is reported

A finding says that a table is read in full although the statement narrows
it, and what index would serve:

```
queryguard: missing index

  orders is read in full: SCAN with filter orders.customer_id = ?
  first at app/orders.py:11 in orders_of
  kind: missing-index  label: orders
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#missing-index
  sql: SELECT orders.id, orders.customer_id FROM orders WHERE orders.customer_id = ?
  fix: CREATE INDEX ix_orders_customer_id ON orders (customer_id)
  alembic: op.create_index("ix_orders_customer_id", "orders", ["customer_id"])
```

When SQLite walks an existing index that the filter cannot use (for example it
walks `ix_users_created_at` to satisfy an `ORDER BY` while the statement filters
on `status`), the first line says so instead:

```
  users scans the whole index ix_users_created_at: the filter users.status = ? does not match the start of any index
```

The fix is still an index for the filtered column, which lets SQLite search
instead of walking the index; whether it then also keeps the `ORDER BY` order is
up to the planner.

Findings are `missing-index` findings: they follow `queryguard_mode`,
`queryguard_fail_on`, `queryguard_ignore`, the baseline, SARIF,
`@pytest.mark.allow_missing_index` and `queryguard_index_ignore_tables` exactly
like the PostgreSQL ones. The table name is the declared name from
`sqlite_master`, so the identity of a finding does not depend on the case used
in the SQL.

| Plan row | Reported when |
|---|---|
| `SCAN t` | the statement compares a column of `t` in WHERE or ON (equality, `IN`, `IS`, a range, `BETWEEN`, or a function of the column, which gets "an expression index may be needed"), and no index of `t` already leads with that column |
| `SCAN t USING COVERING INDEX ix` (or `USING INDEX`) | an equality filter on a column that does not lead any index: the whole index is walked. A range filter or no filter is not reported (pagination walks an index on purpose). The finding says `t scans the whole index ix: the filter ... does not match the start of any index`, not that the table is read in full, and suggests an index for the filtered column |
| `SEARCH t USING AUTOMATIC [COVERING] INDEX (c=?)` | always: SQLite built a temporary index at query time, so `c` is a column an index is missing on (`kind` is "join condition" or "filter") |
| `SEARCH t USING INDEX ...`, `USING INTEGER PRIMARY KEY` | never |

The fix hint is `CREATE INDEX ix_<table>_<columns> ON <table> (<columns>)`
with the equality columns first and at most one range column. It is only given
when the columns are certain, together with the Alembic `op.create_index`
call (the index advisor's output; `queryguard_index_advisor = false` keeps only
the `CREATE INDEX`, which SQLite needs a name for, unlike PostgreSQL). Otherwise
the text is "add an index that matches this condition" (with "an expression
index may be needed" for a function of the column). That happens for `OR` over several unindexed columns, for
compound selects that read one table twice, and when the plan gives no
readable column.

## What is left alone (false-positive traps)

These are covered by tests in `tests/test_sqlite_index.py`.

- A scan with nothing to narrow it: `SELECT * FROM t`, `count(*)`, and
  `ORDER BY` without a filter (SQLite sorts with a temp b-tree; no index on a
  filter is missing). `ORDER BY` is never judged: a plan that scans and then
  sorts is reported only when a filter would also have been served.
- Primary key lookups (`INTEGER PRIMARY KEY`, `WITHOUT ROWID` keys), unique
  constraints and any index that leads with the filtered column.
- A scan although a filtered column already leads an index. That is the
  planner's choice (collation or affinity mismatch, or `ANALYZE` statistics
  that say the table is tiny), not a missing index, and `CREATE INDEX` would
  duplicate the existing one. Columns that lead only a partial index do not
  count as indexed.
- `LIKE` and `GLOB`: a pattern with a leading wildcard cannot use a btree
  index, so the advice would be wrong, and a prefix pattern uses an index only
  with matching collation or `case_sensitive_like`. They are not counted as
  filters. `NOT IN`, `<>`, `!=`, `IS NOT NULL` and arithmetic on the column are
  not judged either.
- The table a join starts from: its join column is no filter, because
  someone has to be scanned. A filter on it still counts. A correlated
  subquery's outer table is not blamed for the inner `WHERE`.
- Two columns of the same table compared to each other.
- Views, CTEs, subquery aliases, virtual tables (FTS and others), tables in
  attached databases, `sqlite_*` internal tables, `SCAN CONSTANT ROW`,
  table-valued functions. The tables behind a view are not judged, because
  the SQL names the view, not the table.
- Any plan shape this code does not recognize, and rows from older or newer
  SQLite releases it cannot parse: no finding and no failure. The check was
  developed against SQLite 3.46; the older `SCAN TABLE t` and `... AS alias`
  forms are parsed, but only from fixture rows.
- A table that the plan uses more than once under one name (a union of two
  selects on the same table) is reported once with the generic hint, and not
  at all when one use is an index search.

## Differences from the PostgreSQL check

- No cost model to switch off. PostgreSQL's check disables sequential scans and
  sees whether any index could serve; SQLite's planner has no such switch, so
  the check reads the plan SQLite chose. A scan is therefore evidence that
  SQLite found no usable index, but without `ANALYZE` it never prefers a scan
  to an index that exists.
- No generic plan. SQLite plans with the real bound values. A partial index
  `WHERE status = 'open'` is used for `status = ?` bound to `'open'` and not
  for another value, so the plan can depend on the parameter. Like the
  PostgreSQL check, only the first execution of a statement shape in a test is
  explained, so a later value that would scan is not seen.
- Small tables. Without `ANALYZE`, SQLite assumes tables are large and uses an
  index whenever there is one, so tiny test tables do not hide a missing index.
  After `ANALYZE` on a tiny, low-cardinality table SQLite may scan even
  though an index exists (verified); such a scan is not reported because the
  column leads an index. A table with no usable index is still scanned, and
  still reported.
- Precision. The plan says which table is read how, not which predicate it
  applies. Which WHERE and ON conditions belong to a scanned table is read
  from the statement text with a small tokenizer, so statements it cannot
  follow (unusual syntax, references it cannot place) give no finding rather
  than a guess.
- Only scan rows are judged. A table that SQLite searches by primary key but
  filters on an unindexed column (an unselective lookup after a scan of the
  other side of the join) is not reported.
- No hypopg-style verification: SQLite has no hypothetical indexes, so the
  suggested index is derived from the statement and the existing indexes, and
  is not tried before it is suggested.
- Notices. Other dialects still get the one-line notice at the end of the
  session; SQLite does not.

## Cost

One `EXPLAIN QUERY PLAN` per distinct statement shape per test, plus a few
catalog queries only when a scan needs judging. `scripts/bench_overhead.py
--index-check --statements 3000 --runs 7` on in-memory SQLite (Python 3.13,
SQLAlchemy 2.1, one machine, median microseconds per statement):

| loop | measurement off | listeners on | plus index check | index check alone |
|---|---|---|---|---|
| Core `select` by key | 64.2 | 81.9 | 98.7 | +16.8 |
| ORM `select` by key | 127.1 | 190.2 | 210.1 | +19.8 |
| Core, every statement a new shape | 285.9 | 334.5 | 372.7 | +38.2 |

A statement shape that was already explained in the test costs a text
normalization and a set lookup (the first two rows); a new shape costs one more
`EXPLAIN QUERY PLAN` on the same connection (the third row). These are timings
of one machine and an in-memory database, not a promise.

## Limits to keep in mind

- The check sees the statements of the test body, as on PostgreSQL; fixtures
  and set-up are not checked.
- Drivers: `sqlite3` through SQLAlchemy's `pysqlite` dialect and aiosqlite were
  tested. Another driver that reports the `sqlite` dialect name should work if
  its cursor accepts `EXPLAIN QUERY PLAN` with the statement's parameters.
- A custom `row_factory` (`sqlite3.Row`, dictionaries) is handled.
