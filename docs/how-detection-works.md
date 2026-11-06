# How detection works

pytest-queryguard does not read your code. It listens to the SQL that
SQLAlchemy sends while a test function runs, and applies a few rules to what
it saw.

## What is measured

1. When pytest starts (unless `queryguard_mode = off`; budgets still install
   the listeners when a test needs them), the plugin adds class-level
   SQLAlchemy listeners: `before_cursor_execute` and `after_cursor_execute`
   on `Engine` see every SQL statement and time it, and tell a relationship
   lazy load or a column load from the ORM's options on its `SELECT`;
   `after_begin` and `after_flush` on `Session` note which `Session` a
   connection serves and what a flush wrote; the `load` and `refresh`
   instance events note which query loaded each object (see below).
2. Measurement covers only the test function body, so queries in fixture
   setup and teardown are not counted. unittest `setUp` and `tearDown` run
   inside the test call and are counted.
3. Each statement is stored with its call site: the first stack frame outside
   SQLAlchemy, pytest, the standard library, `site-packages` and the plugin
   itself. For async sessions the search continues in the parent greenlets,
   where the awaiting code runs.
4. After the test, the stored events are checked against the budget and the
   rules below. A test stores at most `queryguard_max_events` statements;
   past that they are only counted, and the report says so.

The findings are then filtered by the `queryguard_ignore` option, the
`queryguard_ignore` marker and the [baseline](baseline.md), and reported as a
failure, a `QueryGuardWarning` or a line in the [SARIF file](sarif.md),
depending on `queryguard_mode` and `queryguard_fail_on`.

## The rules

Every rule produces a finding with a kind, a label, a call site and a hint.

### relationship-load

One relationship (for example `Order.customer`) is lazy-loaded for at least
`queryguard_relationship_threshold` (default 2) different parent objects
that one query line loaded (see "Where the parents were loaded" below).
The label is the relationship. The hint is `joinedload` for a many-to-one or
one-to-one relationship and `selectinload` for a collection. The rule counts
distinct parent objects, not statements, so it is independent of how
SQLAlchemy batches the loads.

The parents are grouped by the line of the query that loaded them, as strict
mode groups them: a loop over the rows of one query is an N+1, two
single-object lookups that each read the relationship
(`session.get(Order, 1).customer`, then `session.get(Order, 2).customer`) are
not. Lookups made one by one at the same line (`for id in ids:
get_order(id).customer`) count together. The finding counts the lazy loads of
the groups that reach the threshold; when several do, it names the first
query line and how many others. When the query of a parent is unknown
(objects the test created that a query or `session.get()` handed back without
reading their row, objects loaded before the test body, objects that two
queries read), the plugin cannot tell which query it belongs to: it counts for
every query line's group, and the parents of unknown origin also count as a
group of their own, so an unknown origin never hides a loop. A query line
whose lazy loads all ran inside an `ignore("relationship-load")` block is
left out of the finding, the other lines are still reported.

The grouping has a cost: a loop over the rows of several queries that return
one row each (`for order in [*open_orders, *closed_orders]` with one order of
each) has a single parent per query line and is not reported; give the test
two rows for one of the queries, as for any N+1 test.

#### Refreshing an object with an eager relationship

`session.refresh(obj)`, `session.refresh(obj, ["children"])` and the reload of
an expired object do not run a mapped `lazy="selectin"` or `lazy="subquery"`
loader the way a query does. SQLAlchemy loads such a collection for that one
object through its immediate loader, with the parent attached, which is also
how a lazy load looks. The plugin tells the two apart: while the loader runs
for a refresh (the outer load has a refresh state), the statement keeps the
relationship label but is not counted as a lazy load, because
`selectinload` is already the mapped strategy. Only the loader's own query is
exempt: its parent must be the refreshed object and its relationship the one
the loader loads. An `immediateload()` option on a plain query, every
`lazy="select"` access, and a lazy load that user code runs while the loader
builds the children (a `@reconstructor` or a `load` event reading a
`lazy="select"` relationship) are still lazy loads.

