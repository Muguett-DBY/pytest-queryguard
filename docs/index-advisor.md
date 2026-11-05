# Index advisor

When the [missing-index check](index-check.md)
finds a statement that no index can serve, the index advisor says which index
to create. It prints a `CREATE INDEX`, the matching Alembic `op.create_index`
call, and, when the hypopg extension is installed, asks PostgreSQL whether the
planner would use that index and what it would save.

The advisor runs only for statements that already have a finding, inside the
same savepoint as the check, and never changes the test's data or settings.
One exception is a function marked `IMMUTABLE` that has non-transactional side
effects: PostgreSQL evaluates it at plan time, so each plan the check and the
advisor ask for runs it again (see [Limitations](limitations.md)).

## Example

`app/orders.py` lists the open orders of a customer, newest first:

```python
def recent_open_orders(session: Session, customer_id: int) -> Sequence[Order]:
    stmt = (
        select(Order)
        .where(Order.customer_id == customer_id, Order.status == "open")
        .order_by(Order.created_at.desc())
        .limit(20)
    )
    return session.scalars(stmt).all()
```

The test database has 50 000 orders and has been analyzed. With
`pytest --queryguard-index-check` and hypopg installed (PostgreSQL 17, hypopg
1.4.3, Python 3.13, SQLAlchemy 2.1, pytest 9.1; the output is copied from that
run):

```
queryguard: missing index

  orders is read in full: Seq Scan with filter ((orders.customer_id = $1) AND ((orders.status)::text = ($2)::text))
  first at app/orders.py:16 in recent_open_orders
  kind: missing-index  label: orders
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#missing-index
  sql: SELECT orders.id, orders.customer_id, orders.status, orders.created_at FROM orders WHERE orders.customer_id = %s::INTEGER AND orders.status = %s::VARCHAR ORDER BY orders.created_at DESC LIMIT %s::INTE...
  fix: CREATE INDEX ON orders (customer_id, status, created_at DESC)
  alembic: op.create_index(
      "ix_orders_customer_id_status_created_at",
      "orders",
      ["customer_id", "status", sa.text("created_at DESC")],
  )
  hypopg: the planner uses it (Index Scan); estimated cost 1070.42 -> 19.34
```

After adding the index to the model (`Index("ix_orders_customer_id_status_created_at",
"customer_id", "status", text("created_at DESC"))`) the same test passes:

```
tests/test_orders.py .                                                   [100%]

============================== 1 passed in 0.42s ===============================
```

Without hypopg the suggestion is the same, without the last line, and the
session summary says how to get it:

```
============================ queryguard index check ============================
index advisor: suggested indexes were not verified: the hypopg extension is available but not installed in this database. Run CREATE EXTENSION hypopg (needs a superuser), or set queryguard_hypopg = on to let the check create it inside its own rolled-back savepoint; or set queryguard_hypopg = off to silence this notice
```

## How the index is chosen

The advisor reads the conditions of the plan node that reads the table
(`Filter` and `Index Cond`, parsed by `pytest_queryguard.predicates`) and the
keys of a `Sort` node directly above it (the `Sort Key` field of the JSON plan),
and builds the key columns in this order:

1. Columns compared with `=`, `= ANY (...)` (an `IN` list) or `IS NULL`, in the
   order the statement has them. A `varchar` column compared as `::text` counts
   as the plain column, as in the check itself. A join condition on another
   table, such as `orders.customer_id = customers.id`, counts as an equality.
2. The first range column (`<`, `<=`, `>`, `>=`).
3. The `ORDER BY` columns, with their `DESC` and `NULLS` clauses, when the
   conditions are equalities only or the statement has a `LIMIT`: an index in
   that order lets PostgreSQL skip the sort, and with a `LIMIT` stop early.
   Without a `LIMIT` the range column is kept instead, because narrowing the
   rows matters more than their order.

At most 4 key columns are suggested. Conditions the advisor cannot index with
a plain column (`lower(x) = ...`, a cast other than varchar to text, `LIKE`,
`<>`, `OR` between columns, array and JSON operators) are left out; when nothing
else is left, the finding keeps the generic hint of the check (an expression
index may be needed). The advisor never suggests an expression index, because
PostgreSQL only accepts one if every function in it is immutable, which the
plan does not say.

Statements that a partial index already on the table may serve are not
advised at all, and not reported (see [index-check.md](index-check.md)).

