# Limitations


- SQLAlchemy only. The plugin listens to SQLAlchemy's events, so a statement
  that does not go through SQLAlchemy is not counted, not budgeted and not
  checked for indexes, and nothing says so. That covers a raw `sqlite3`,
  `psycopg` or `asyncpg` connection used directly, and the Django ORM, Peewee,
  Tortoise ORM and any other ORM. SQLModel and Flask-SQLAlchemy build on
  SQLAlchemy sessions, so their statements should be seen (not tested here). If your suite passes but you expect findings, run
  with `--queryguard-report`: a test that shows 0 statements was not seen. For
  Django see the [comparison](comparison.md).
- The repeat rules count per test by default, so a test that makes several
  independent requests can report a repeat that is no loop. Set
  `queryguard_scope = session` to count per `Session`; that has limits
  of its own, listed in [Tests with several sessions or
  requests](several-sessions.md).
- Everything in `site-packages` counts as library code. If your application
  is installed non-editable (for example `pip install .` in a Docker CI
  image), its frames are skipped too, and call sites point to the test file
  instead of the application line.
- A call site is the first stack frame that is not SQLAlchemy, pytest, the
  standard library or `site-packages`. When a SQLAlchemy callable is handed to
  a worker thread directly, as in `await asyncio.to_thread(session.execute,
  stmt)`, `anyio.to_thread.run_sync(session.execute, stmt)` or
  `loop.run_in_executor(None, session.execute, stmt)`, the worker thread's
  stack holds only the executor, SQLAlchemy and the driver, so the finding says
  `first at <unknown location>`. In the SARIF file such a result is located at
  the test instead of being left out, but the line that issues the statement is
  not known. To get the call site, wrap the call in a function of your own, so
  that an application frame runs in the worker thread:
  `await asyncio.to_thread(lambda: session.execute(stmt))` or a named helper.
- One measurement is active per process at a time, so tests in one process
  must run sequentially. Under pytest-xdist each worker measures its own
  tests, so detection and budgets work. The report needs a single process,
  so under pytest-xdist it prints
  `queryguard report: not available with pytest-xdist`.
- A thread a test starts and does not stop (a daemon worker, a server started
  in the test body) goes on into the tests after it: its statements count
  for whichever test is measured while they run, in its findings and its
  budgets, and cannot be told apart from that test's own. Stop and join such
  threads in the test or in the teardown of its fixture. Statements that run
  between two test bodies are not counted.
- Tests that use `pytester` to run pytest in-process are also measured by the
  outer test, which sees the inner run's queries. Mark such tests with
  `queryguard_ignore` or use `pytester.runpytest_subprocess()`.
- unittest `setUp` and `tearDown` run inside the test call, so their queries
  are counted, unlike pytest fixtures.
- A non-strict `xfail` test that has an N+1 is reported as xfailed rather
  than XPASS, because the N+1 failure looks like the expected failure.
- `INSERT`, `UPDATE` and `DELETE` never count towards the repeated-statement
  rule: at cursor level a unit-of-work flush cannot be told apart from an
  application write. Row-by-row writes from one source line can be reported
  by the opt-in `write-loop` kind (`queryguard_write_loop_threshold`), and a
  budget counts every statement, writes included. The statements of one ORM
  flush count as one write, so `add_all()` plus one `flush()` is not reported
  even when the dialect sends one `INSERT` per row. That relies on finding
  `Session._flush` on the call stack, a SQLAlchemy internal.
- The repeat rules leave pagination and chunked `IN` loads alone only when the
  bound parameters show them (see "Pagination and chunks" in
  [How detection works](how-detection-works.md)). Composite keyset conditions,
  keyset walks on a string or date column, MySQL's `LIMIT offset, count`,
  literal `IN` lists, `IN` lists shorter than 50 values and the same keys
  loaded twice in one test are still reported. A loop that sends 50 or more
  keys per item, each item's own and the same number for every item, looks
  like chunks and is not reported. Neither
  rule can tell a polling loop that repeats a statement on purpose from a
  loop-invariant query inside a loop: both repeat one statement with the same
  parameters, so both are reported. Raise `queryguard_repeat_threshold` or mark
  the test with `queryguard_ignore` for such code. The
  [accuracy page](accuracy.md) lists the scenarios.
- A few statements below the thresholds are not reported by default: fewer than
  five runs of one statement (`queryguard_small_repeat_threshold` reports them)
  and fewer than five instances that reload columns after a commit
  (`queryguard_column_load_threshold`). The same thresholds make the opt-in
  rules report legitimate code: a cached lookup that runs twice, or two objects
  read after a commit.