A loop of refreshes is its own finding, a column-load (see below), not a
relationship-load: `for obj in objs: session.refresh(obj)` is reported once the
column-load threshold (default 5) is reached, and the collection queries it
runs are never a repeated-statement finding. A refresh that reads back the
object the flush before it wrote is not counted at all (see "Refresh after a
write" under column-load).

The plugin tells the immediate loader's statement by the outer query's
context, which SQLAlchemy hands to it (the private execution option
`sa_top_level_orm_context`, the same from 2.0.13 to 2.1): for a refresh it
names the object refreshed. If a SQLAlchemy release stops passing it, these
loads count as lazy loads again, as before this rule: a possible false
positive, never a missed lazy load and never an error.

#### Where the parents were loaded

The report names the query that loaded the parent objects
(`parents loaded at`) and chains the loader hint from it. The call site of
each `SELECT` is kept with its result, and while the ORM turns its rows into
objects the `load` instance event (and `refresh`, for an object the `Session`
already had whose row the query reads again) stores that call site and the
object's load path (`Order -> Order.customer -> Customer`) on the object's
state. The call site is looked up once per query, not per row. Objects of a
`selectinload` or `subqueryload` query and of a lazy load inherit the site of
the query they were loaded for. A lazy load later reads them from its parent,
and the finding uses them only when all its loads agree: a single site (or,
when the parents of several query lines each reach the threshold, the first
one and how many others), and a single load path that can be written as
`select(Root).options(loader(A.b).loader(B.c))` (no aliased class or
`of_type()`). Otherwise the line is left out and the hint names the
relationship's own class, as without the information. An object whose row
two queries from different places read gets no origin; one that a query
returns from the identity map without reading its row fires no event, so it
keeps the query that loaded it. Origins are kept per measurement: an object
loaded before the test body (or before a `record_queries()` block) has none
there. The in-app mode does not record origins and prints the earlier hint.

#### How a lazy load's statement is attributed

The ORM keeps what a `SELECT` is for in the options of that execution: the
parent object of a lazy load, the object of a column load and the
relationship path of a loader. The plugin reads them in
`before_cursor_execute`, from the statement itself, so nothing else that runs
in between can take or clear them: the autoflush before a lazy load, the
`Session`'s begin step (the engine's `begin` event and `after_begin`
listeners, such as SQLAlchemy's pysqlite `BEGIN` recipe), statements of
another `Session` on the same connection (a second `Session` joined to the
external connection of a "test in a transaction" fixture), and other asyncio
tasks or threads. A lazy load that never reaches the database (a
`do_orm_execute` listener of yours answers it from a cache) is not counted.
These options are private to SQLAlchemy; the plugin reads them in one place
and is tested from SQLAlchemy 2.0.13 to 2.1.

### repeated-statement

One `SELECT` runs at least `queryguard_repeat_threshold` (default 5) times in
one test. Statements are compared after normalization: whitespace is
collapsed, literals and parameter placeholders become `?`, and `IN (...)`
lists of any length are one shape. Statements already reported as a
relationship or column load are not counted again, and `INSERT`, `UPDATE`
and `DELETE` are never counted by this rule, nor are transaction-control
statements: `BEGIN`, `COMMIT`, `ROLLBACK` (also `ROLLBACK TO SAVEPOINT`),
`SAVEPOINT`, `RELEASE`, `START TRANSACTION` and `SET TRANSACTION`. A loop that
commits on each pass runs one `BEGIN` and one `COMMIT` per pass, for example
with SQLAlchemy's pysqlite `BEGIN` recipe, and that is not a query to batch.
Budgets (`max_queries`) still count these statements. Other `SET` statements
(`SET search_path`, `SET LOCAL ...`) are counted like any statement, and so is
a `BEGIN` that opens a block rather than a transaction (`BEGIN
load_order(:id); END;`). A loop that opens a `SAVEPOINT` for each row still
shows its `INSERT`s to the [write-loop](#write-loop) rule. Pagination and
chunked `IN` loads are not counted either, when their parameters show it (see
below). The label is the normalized statement. This rule sees Core and
`text()` statements as well as ORM ones.

Schema reflection is not counted either, by this rule or by the small-repeat
rule: SQLAlchemy's `MetaData.reflect()`, `Inspector.has_table()` and
`metadata.create_all()` run one catalog query per table, and Alembic's
autogenerate and version-table checks do the same, so a loop over them is no
N+1 the application could batch. A statement is skipped when it starts with
`PRAGMA`, reads `sqlite_master`, `sqlite_schema`, `sqlite_temp_master` or
`sqlite_temp_schema` (also as `main.` or `temp.`), or reads anything in the
`pg_catalog` or `information_schema` schema. It is matched on the normalized
text, so a string value such as `'sqlite_master'` does not count, nor does a
name in an SQL comment, or a CTE or table alias the statement itself names
`sqlite_master`, `pg_catalog` or `information_schema`. A statement that joins
an application table with a real catalog is skipped too.
Not skipped: MySQL's `SHOW` and `DESCRIBE`, and PostgreSQL catalog tables read
without the schema name (`pg_class` found through the search path). These
statements still count for `max_queries` and appear in the report.

With `queryguard_small_repeat_threshold` set (off by default, for example
`2`), a statement that ran at least that many times but fewer than the repeat
threshold is reported too, if all of these hold: every run came from the same
source line, every run had different parameters, the statement is not a write
or transaction control, and it is not pagination (`OFFSET`, or `LIMIT` with a `>` or `<` condition on
a parameter) or a chunked `IN` load as described below.

#### Pagination and chunks

Walking through a result page by page, and loading a long list of keys in
slices, run one statement shape many times on purpose. Both rules leave such
runs alone when the statements and their bound parameters show it. Each
parameter is classified by the SQL just before it: after `OFFSET` it is the
offset, after `LIMIT` (or `FETCH FIRST`, `TOP`) the page size, after `<`,
`<=`, `>` or `>=` a range bound, inside an `IN (...)` list a list member, and
anything else a plain value. Literal `LIMIT 4 OFFSET 8` values count as page
size and offset. The values come from the parameters the driver got, by
position (`?`, `%s`, `$1`) or by name (`%(name)s`, `:name`).

The runs of one shape are **pagination** when all of these hold:

- the statement has `OFFSET`, or `LIMIT` with a range bound;
- the plain values, the `IN` lists and the page size are the same in every run:
  only the cursor (the offsets and range bounds) changes. A loop over items
  changes a plain value (`WHERE customer_id = ?`), so the first page of each
  customer's orders is still reported;
- the page size, when it is known, is more than one row. `LIMIT 1 OFFSET n`
  reads a table one row per statement and is still reported, which answers the
  concern in [#60](https://github.com/MaciejZiel/pytest-queryguard/issues/60);
- the cursor moves the same way (up or down) from run to run and never repeats,
  and an offset moves by at least the page size, so pages do not overlap;
- a keyset bound is an integer and moves by at least the page size: the next
  cursor is the last key of a full page (only the step after the last, partly
  filled page may be shorter, and it ends the walk). A loop that passes each row's own key
  (`WHERE id > :row_id LIMIT 3` for every row) moves it by one row and is
  reported. String and date bounds cannot be measured, so their walks are
  reported as before;
- the walk may start again from its first cursor once (a test that reads the
  same list twice). The same walk run three or more times is a loop-invariant
  query inside a loop and is reported, and so is a restart anywhere else.

The runs are a **chunked `IN` load** when all of these hold:

- one `IN` list changes from run to run and everything else is the same;
- the lists share no value: together they partition one list of keys;
- every list has the same length, at least 50 values, except one shorter list
  (the remainder, in any position).

The minimum of 50 is there because per-item lists are short: the two
addresses of an order, a customer's ten orders. A helper called with each
customer's own orders sends equal-sized lists that share no value, which only
their length tells apart from chunks. Batch sizes of loaders and ORMs are
larger (`selectinload` uses 500). A list of one id per run is reported.

What these rules cannot see, and so still report: a polling loop and a
loop-invariant query, which both repeat one statement with the same
parameters (the first is fine, the second is a bug, and the statements are the
same); composite keyset conditions such as `a < ? OR (a = ? AND b < ?)`, where
the bound is also a plain value; keyset walks on a string or date column;
MySQL's `LIMIT offset, count`; `IN` lists
written as literals; and two chunked loads of the same keys in one test (the
lists overlap). A parameter that cannot be read (an unknown placeholder
style, values that do not compare) means the statements are reported as
before.

### column-load

At least `queryguard_column_load_threshold` (default 5) instances of one class
each go back to the database for columns they already have a row for. There
are two causes, and the report tells them apart: a deferred column
(`deferred()`, `defer()`, `load_only()`), where the hint is
`undefer(Model.column)`; and attributes expired by `commit()`, `expire()` or
`expire_all()`, where the hint is `expire_on_commit=False` (a `refresh()` in a
loop gets its own hint). The label is the class and its deferred columns
(`Book.summary`) or the class and the cause (`Book (expired)`,
`Book (refresh)`). `session.refresh(obj, ["name"])` is a refresh too, although
it expires the names before loading them.

#### Refresh after a write

A repository layer often refreshes every object it creates or updates, to read
server defaults and `onupdate` values (advanced-alchemy's `auto_refresh`, on
by default, does it after each `add()` and `update()`). Each of those
refreshes is asked for once per write, so a test that creates ten rows through
such a repository is not a loop of reads. A `session.refresh()` of an object
that the latest flush of its Session inserted, or changed (a column or a
collection, after a net change), is therefore not counted, for one object per
class and flush; its statement still carries the column-load label, so it is
not a repeated statement either. A loop that writes one object, flushes and
refreshes it in each iteration is therefore not a column-load finding; its
INSERT or UPDATE statements are what the opt-in [write-loop](#write-loop)
rule reports. Still counted:

- a refresh of an object that was only read (`for obj in query: session.refresh(obj)`);
- refreshing every row of one bulk insert (`add_all()`, one flush, a refresh
  per row): the first refresh is let through, the others count;
- a second refresh of the same write, and a refresh of an object an older
  flush wrote;
- a refresh of an object that was in `session.dirty` but had no net change
  (an attribute set to its own value), since the flush wrote nothing for it;
- attributes reloaded after `commit()` or `expire()` without a refresh (the
  `expired` cause).

### write-loop

Off by default. With `queryguard_write_loop_threshold` set (for example `5`),
the same `INSERT`, `UPDATE` or `DELETE` shape, one row per statement, run that
many times from one source line is reported, with a hint to write the rows
together (`session.add_all()` and one flush, or
`session.execute(insert(Model), rows)`). `executemany` and multi-row `VALUES`
statements are already batched and never counted. The statements of one ORM
flush count as one: on a dialect without `insertmanyvalues`, `session.add_all()`
and one `flush()` still send one `INSERT` per row, and that is not a loop in
your code. A `flush()` or `commit()` per row, which is, is reported. The flush
is recognized from the call stack (SQLAlchemy's `Session._flush`); if a future
SQLAlchemy release changes that, such a flush is reported again until the
detection is updated.

### What the count rules count together

The relationship, repeated-statement, column-load and write-loop rules count
over a scope. By default (`queryguard_scope = test`) the scope is the
whole test. With `session`, each SQLAlchemy `Session`, or each connection
checkout for statements no `Session` ran, is counted on its own, and a finding
needs one scope to reach the threshold. See [Tests with several sessions or
requests](several-sessions.md).

### unused-eager-load (experimental)

Off by default. See [Unused eager loads](unused-eager-load.md).

### unused-columns (experimental)

Off by default. See [Unused columns](unused-columns.md).

### cartesian-load

One query, so no count rule and no budget sees it:

```python
select(Customer).options(joinedload(Customer.orders), joinedload(Customer.notes))
```

The SELECT joins both collections to the customer, so each customer comes
back once per pair of an order and a note: 20 orders and 20 notes are 400
rows for one customer, and the ORM folds them back into 20 + 20 objects. The
rows grow as the product of the collections, which is slow and memory-heavy
long before a test notices.

The rule reads the loader options of each top-level ORM SELECT. It reports a
statement in which two collections are loaded with `joinedload` side by side:

- **side by side**: neither is on the path of the other.
  `joinedload(Customer.orders).joinedload(Order.items)` is a chain whose rows
  grow with the number of items, not a product, and is not reported;
  `joinedload(Order.customer).joinedload(Customer.notes)` with
  `joinedload(Order.items)` is (a scalar on the way does not multiply, the
  two collections still do).
- **in one statement**: a `selectinload` or `subqueryload` on the way starts
  a SELECT of its own, so `selectinload(Customer.orders).joinedload(Order.items)`
  and `...joinedload(Order.tags)` are a product inside that second SELECT and
  are reported, while `joinedload(Customer.orders)` with
  `selectinload(Customer.notes)` is not.
- a `defaultload()` or `lazyload()` step runs the relationship's own loader,
  usually a lazy load with a SELECT of its own, so
  `defaultload(Customer.orders).joinedload(Order.items)` with
  `...joinedload(Order.tags)` is a product inside each lazy load and is
  reported.
- `contains_eager()` is left out: a query that joins on purpose to filter
  chose its rows. So are wildcards (`joinedload("*")`) and a select of
  several entities whose collections belong to different entities.
- With a chain next to a collection,
  `joinedload(Customer.orders).joinedload(Order.items)` and
  `joinedload(Customer.notes)`, the product is orders x notes: the finding
  names the topmost collection of each branch.

The label names the collections, `Customer.notes + Customer.orders`; the fix
keeps one `joinedload` and turns the others into `selectinload`, one more
query each and no repeated rows:

```
  Customer.notes and Customer.orders were joined into one query side by side, once
  first at app/customers.py:12 in list_customers
  kind: cartesian-load  label: Customer.notes + Customer.orders
  each row repeats per pair of their items: the rows grow as their product
  fix: load all but one of them with selectinload (one more query each, no repeated rows), e.g.
       select(Customer).options(joinedload(Customer.notes), selectinload(Customer.orders))
```

It warns by default (`queryguard_cartesian_load = warn`); `fail` fails the
test in `fail` mode, `off` turns the check off. Loader strategies set on the
mapping (`relationship(lazy="joined")`) are not options of the query and are
not seen.

### missing-index

Off by default; PostgreSQL 16 and newer, SQLite, MySQL and MariaDB. See the
[PostgreSQL](index-check.md), [SQLite](index-check-sqlite.md) and
[MySQL and MariaDB](index-check-mysql.md) pages.

## Thresholds and why

| Option | Default | Why this value |
|---|---|---|
| `queryguard_relationship_threshold` | 2 | One lazy load of a relationship is normal. Two parents each loading it is the smallest case of a loop over rows, and the rule has to work on test databases with a few rows. Raise it if two is too strict for your code (see the [FAQ](faq.md)). |
| `queryguard_repeat_threshold` | 5 | A few runs of one statement are often a deliberate small batch, so the default is higher than for relationships. |
| `queryguard_small_repeat_threshold` | 0 (off) | Catches loops of two to four runs; off because it can report small legitimate repetition, so it is a choice. |
| `queryguard_column_load_threshold` | 5 | Touching a few objects after a `commit()` is ordinary test code; the limit is as high as the repeat rule's. |
| `queryguard_write_loop_threshold` | 0 (off) | Row-by-row writes are normal in many tests, so the kind is opt-in. |

The defaults are choices, not measurements. Every threshold is an ini option.

## What the rules cannot see

See [Limitations](limitations.md). In short: only SQLAlchemy statements are
seen, only inside the test body, and a rule says that a pattern happened, not
that production would be slow.