A **partial index** is considered when the statement has a constant predicate
on the table (`deleted_at IS NULL`, `status = 'open'` written as a literal; a
bound parameter is not constant). The index is then built without that column
and with the predicate as its `WHERE`. It replaces the full index only when
hypopg is installed and estimates it at most a quarter of the full index's size
(it uses the table's statistics, so it needs `ANALYZE`), and the planner still
uses it. Without hypopg there is no way to tell whether the predicate is
selective, so the suggestion is the full index with the column as a key.

`INCLUDE` columns are never suggested: whether a covering index pays depends on
the visibility map, which a test database does not show.

## When no index is suggested

If the table already has a btree (or, for one column, hash) index that starts
with the suggested columns, the plan did not use it for another reason, and a
new index would not help. The finding then says why instead of a `CREATE INDEX`:

```
  fix: no new index: index orders_customer on (customer_id) exists but is marked invalid (a CREATE INDEX CONCURRENTLY that failed, or CREATE INDEX ON ONLY a partitioned table with partitions not attached?): drop it and create it again
```

The reasons the advisor can read from the catalog:

- the index is invalid;
- the index is partial (`WHERE ...`), so it serves only statements that include
  its condition: add that condition to the query, or create a full index if the
  query must not have it;
- the index is valid and not partial, so the planner could use it: compare the
  type of the column and of the parameter (an implicit cast), the collation and
  the operator class. This is the one reason the advisor cannot name exactly.

An index that has the columns in another position, such as the trailing column
of a multicolumn index, does not count: for `WHERE b = $1` and an index on
`(a, b)` the advisor suggests an index on `(b)` (before PostgreSQL 18, which
can skip-scan the leading column).

## Verification with hypopg

[hypopg](https://github.com/HypoPG/hypopg) creates hypothetical indexes: only
catalog entries in the memory of the session, with no data. When it is installed
in the database, the advisor, inside the savepoint the check already opens:

1. plans the statement with sequential scans allowed (the cost before);
2. creates the suggested index with `hypopg_create_index()`;
3. plans the statement again with `enable_seqscan = off`, as the check does, and
   looks for the hypothetical index in the plan: that is "the planner uses it";
4. plans it once more with sequential scans allowed (the cost after);
5. calls `hypopg_reset()` and releases the savepoint.

Facts about hypopg that shape this:

- Hypothetical indexes are **not transactional**: a `ROLLBACK` does not remove
  them, so the advisor resets them itself, also after an error (it recovers
  with a nested savepoint of its own, then resets).
- Only a plain `EXPLAIN`, without `ANALYZE`, sees them, and only in the session
  that created them. `EXPLAIN (GENERIC_PLAN)` is such a plan. The check already
  uses `enable_seqscan = off` so that a missing index shows even on a table
  with a few rows; the verification asks "would the planner use this index
  when it can?" in the same way, and takes the costs with sequential scans
  allowed, so they are the planner's real estimates.
- The costs are the planner's estimates for the **whole statement**, from the
  table statistics in the test database. On a table of a few rows the index
  may not lower them; the message then says "no gain at the current table
  size", and the verdict "the planner uses it" is still the answer to whether
  the index can serve the statement.
- On a partitioned table the hypothetical index is created on the
  partitioned table itself, and the planner uses it on every partition it
  reads, as it would use a real index created there (checked with hypopg
  1.4.3 on PostgreSQL 16 and 18).
- hypopg's functions are called with their schema, so an application's own
  `search_path` does not matter.
- On asyncpg the EXPLAIN already runs in a DO block, because asyncpg prepares
  every statement it fetches rows for. hypopg needs no such trick:
  `hypopg_create_index($1)` takes the definition as a parameter (one cached
  statement), and the planner hook of hypopg is in the same backend. The
  verification works the same on sync drivers and on asyncpg (the tests run
  both).

If hypopg cannot create the index (an error from the extension), the finding
shows the suggestion and `hypopg: could not verify this index: ...`. If the
planner would not use it, the finding says so, so the suggestion can be
questioned.

### Installing hypopg

On Debian and Ubuntu, with the PostgreSQL apt repository: `apt install
postgresql-<major>-hypopg` (the package exists for PostgreSQL 14 to 18). The
official `postgres` Docker image is Debian-based, so a Dockerfile can add the
package to it. Then, once per database (it needs a superuser):

```sql
CREATE EXTENSION hypopg;
```

`queryguard_hypopg` chooses what the check does about it:

| Value | Meaning |
|---|---|
| `auto` (default) | Verify when the extension is installed in the database. Otherwise suggest without verifying and print one notice at the end of the session, which says whether the extension is available on the server or has to be installed. |
| `on` | As `auto`, and when the extension is available but not installed, run `CREATE EXTENSION hypopg` inside the check's savepoint. The savepoint is rolled back, so nothing is left in the database, but the role needs the privilege (usually a superuser). |
| `off` | Never touch hypopg; suggest only, with no notice. |

## Options

| Option | Default | Meaning |
|---|---|---|
| `queryguard_index_advisor` | `true` | Suggest an index for each missing-index finding. Used only when `queryguard_index_check` is on. |
| `queryguard_hypopg` | `auto` | See above. |

The advisor is on by default with the index check because it only adds lines
to a finding the check already makes, does extra work only for statements with
a finding, fails open (a failure of the advisor leaves the finding with the
generic hint and one notice), and prints the old `fix:` line unchanged for the
common case of one plain column. `queryguard_index_advisor = false` restores
the old output exactly. Verification needs an installed extension and is never
attempted without it.

`queryguard_ignore`, `queryguard_index_ignore_tables` and
`allow_missing_index` apply as before: an ignored finding is dropped with its
advice.

## In the Finding model

A `missing-index` finding has an `advice` field (`pytest_queryguard.findings.IndexAdvice`):
`create_index`, `alembic`, `columns`, `where`, `skipped`, `verified` (`None`
when hypopg did not check it, `True` when the planner uses the index, `False`
when it would not), `cost_before`, `cost_after`, `size_bytes` and
`verification_error`. It is not part of the finding's identity, so baselines
do not change.

## Limits

- PostgreSQL 16 or newer, like the check. The advisor's tests ran against
  PostgreSQL 16, 17 and 18 with hypopg 1.4.3 (and the Debian package
  `postgresql-<major>-hypopg` exists for 14 to 18, but the check skips 14 and
  15).
- Equality columns keep the order of the statement: the advisor does not know
  which column is the most selective.
- No expression indexes, no `INCLUDE`, no GIN, GiST or BRIN indexes.
- Findings are advised one by one: for a statement with several findings every
  suggestion is verified on its own, and the costs are for the whole
  statement with only that index added.
- Other tools use hypopg too, for example Dexter, which works from production
  query logs. This one works from the statements of a test suite, at the
  line of code that ran them; it does not replace looking at production
  workload.