- The write-loop rule cannot tell test seeding (Factory Boy creating rows with
  a flush each, or a repository's `create()` called per row by the test) or a
  per-row insert with its own error handling from a write loop in the code
  under test. It is off by default for that reason; see
  [Factory Boy and seeding](recipes-factory-boy.md) for suites that seed in
  the test body.
- The PostgreSQL index check needs PostgreSQL 16 or newer, and a sync engine or an
  async engine on asyncpg. It is tested on 16, 17 and 18; a newer server is
  checked, with one notice that it is untested (see
  [PostgreSQL versions](index-check.md#postgresql-versions)). Other async drivers, such as psycopg's async
  mode, are skipped with a notice. The test suite uses pg8000 and asyncpg,
  and CI runs it once more with psycopg 3 (sync and async), installed only
  for that run since it is LGPL-licensed. psycopg2 is not run in CI; it was
  checked by hand during review and gives the same findings for plain
  statements, `%` in SQL, expanding `IN`, autocommit and `stream_results`.
- On asyncpg the check needs PL/pgSQL, which PostgreSQL installs by
  default. Without it, each statement is counted as one PostgreSQL could
  not plan. After the first check, the connection knows the custom setting
  `queryguard.plan`, with an empty value: `current_setting('queryguard.plan')`
  returns `''` there instead of an error.
- `EXPLAIN` does not run the statement, but PostgreSQL's planner evaluates
  calls to functions marked `IMMUTABLE` that have constant arguments while it
  plans. A function that is marked `IMMUTABLE` and still has a side effect
  therefore runs again for every plan the check asks for: once for the check's
  `EXPLAIN` and, when the [index advisor](index-advisor.md) verifies its
  suggestions with hypopg, once more for the plan without them and twice for
  each hypothetical index it tries (three more for a single suggestion). The savepoint rolls back what is transactional, but not a
  `nextval()` call, an advisory lock or a write through `dblink` or
  `COPY ... TO PROGRAM`. A test that sees a sequence advance by more than it
  expects can look for such a function.
- The index check looks only at `SELECT` and `WITH` statements and judges
  plans, not data: it says that no index can serve a filter, not that an
  existing index is a good one. A missing index that only a join would use
  (a join without a `WHERE` on that table) is not reported, because the
  plan for it depends on statistics.
- The index check plans with a generic plan, which cannot see bound values.
  When a table has a partial index (`... WHERE status = 'open'`) whose
  predicate columns are all filtered by the statement, and one of them is
  compared with a parameter, the plan cannot tell whether the index serves
  that value. Such a statement is not reported and gets no advice; the
  session summary counts them (`N statements skipped: a partial index on
  <table> may serve them; the generic plan cannot tell`). A genuinely
  missing index on such a statement is therefore not found.
- A partitioned table is reported under its own name, once, and
  `queryguard_index_ignore_tables` matches that name, not the names of its
  partitions, also when the statement names a partition itself. An ignore
  or baseline entry that names a partition no longer matches (see the
  [index check](index-check.md) for the migration).
- The index check clears its catalog cache on DDL it sees through
  SQLAlchemy in the same process. DDL run another way while the session
  runs, through a raw DB-API connection, `psql`, another process or a
  function called from a `SELECT`, is not seen, and the check may judge an
  index by its old definition. A fresh pytest session starts with an empty
  cache.
- The index check's session summary is printed by the process that runs the
  tests, so under pytest-xdist it is not shown. Findings in tests are
  reported as usual.
- On pytest 9.0 and 9.1, a wrongly typed value in the native `[tool.pytest]`
  table for one of pytest's own options (for example
  `console_output_style = 3`) stops pytest with an internal error, with or
  without this plugin. Wrong types for this plugin's options are reported
  as usage errors.
- On SQLAlchemy 2.0.20 to 2.0.49 (and 2.1.0b1, 2.1.0b2), any `do_orm_execute`
  listener makes the outer statement's `yield_per`, `autoflush`,
  `populate_existing` and `identity_token` options load options of its
  `selectinload` and `immediateload` statements (SQLAlchemy issue 13301, fixed
  in 2.0.50). With `yield_per` the loader statement fails with "Can't use the
  ORM yield_per feature in conjunction with unique()"; with `autoflush=False`
  it no longer flushes. The plugin installs such a listener only for strict
  mode (`queryguard_strict`) and the unused eager-load check
  (`queryguard_unused_eager_load`), both off by default, and the in-app mode
  installs none; then, on those versions, it removes the inherited options from the loader
  statement before SQLAlchemy reads them, and the statement runs as it does
  without the plugin; the cursor still gets the outer statement's options.
  This happens only when the plugin's listeners are the only ones: with a
  `do_orm_execute` listener of your own the plugin changes nothing, because
  your listener triggers the same issue without the plugin. Upgrade to
  SQLAlchemy 2.0.50 or later to remove the cause.
- Relationship and column loads are counted per `SELECT` sent to the
  database. A load that a `do_orm_execute` listener answers without one
  (a result cache) is not counted; one that runs several (horizontal
  sharding runs the lazy load once per shard, a `handle_error` listener
  that retries the statement) counts once per `SELECT`.
- With several `Session`s on one connection (a second `Session` joined to
  the external connection of a "test in a transaction" fixture) and
  `queryguard_scope = session`, the query of a top-level
  `subqueryload` belongs to the `Session` that began on the connection last:
  SQLAlchemy does not say which `Session` runs it. Lazy loads, column loads
  and `selectinload` queries belong to their own `Session`.
- SQLAlchemy checks an object's load context after the `refresh` instance
  event only while a listener for that event exists, and the plugin always
  has one (it notes which query read each object). A query with
  `populate_existing` and a `subqueryload()` of a self-referential
  relationship moves the objects to the subquery's context inside
  SQLAlchemy, before the event, so with the plugin installed SQLAlchemy emits
  `SAWarning: Loading context for <...> has changed within a load/refresh
  handler`; without it, the same load runs silently. The loaded data is the
  same. A suite with `filterwarnings = error` can ignore that message
  (`ignore:Loading context for:sqlalchemy.exc.SAWarning`) or use
  `selectinload()`, which does not move the objects.
