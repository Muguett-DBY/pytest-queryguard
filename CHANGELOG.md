# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Each finding shows a short stack: under `first at`, a `called from` line
  for each line of your code that called it, innermost first, up to the test,
  with SQLAlchemy, pytest and plugin frames left out, so the path through
  helpers is visible. `queryguard_stack_depth` sets how many (3 by default,
  `0` for none). The SARIF file carries them as a code flow, which GitHub
  shows as the path to the alert, and the JSON file as `stack` on each
  finding, and `CallSite.callers` of `record_queries()` has them too. They
  are recorded once per line and test, not per statement (#226).
- A `queryguard` fixture for budgets inside a test:
  `with queryguard.max_queries(3):` fails above 3 statements,
  `with queryguard.num_queries(3):` unless exactly 3 (pytest-django's
  `django_assert_max_num_queries` and `django_assert_num_queries`), both with
  `QueryBudgetExceeded` and a `QueryLog` of the block. The blocks count only
  the statements of the test's own thread or asyncio task (and of tasks and
  threads that run in a copy of its context). `queryguard.queries` and
  `queryguard.count` are the statements the test function has run so far,
  from every thread. `QueryGuardFixture` is its type for hints (#233).
- `pytest_queryguard.ignore(*kinds, reason=None)`, a `with` block (and a
  decorator) that leaves out the findings of those kinds made inside it, in a
  test or in the application code: `with ignore("relationship-load"):`. A
  finding is left out only when everything it counts ran inside the block, in
  the thread or asyncio task that entered it (or a task created inside it);
  threads started inside the block are not covered unless they run in a copy
  of the context. Strict mode does not raise inside it, and
  `log.assert_no_nplusone()` and the in-application mode honour it too; past
  `queryguard_max_events` it leaves nothing out. A block that left out
  nothing in the whole run, for a kind that was checked, is listed under
  "stale ignores" with its file and line, also with pytest-xdist (#234).

### Changed

- CI runs the MySQL and MariaDB index check against live servers: a `mysql`
  job starts MySQL 8.4 and MariaDB 11.8 from their official Docker images and
  runs the MySQL test files with PyMySQL and aiomysql. The job sets
  `QUERYGUARD_REQUIRE_BACKENDS`, which turns the skip of the live tests (no
  URL, no server, no driver) into a failure, so it cannot pass without having
  run them. The check was tested by hand before (#277).

### Documentation

- The comparison lists the current releases of the other tools, checked on
  PyPI on 2026-11-05: queryspy 0.6.0 and django-zeal 2.3.0 (the measurements
  stay as made, on queryspy 0.5.0), and adds orm-lens, a static analyser of
  Django and SQLAlchemy models (#281).

## [0.6.0] - 2026-11-05

This release adds strict mode (raise at the lazy load), a new finding kind,
`cartesian-load`, a default query budget and a budget file written from a run,
per-test JSON output and a pull-request comparison of query counts in the
GitHub Action, a link to the documentation in every finding, and a summary
that names a finding of many tests once. In-app `track()` works as a
decorator, with `async with` and with `start()`/`stop()`.

Upgrading from 0.5.0: `cartesian-load` warns by default. A suite that turns
warnings into errors (`filterwarnings = error`) and has such a query fails
after the upgrade; fix the query, ignore the finding, or set
`queryguard_cartesian_load = off`. Everything else is opt-in.

### Added

- `scripts/compare/run.py` measures the overhead of pytest-queryguard,
  pytest-querycount and queryspy side by side, each in its own environment,
  and `docs/performance.md` has its results for 0.5.0, 0.4.0 and 0.6.0 (#253).
- In-app mode: `track()` works as a decorator (one unit per call, named
  after the function; coroutine functions across their awaits, generator
  functions over their iteration), as `async with`, and through
  `start()`/`stop(failed=...)` for integrations whose hooks start and end a
  unit separately, such as Celery's `task_prerun`/`task_postrun` signals.
  One `track()` object can be entered by several threads at once.
  `docs/in-app.md` has recipes for Celery and RQ (#240).
- Strict mode, `queryguard_strict = true` or `--queryguard-strict`: the
  lazy load of a relationship for the second parent loaded at one line
  (`queryguard_relationship_threshold`) raises
  `NPlusOneDetected` at the access, with a traceback that ends at the line
  that read the attribute, instead of a report when the test ends. Single
  objects never raise; `allow_nplusone`, `queryguard_ignore` and the baseline
  apply as for the report (#239).
- A new finding kind, `cartesian-load`: one SELECT that loads two collections
  with `joinedload` side by side, e.g.
  `select(Customer).options(joinedload(Customer.orders), joinedload(Customer.notes))`,
  whose rows grow as the product of the collections while the query count
  stays at one. Chains, `selectinload` and `contains_eager()` are understood;
  a product inside the SELECT of a `selectinload` is found too. It warns by
  default; `queryguard_cartesian_load = fail` or `off` (#229). A suite that
  turns warnings into errors (`filterwarnings = error`) and has such a query
  fails after the upgrade: fix the query, ignore the finding, or set
  `queryguard_cartesian_load = off`.
- `queryguard_max_queries` gives every test without a `max_queries` marker a
  default query budget (#232).
- A budget file: `pytest --queryguard-budgets=queryguard-budgets.json
  --queryguard-update-budgets` records each test's query count, and with
  `queryguard_budgets` set a test that runs more queries than its entry
  fails, saying where the limit comes from. An update on part of the suite
  keeps the other entries, one on the whole suite drops tests that no longer
  exist, one that stops early writes nothing; pytest-xdist included. The order
  is marker, file, default (#232, #244).
- On a pull request the GitHub Action compares the per-test query counts with
  the latest run on the base branch and writes the result to the job summary
  and to one pull-request comment, updated on later pushes: tests that run
  more statements, new findings and fixed ones. The action now writes
  `--queryguard-json` and uploads it as an artifact; new inputs `json-file`,
  `compare`, `comment` and `github-token`. `python -m pytest_queryguard.compare
  base.json head.json` prints the same report locally (#231).
- `--queryguard-json=PATH` writes each test's statement count, the count by
  verb, its database time and its findings (baselined ones marked) as JSON, also with
  pytest-xdist, with `complete: false` for a run that stopped early. It is the
  input for comparing a pull request's query counts with its base branch
  (`docs/ci.md`, #230).
- The terminal summary names a finding that several tests reported once,
  with the number of tests and of places in the code it came from:
  `30 tests  relationship-load  Order.customer  (900 times in total, from one
  place in the code; e.g. tests/test_orders.py::test_list[0])`, the most tests
  first, also with pytest-xdist. A shared helper that runs an N+1 no longer
  hides behind 30 identical blocks (#228).

### Changed

- Each finding in a report has a `docs:` line, after its `kind:` line, with a
  link to the section of the documentation that explains its kind, e.g.
  `docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load`.
  The SARIF rules link to the same sections in `helpUri` instead of the
  README, so GitHub code scanning shows them as the rule's help (#227).
- The report of a relationship load says where the parents were loaded
  (`parents loaded at app/orders.py:21 in _base_query`): the line that ran the
  query whose rows became the parent objects, which may be a helper far from
  the loop. For parents reached through other relationships the `fix` line
  chains the loaders from that query, e.g.
  `select(Order).options(joinedload(Order.customer).joinedload(Customer.country))`
  instead of `select(Customer).options(joinedload(Customer.country))`, which
  named a query the code does not have. When the origin is unknown (objects
  the test created, loaded before the test body, or read by two queries from
  different places) the report is as before. The pytest plugin only; the
  in-app mode keeps the earlier hint.
  The plugin listens to the ORM's `load` and `refresh` instance events for
  this: in the overhead benchmark (in-memory SQLite) about 0.6 microseconds
  per loaded object, and a few microseconds per ORM query (#225).

### Fixed

- The in-app panel's Content-Security-Policy lists the SHA-256 hashes of the
  page's own script and style instead of `'unsafe-inline'`, so an injected
  script or style would not run. The JSON URL moved from the script into a
  `data-json-url` attribute to keep the script the same on every mount
  (#242).
- On psycopg 3, the index check and the index advisor's hypopg verification
  run `EXPLAIN` without preparing it. psycopg prepares a statement on the
  server once a connection has run it five times, and the prepared `EXPLAIN`
  of a statement with `$n` placeholders failed ("bind message supplies 0
  parameters"): the advisor said "could not verify this index" for a second
  table or a partial-index candidate (#255).

## [0.5.0] - 2026-11-01

The notes of the three release candidates (0.5.0rc1 to 0.5.0rc3) are merged
below by section; the entries of the candidates follow unchanged.

This release adds an experimental in-application mode: the same N+1 rules and
query budgets on every HTTP request of a running FastAPI, Starlette or Flask
application, for development and staging. There is nothing to do when
upgrading from 0.4.1: the pytest plugin behaves as before, and no option or
default changed. The in-app mode is listed under "Experimental" in the
[stability policy](docs/stability.md#experimental), so its names may still
change in a minor release.

### Added

- An experimental in-application mode, `pytest_queryguard.app`: `watch(engine)`
  and `unwatch(engine)` for an `Engine` or `AsyncEngine`, `QueryGuardASGI`
  (FastAPI and Starlette), `QueryGuardWSGI` (Flask and other WSGI apps) and
  `track()` for other units of work. Findings are reported through `logging`,
  `QueryGuardWarning` or a callback, and per-route query budgets apply.
  Statements are collected per request in a `contextvars.ContextVar`, so
  concurrent requests, tasks and threads never mix. It applies the plugin's
  rules, column loads included. Importing it registers nothing. See
  `docs/in-app.md` and `examples/in-app`; `scripts/bench_overhead.py --app`
  times it. Thanks to @Muguett-DBY for reviewing it before release and for
  fixes to its panel and WSGI responses (#203, #210, #211).
- An optional development panel (`panel=True`, off by default) that serves the
  last requests as JSON and as a self-contained HTML page. It answers only
  loopback clients by default and checks the `Host` header against DNS
  rebinding; to serve other clients (`panel_loopback_only=False`) a
  `panel_token` is required, sent as `Authorization: Bearer <token>`.

### Changed

- Tests and CI: the overhead check has a baseline per CPU model and times the
  measurement off and on interleaved, and the release workflow publishes only
  when the release gate passed on the tagged commit.

### Documentation

- How the in-app mode behaves under uvicorn, granian, gunicorn, waitress and
  the Flask development server: per-worker panel history, `BackgroundTasks`
  and streamed bodies, Starlette's `500` handlers, Flask-SQLAlchemy binds.
- The FAQ shows how to check code that runs in a fixture, with
  `record_queries()` and `assert_no_nplusone()`. Suggested by @Muguett-DBY.

## [0.5.0rc3] - 2026-11-01

Third release candidate: install it with `pip install --pre pytest-queryguard`.

The in-application mode now applies the plugin's column-load rules, and its
WSGI middleware no longer cuts a response short under an `error` warnings
filter. Running the mode under uvicorn, granian, gunicorn and waitress found
no other defects; what it showed is now documented. The release checks are
steadier, and a release is published only after the release gate passed on
its commit. The pytest plugin behaves as before; its column-load code is now
split into helpers that the in-app mode shares.

### Changed

- `QueryGuardWSGI` with `report="warn"` under an `error` warnings filter logs
  the error (one ERROR record on `pytest_queryguard.app`) instead of raising
  it from the response body. Raised there, it could not fail the response,
  only cut it short: under gunicorn and waitress a chunked response lost its
  final chunk. ASGI and `track()` still raise it. The in-app mode is
  experimental, so this has no deprecation period.
- Tests and CI: the overhead check (`scripts/bench_gate.py`, the release
  gate's `overhead` job) times the measurement off and on interleaved in the
  same process and keeps the fastest block of each, and measures again up to
  three times when over the limit. The baseline has an entry per CPU model,
  since the ratio differs by up to a third between the CPU models of GitHub's
  runners but repeats within about 0.01 on one model; it is re-recorded on
  the runner for the new method, and `--record` writes it from gate logs. The
  earlier method failed about every other gate run on unchanged code.
- Tests and CI: the release workflow publishes only when the release gate,
  run by hand on `main`, passed on the tagged commit. The gate also runs on
  pull requests that change the overhead benchmark or its baseline.

### Fixed

- The in-app mode labels and counts column loads as the pytest plugin does;
  both now share one classification in `listeners.py`. A `session.refresh()`
  of the object that the latest flush in the same request wrote is no longer
  reported as `column-load 'Item (refresh)'` (one per class and flush, as in
  the plugin). `session.refresh(obj, ["name"])` is a refresh, not an
  `(expired)` load with the `expire_on_commit=False` hint. The column load of
  a joined-inheritance subclass is recognised on SQLAlchemy before 2.0.30
  (#161). The `lazy="selectin"` or `lazy="subquery"` loader that a refresh
  runs for one object is no longer reported as a lazy load.

### Documentation

- The in-app example installs `sqlalchemy[asyncio]`: SQLAlchemy 2.1 no longer
  pulls in greenlet, which the async engine needs.
- The free-threaded note in Getting started says that CI runs the in-app
  mode's tests (`tests/inapp`) on CPython 3.14t; it said those builds were
  not in the CI matrix.
- `docs/in-app.md`, from running the in-application mode under uvicorn,
  granian, gunicorn (sync, gthread, gevent), waitress and the Flask
  development server:
  - Each server process keeps its own panel history, so with several workers
    the panel shows only what the worker that answered has served.
  - `BackgroundTasks` and streamed bodies count for their request, and
    the report arrives after the client has its response.
  - The reported `status` with Starlette's handlers for `500` or
    `Exception`, which run outside `add_middleware()`, and how to include
    them by wrapping the whole application.
  - Watching Flask-SQLAlchemy engines and binds.
  - uvicorn takes the client address from `X-Forwarded-For` sent by
    `127.0.0.1`.

## [0.5.0rc2] - 2026-10-31

Second release candidate: install it with `pip install --pre pytest-queryguard`.

It fixes the in-application mode after an audit, a code review and a security
review of 0.5.0rc1, and adds a token for its panel. The mode stays
experimental. The pytest plugin is unchanged apart from documentation.

### Added

- `panel_token` for `QueryGuardASGI` and `QueryGuardWSGI`. It is required
  with `panel_loopback_only=False`: without one the middleware raises
  `ValueError`. The panel's `requests.json` then answers only
  `Authorization: Bearer <token>` and refuses anything else with `401`; the
  page reads the token from its URL fragment (`/_queryguard/#token=...`).
  See `docs/in-app.md`.

### Changed

- `QueryGuardASGI` and `QueryGuardWSGI` with `panel=True` and
  `panel_loopback_only=False` raise `ValueError` without a `panel_token`. An
  application that sets `panel_loopback_only=False` must add a token. The
  in-app mode is experimental, so this has no deprecation period.

### Fixed

- The development panel of an ASGI application mounted under a `root_path`
  serves its page and history at that prefix. Its JSON URL escapes path
  characters such as `?`, `#` and `%`, and a mount named like the panel no
  longer makes the middleware take over the application's own root. Found
  and fixed by @Muguett-DBY (#210).
- WSGI file responses preserve the server's native `wsgi.file_wrapper`
  iterable, allowing the server to use its optimized transfer path. File
  operations and cleanup stay in the request's query context, and the
  report is finalized when the server closes the wrapper. Found and fixed
  by @Muguett-DBY (#211).
- A budget key whose path has spaces (`"GET /files/my docs/*"`) matches; the
  method is the key's first word when it is letters and hyphens only, and any whitespace
  may separate it from the path.
- Under a warnings `error` filter, the WSGI middleware no longer replaces the
  server's error with a budget warning when the server closes the response
  while handling it, as wsgiref and gunicorn do when the client disconnects.
- The in-app mode no longer calls `Session.get_bind()` to tell which engine a
  lazy load runs on, so an override of it, such as a `ShardedSession`'s
  `shard_chooser` or a replica router, runs only when SQLAlchemy calls it. A
  load whose SELECT runs on an unwatched engine is dropped as it runs, for
  every kind of Session.
- When the last watched engine is garbage collected without `unwatch()`, the
  shared listeners return at once and the next `watch()` or `unwatch()`
  removes them.

- With `panel_loopback_only` on (the default), the panel also refuses a
  request whose `Host` header names another site, so a web page whose name
  was rebound to `127.0.0.1` (DNS rebinding) cannot read the SQL it shows.
  Requests to `localhost`, `*.localhost` or a loopback address, and
  Starlette's test client (`testserver`), are answered as before.
- An engine made with `engine.execution_options(...)` counts as watched with
  its parent: its lazy loads are reported again, and watching both it and its
  parent, in either order, no longer counts every statement twice.

- `report="warn"` warns for every reported request. Python's `default` warning
  action showed each distinct message only once per process and kept every
  one in a registry that grew for as long as the server ran.
- The WSGI panel URL-escapes `SCRIPT_NAME` in the URL its page fetches, as the
  ASGI panel already did for `root_path`, so a mount such as `/team #2` or
  `/café` loads its history.

- `panel_path` must be ASCII and have no empty segment. `"//"` normalised
  to an empty path, so the panel answered every path of the application, and
  a non-ASCII path never matched under WSGI.

### Documentation

- The FAQ shows how to check code that runs in a fixture, with
  `record_queries()` and `assert_no_nplusone()`. Suggested by @Muguett-DBY.

## [0.5.0rc1] - 2026-10-24

Release candidate: install it with `pip install --pre pytest-queryguard`.

This release adds an experimental in-application mode: the same N+1 rules and
query budgets on every HTTP request of a running FastAPI, Starlette or Flask
application, for development and staging. The pytest plugin is unchanged.

### Added

- An experimental in-application mode, `pytest_queryguard.app`, that runs the
  N+1 rules and per-route query budgets on every HTTP request of a running
  application, for development and staging: `watch(engine)` and
  `unwatch(engine)` for an `Engine` or `AsyncEngine`, `QueryGuardASGI` (pure
  ASGI, for FastAPI and Starlette), `QueryGuardWSGI` (Flask and other WSGI
  apps) and `track()` for other units of work. Findings are reported through
  `logging`, `QueryGuardWarning` or a callback. Statements are collected per
  request in a `contextvars.ContextVar`, so concurrent requests, tasks and
  threads never mix (on Python 3.14 builds where threads inherit the
  context, a thread pool needs `copy_context().run` per task for that; see
  `docs/in-app.md`). An optional development panel (`panel=True`, off by
  default) serves the last requests as JSON and as a self-contained HTML
  page. Importing it registers nothing; the pytest plugin is unchanged. It is
  listed under "Experimental" in `docs/stability.md`: its names may change in
  a minor release. See `docs/in-app.md` and the new `examples/in-app`;
  `scripts/bench_overhead.py --app` times it. Thanks to @Muguett-DBY for
  reviewing it before release and for the WSGI response lifecycle fix (#203).

## [0.4.1] - 2026-10-23

A patch release with two fixes to the database time of `record_queries()`,
`max_queries()` and `max_db_time()` when a statement finishes in another thread
after the block. Thanks to @Muguett-DBY, who found and fixed the first one.

### Fixed
- `QueryLog.db_time` now stays fixed after its recording or budget block ends,
  matching the recorded query details. A query finishing in another thread
  after the block no longer changes the finished log's total. Found and
  fixed by @Muguett-DBY (#202).
- `max_db_time()` and the `max_queries()` failure message use that fixed
  total too, so a statement finishing in another thread just after the block
  can no longer fail the time budget or change the reported time.

### Documentation
- `docs/sarif.md` and `docs/ci.md` now state what was verified about GitHub's handling of
  `primaryLocationLineHash` (alerts keep their identity when code moves) and explain the
  per-finding `Calculated fingerprint ... inconsistent fingerprint value` warning that
  `upload-sarif` prints. Closes #189.

## [0.4.0] - 2026-10-14

The notes of the four release candidates (0.4.0rc1 to 0.4.0rc4) are merged
below by section; the entries of the candidates follow unchanged.

This release adds baselines, SARIF output and a GitHub Action, per-finding
ignores, new finding kinds (column loads, write loops, unused eager loads), query
timing and `max_db_time`, an index advisor, and a missing-index check for SQLite,
MySQL and MariaDB. It also adds a documentation site and runnable examples. Four
things can change the result or the output of an existing test run without any new
option: the `column-load` kind is on by default, the index advisor is on by default
with the index check, and the index check now also runs on SQLite, and on MySQL and
MariaDB. They are listed first under "Changed" as behavior changes, with how to turn
them off, as the pre-1.0 clause of the [stability policy](docs/stability.md)
requires. A few other changes can break code that worked on 0.3.0 (the object
`max_queries()` yields, the minimum SQLAlchemy, the labels of partitioned-table
findings, `--continue-on-collection-errors`); they are listed under "Breaking
changes" after the behavior changes.

### Added

**Missing indexes**

- Missing-index check for SQLite, behind the same switch as PostgreSQL
  (`queryguard_index_check`, `--queryguard-index-check`). Each distinct
  `SELECT`, `WITH`, `UPDATE` and `DELETE` in a test body is explained with
  `EXPLAIN QUERY PLAN` and its real bound parameters; the statement is not run,
  so data and transaction are untouched. A scanned table that the statement
  filters or joins on, or an automatic index, is a `missing-index` finding with
  a `CREATE INDEX ix_<table>_<columns> ON <table> (<columns>)` hint. `LIKE`,
  views, `ORDER BY` without a filter, primary keys and unrecognized plan shapes
  are left alone. Works with `sqlite3`, aiosqlite, SQLAlchemy ORM and Core, and
  pytest-xdist; findings honor `queryguard_ignore`, `allow_missing_index`,
  `queryguard_index_ignore_tables`, the baseline and SARIF. See
  `docs/index-check-sqlite.md`. `scripts/bench_overhead.py --index-check` times it.
- Index advisor. Each missing-index finding now suggests an index: a
  `CREATE INDEX` built from the equality, range and `ORDER BY` columns of the
  plan node, a partial index when a constant predicate is selective, and the
  Alembic `op.create_index` call. When the hypopg extension is installed, the
  suggestion is verified inside the check's savepoint: whether the planner
  would use the index and the statement's estimated cost before and after;
  hypothetical indexes are reset, also after errors, on sync drivers and
  asyncpg. No new index is suggested when the table already has one for these
  columns (invalid, partial, or valid but unused), and the finding says why.
  Without hypopg the suggestion stays and one notice says how to install it.
  New options `queryguard_index_advisor` (default `true`, used with the index
  check) and `queryguard_hypopg` (`auto`, `on` or `off`). Findings carry the
  suggestion in an additive `advice` field. See `docs/index-advisor.md`.

- Missing-index check for MySQL and MariaDB, behind the same switch
  (`queryguard_index_check`, `--queryguard-index-check`). Each distinct
  `SELECT`, `UPDATE` and `DELETE` is explained with `EXPLAIN` and the real bound
  parameters (never `ANALYZE`); a table read in full (`type: ALL`, or an index
  read in full on a column that leads no index) although the statement filters
  or joins on its columns is a `missing-index` finding with a `CREATE INDEX`
  hint and an Alembic call. The suggestion is not verified: neither server has
  hypothetical indexes. Tested with PyMySQL and aiomysql on MySQL 8.4 and
  MariaDB 11.8 on a developer machine; CI does not run a MySQL or MariaDB
  server. See `docs/index-check-mysql.md`.

**New findings and rules**

- Column-load detection, a new finding kind `column-load`. Instances that each
  load a deferred column, or whose attributes were expired by `commit()`,
  `expire()` or `expire_all()`, on their own are reported with the right
  advice: `undefer(Model.column)` (or removing `defer()`, `load_only()` or
  `deferred()`), or `expire_on_commit=False`; `session.refresh()` in a loop
  gets its own hint. The ini option `queryguard_column_load_threshold`
  (default 5 instances) and the `column_load_threshold` argument of
  `QueryLog.assert_no_nplusone()` set the limit. The kind works with
  `queryguard_ignore` and `allow_nplusone`.
- Write-loop detection, opt in: `queryguard_write_loop_threshold` (off by
  default) and the `write_loop_threshold` argument of
  `QueryLog.assert_no_nplusone()`. The same `INSERT`, `UPDATE` or `DELETE`
  shape run row by row from one source line is reported as the new finding kind
  `write-loop`, with a hint to use `session.add_all()`, `executemany` or a bulk
  statement. `executemany` and multi-row `VALUES` statements are not counted.
  The statements of one ORM flush count as one write, so `add_all()` and one
  `flush()` is not reported even where the dialect sends one `INSERT` per row;
  a `flush()` or `commit()` per row is. The flush is found through SQLAlchemy's
  `Session._flush` frame; if that changes, the rule counts every statement.
  The kind works with `queryguard_ignore`, `allow_nplusone` and
  `queryguard_fail_on`.
- Experimental detection of unused eager loads, off by default:
  `queryguard_unused_eager_load` (`off`, `warn` or `fail`). A relationship a
  test loads with `selectinload()`, `joinedload()`, `subqueryload()` or
  `immediateload()` and never reads is reported as the new finding kind
  `unused-eager-load` (label `Class.relationship`), with a hint to remove the
  option. While the option is `off` nothing is installed. Opt out with
  `allow_nplusone(kind="unused-eager-load")` or `queryguard_ignore`. See
  `docs/unused-eager-load.md` for how it works and its limits.
- `queryguard_small_repeat_threshold`, an ini option (off by default) and the
  `small_repeat_threshold` argument of `QueryLog.assert_no_nplusone()`: a
  statement that ran a few times (below `queryguard_repeat_threshold`) from one
  source line, each time with different parameters, is reported as a repeated
  statement. Identical parameters, runs from several lines, writes and
  pagination are left alone. The default threshold of 5 is unchanged, so a batch
  of two to four statements is not flagged unless this is turned on.

**Adopting it on an existing project**

- Baseline file for existing findings: `queryguard_baseline` (ini) or
  `--queryguard-baseline=PATH`, and `--queryguard-update-baseline` to write it.
  A finding whose identity (kind, label, file and function; rootdir-relative
  paths, no line numbers or counts) is in the file does not fail or warn, for
  every finding kind. New findings are reported as before, and entries that
  matched nothing in the run are listed as stale baseline entries. The file is
  JSON with a `version` field, sorted for clean diffs; a missing, corrupt or
  unknown-version file is a usage error. Works with pytest-xdist: workers apply
  the file, the controller merges what they saw and writes it on an update.
  See `docs/baseline.md`.
- SARIF output: `--queryguard-sarif=PATH` writes the findings of the run as
  SARIF 2.1.0 for GitHub code scanning (the `--queryguard-report` flag is
  unchanged). One rule per finding kind with a fix hint, one result per
  finding identity with a rootdir-relative location, `partialFingerprints`
  derived from the identity, and `error` or `warning` level following
  `queryguard_fail_on`. Ignored and baselined findings are left out. Works with
  pytest-xdist: workers send their findings to the controller, which writes the
  file once. See `docs/sarif.md`.
- A composite GitHub Action in `action/`: installs the plugin, runs pytest with
  `--queryguard-sarif`, uploads the file with `github/codeql-action/upload-sarif`
  (pinned by commit SHA) and fails the step when pytest fails. Inputs:
  `pytest-args`, `python-version`, `package`, `baseline`, `fail`,
  `working-directory`, `sarif-file`, `upload`, `category`. The upload is skipped
  for pull requests from forks. A CI job runs the action on
  `examples/action-sample`. See `docs/ci.md`.

**Ignoring and gating**

- `queryguard_ignore`, an ini option that leaves single findings out of the
  reports. One pattern per line, `KIND LABEL [file=GLOB] [function=GLOB]`,
  matched with `fnmatch`. The kinds are `relationship-load`,
  `repeated-statement`, `column-load`, `write-loop`, `unused-eager-load` and
  `missing-index`; `file` is relative to the rootdir.
- `allow_nplusone` takes label patterns and the keywords `kind`, `file` and
  `function` to ignore only the findings they match. Without arguments it
  still ignores the whole test.
- Ignore patterns that matched no finding in the run are listed in the
  terminal summary as stale ignores (not with pytest-xdist).
- `queryguard_fail_on`, an ini option with finding kinds (`fnmatch` patterns,
  one per line). In `fail` mode only these kinds fail a test; the other kinds
  are reported as `QueryGuardWarning`. Empty means every kind fails, as before.
  Budgets are not affected.

- `queryguard_repeat_scope` (`test` or `session`, default `test`) and
  `QueryLog.assert_no_nplusone(repeat_scope=...)` for false positives in tests
  that make several requests. The repeat, relationship,
  column-load and write-loop rules counted over the whole test, so a test that
  made several independent requests (a test client, for example, with one
  `Session` per request) reported the same authentication or organisation
  lookup as a loop. With `session`, each `Session`, or each connection checkout
  for statements no `Session` ran, is counted on its own. The default is
  unchanged, but a finding whose count spans several Sessions or checkouts now
  says so and names the option. See `docs/several-sessions.md`.

**Timing and budgets**

- Per-statement timing. `Query.duration` is the time in seconds the database
  and the driver took for a statement, measured around the cursor call;
  `QueryLog.db_time` is the total and `QueryLog.slowest(n)` the slowest
  statements.
- `max_db_time(seconds)`, a context manager, and the `max_db_time` marker:
  fail when the statements take longer than the limit in total. The failure
  lists the slowest statements. A `max_queries` failure now shows the total
  database time and its slowest statements as well.

**Documentation and examples**

- A documentation site built from `docs/` (`scripts/build_docs.py`, with the new
  `docs` dependency group: Markdown and Pygments): getting started, how detection
  works, a configuration reference that a test keeps in step with the options, FAQ,
  limitations, a comparison with other tools and a migration guide from nplusone.
  The README is now a short overview that links to it.
- `examples/` with runnable projects (sync N+1, async FastAPI, PostgreSQL and SQLite
  missing indexes, baseline and SARIF), each with a README and a smoke test in CI.
- Community files: `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`
  (Contributor Covenant 2.1), issue forms and a pull request template.
- The documentation site has a description, canonical URL and Open Graph tags
  on every page, a favicon, `sitemap.xml`, `robots.txt`, a social preview
  image, a client-side search (no third-party code), "Edit this page" links
  and Previous/Next links. Every page needs a `description` in
  `docs/site.toml`; the strict build fails without one.
- A "Recipes" section in the documentation: FastAPI (`TestClient` and
  `httpx.AsyncClient` with dependency-overridden sessions), Flask-SQLAlchemy,
  SQLModel, Litestar, pytest-asyncio configuration pitfalls, budgets for hot
  endpoints, adopting the plugin on a legacy codebase and CI. A new example,
  `examples/fastapi-dependency-override`, is run by the test suite.
- `QueryGuardDeprecationWarning`, a `DeprecationWarning` subclass exported from
  `pytest_queryguard`, is the class the deprecation policy uses. It is not a
  `QueryGuardWarning`, so `filterwarnings = ["error::pytest_queryguard.QueryGuardWarning"]`
  (and `ignore::` for findings) neither turns a deprecation into a failure nor
  hides it. `docs/stability.md` names it in step 1 of the policy. Nothing is
  deprecated yet.

### Changed

**Behavior changes**

- The `column-load` finding kind is on by default (threshold 5, see
  `queryguard_column_load_threshold`). A test that loops over deferred columns, or
  over objects expired by `commit()`, passed before and now fails in `fail` mode.
  To keep such tests green while you fix them, use a baseline, `queryguard_fail_on`
  without `column-load` (it then only warns), or
  `queryguard_ignore = ["column-load *"]`.
- Those loops used to be reported as a `repeated-statement` with the wrong advice
  (`WHERE id IN (...)`); they are no longer counted by that rule, so a baseline or
  ignore entry for such a finding has to use the new kind.
- The index advisor is on by default whenever the index check is on
  (`queryguard_index_advisor = false` turns it off). Missing-index findings now carry
  a `fix: CREATE INDEX ...` line and an `alembic:` line, and without the hypopg
  extension one notice at the end of the session says how to install it
  (`queryguard_hypopg = off` silences it).
- `queryguard_index_check` now also checks SQLite. A project that turned the check
  on and ran its tests on SQLite used to get a "not checked" notice and now gets
  findings, which fail tests in `fail` mode.
- `queryguard_index_check` now also checks MySQL and MariaDB. A project that
  turned the check on and ran its tests on MySQL or MariaDB used to get a "not
  checked" notice and now gets findings, which fail tests in `fail` mode. Turn the
  check off again with `queryguard_index_check = false`, skip tables with
  `queryguard_index_ignore_tables`, or record the findings in a baseline.
- Durations below one millisecond are printed in microseconds (`37 us`) instead of
  `0.0 ms` in the report and in budget failures.
- The hint for a lazy-loaded many-to-one or one-to-one relationship is now
  `joinedload(...)`; collections still get `selectinload(...)`.
- A test stores at most 10000 statements and 10000 relationship loads for its
  reports (`queryguard_max_events`, `0` for no limit). Past the limit they are only
  counted.
- When a listener raises, the plugin still stops measuring so that it never breaks
  the suite, but it warns and prints a "queryguard: measurement stopped" section
  instead of staying silent (see Fixed).

The other new options are off by default and change nothing until you turn them on:
`queryguard_small_repeat_threshold`, `queryguard_write_loop_threshold`,
`queryguard_unused_eager_load`, `queryguard_baseline`, `queryguard_fail_on` (empty:
every kind fails, as before) and `--queryguard-sarif`.

**Breaking changes**

These can break code or configuration that worked on 0.3.0.

- `max_queries()` and `max_db_time()` now yield the public `QueryLog` (the
  object `record_queries()` yields) instead of the internal `QueryCollector`,
  and are annotated `AbstractContextManager[QueryLog]`. `with max_queries(3) as log:`
  gives `log.count`, `log.queries`, `log.slowest()` and `log.assert_no_nplusone()`.
  Code that read `.statements` of the yielded object, an undocumented
  attribute, must use `log.queries` or `log.count`.

- The declared minimum SQLAlchemy is now 2.0.13 (`sqlalchemy>=2.0.13`). The
  suite and the async helpers need `AsyncAttrs`, which does not exist in
  earlier 2.0 releases, so the old `>=2.0` bound promised more than was tested.

- PostgreSQL index check: a statement on a partitioned table is now one
  finding on the partitioned table, with `CREATE INDEX ON <table> (...)` (which
  creates the index on every partition) and its hypopg verification, instead of
  one finding and one `CREATE INDEX` per partition
  ([#86](https://github.com/MaciejZiel/pytest-queryguard/issues/86)). An
  ignore or baseline entry that named a partition no longer matches: replace
  the partition's name with the partitioned table's in
  `queryguard_index_ignore_tables`, `allow_missing_index` and `missing-index`
  patterns, and record the baseline again with `--queryguard-update-baseline`
  (see `docs/index-check.md`). A statement that names a partition itself is
  reported under the partitioned table too; inheritance children
  (`INHERITS`) are still reported one by one. Each partition is looked up once
  per session (`pg_partition_root()`), only for statements with findings. The
  advisor's reason for an invalid existing index now also names
  `CREATE INDEX ON ONLY` a partitioned table whose partitions are not all
  attached.

- A serial run with `--continue-on-collection-errors` and a collection error
  no longer rewrites the baseline or writes the `--queryguard-sarif` file, as it
  did before (under pytest-xdist, and in a run with `-x`, this was already so). The tests of the module that
  did not collect never ran: a rewritten baseline would drop their entries and
  an uploaded report would close their alerts. The summary gives the
  interrupted-run reason; the GitHub Action skips the upload with a warning.

**Internals**

- The missing-index check reads the conditions of a plan node with a tokenizer
  and parser (`pytest_queryguard.predicates`) instead of regular expressions
  over the text. PostgreSQL's JSON plan has no structured form of a predicate,
  so the relation, alias, schema and index still come from the plan's fields and
  the condition text is parsed into a tree; the old text matching stays as the
  fallback for syntax the parser does not know. Findings do not change. The
  parsing is tested on real `EXPLAIN (GENERIC_PLAN, VERBOSE, FORMAT JSON)` plans
  captured from PostgreSQL 14 to 18 (`scripts/capture_plans.py`).
- The measurement registers one more SQLAlchemy listener (`after_cursor_execute`)
  to time statements. `scripts/bench_overhead.py` puts the added cost of all the
  measurement, not of this listener alone, at about 20 to 55 microseconds per
  statement on in-memory SQLite (roughly +30 to +40 %); against a real database
  round trip it is small. See the FAQ.
- Internal: detectors now hand their results to a stable `Finding` model
  (`kind`, `label`, `file`, `function`, `message`, `severity`) before the
  report is rendered. The messages and the public API are unchanged.
- The check that decides whether a stack frame belongs to SQLAlchemy, pytest or
  the standard library is now cached per file name. Measured with
  `scripts/bench_overhead.py`, this cuts the per-statement overhead of the
  measurement by about half.
- Tests: an accuracy corpus under `tests/corpus`: 110 small scenarios of realistic ORM
  code (views, serializers, FastAPI, Flask-SQLAlchemy, SQLModel, repositories,
  pagination, bulk writes, async), each marked BUG or CLEAN, run through the
  plugin in its default configuration and with each opt-in rule. Limitations it
  found are in `docs/limitations.md`; `scripts/accuracy_report.py` prints the
  precision and recall.
- The repeated-statement rules no longer report pagination and chunked `IN`
  loads when the bound parameters show them: only the `OFFSET` or an integer
  keyset bound changes, in one direction, by at least a page of more than one
  row (the walk may be repeated once); or one `IN` list of at least 50 values
  changes, the lists share no value and all but one have the same length.
  `LIMIT 1 OFFSET n` walks, a page per item, a keyset bound per row, string
  and date keyset bounds, per-item `IN` lists, a paginated lookup inside a
  loop and identical repeats are still reported. Closes #60.
  The accuracy corpus has five more CLEAN scenarios for these shapes and five
  more BUG scenarios that imitate them; on its 141 scenarios the default
  repeated-statement precision goes from 80% to 97% (90% to 97% without the
  five CLEAN ones), recall is unchanged at 95%, no BUG scenario changes in any
  configuration, and the release gate's minimum precision for the kind is now
  90%. See "Pagination and chunks" in `docs/how-detection-works.md`.

- `docs/stability.md` has an "Experimental" section: the
  `queryguard_unused_eager_load` option, the `unused-eager-load` finding kind
  and the missing-index check on MySQL and MariaDB are exempt from the
  deprecation policy and may change or go in a minor release with a changelog
  entry. The policy had no such tier although the option was labelled
  experimental.

- The options with a fixed set of values now agree on case. `queryguard_mode`,
  `queryguard_hypopg` and `--queryguard-mode` used to reject `FAIL`, `ON` and
  `Warn` while `queryguard_unused_eager_load` and `queryguard_repeat_scope`
  accepted any case; all of them now ignore case and surrounding spaces, and
  unknown values are still a usage error.

- Lower the per-statement overhead by about a sixth in the in-memory SQLite
  micro-benchmark: one shared `CallSite` per source location, one write check
  per statement and a cache for `normalize()`. Behaviour is unchanged. New
  `docs/performance.md` and `scripts/bench_suite.py` (a whole-suite benchmark
  for file-based SQLite or PostgreSQL).

- `allow_nplusone` with patterns now also applies to missing-index findings.
  `kind="missing-index"` was accepted but never matched anything and was then
  reported as a stale ignore; it now works like the same pattern in
  `queryguard_ignore`. The marker without arguments still leaves the index
  check alone (`allow_missing_index` skips it).

- The accuracy corpus has 23 more scenarios (133 in all): GraphQL-style
  resolvers and DataLoaders, `asyncio.gather` and Celery-style fan-out,
  caching repositories, `with_for_update`, nested transactions, `yield_per`,
  large `selectinload`, ORM bulk statements, upserts, and BUG scenarios that
  look like pagination, batching or polling. `docs/accuracy.md` and the release
  gate's minimum scenario count are updated; the plugin is unchanged.

- Tests and CI: a release gate (`.github/workflows/release-gate.yml`,
  `scripts/release_gate.py`) that checks the test matrix, the accuracy
  minimums, the documentation examples, clean installs, the upgrade from the
  previous release, the overhead budget, the package contents and the
  changelog before a release is tagged. See `docs/releasing.md`.

- The classes and functions in `pytest_queryguard.__all__` now report
  `pytest_queryguard` as their `__module__`. Tracebacks and logs show
  `pytest_queryguard.NPlusOneDetected` instead of
  `pytest_queryguard.api.NPlusOneDetected`, so the printed name matches the
  documented import path and the submodules can move without changing it. A
  `filterwarnings` entry that names the submodule
  (`error::pytest_queryguard.warnings.QueryGuardWarning`) keeps working.

- The keyword of the `max_queries` marker is now `limit`, as in the
  `max_queries(limit)` function: `@pytest.mark.max_queries(limit=3)`. The
  keyword `n=` of earlier releases is still accepted, without a warning. It
  may be removed in a future minor release, but only after a deprecation
  period with a warning, as described in `docs/stability.md`. The positional
  form is unchanged.

- Every finding in a report now has a `kind: ...  label: ...` line, the pair that
  `queryguard_ignore`, `allow_nplusone` and the baseline match on, for example
  `kind: relationship-load  label: Order.customer`. For a repeated statement or
  a write loop the line replaces the separate line with the normalized
  statement (a statement longer than 200 characters is cut and ends with `*`,
  so the printed label still works as a pattern). An ignore pattern that is
  the exact label now matches even when the label holds `[` or `]`. The
  line is in the SARIF message too
  ([#105](https://github.com/MaciejZiel/pytest-queryguard/issues/105)).

### Fixed

- The index check no longer reports a missing index, and the advisor no longer
  recommends a redundant one, for a statement that an existing partial index
  may serve. The generic plan cannot see bound values, so it could not know
  that `status = $1` matches `... WHERE status = 'open'`. Such statements are
  skipped and counted in the session summary. A missing index on them is not
  found; see the limitations.
- Loops over deferred or expired columns were reported as a repeated statement
  with the wrong advice (`WHERE id IN (...)`). They are now a `column-load`
  finding and no longer count towards the repeated-statement rule.
- When a listener raises, the plugin still stops measuring so that it never
  breaks the suite, but it no longer does so silently. The warning names the
  listener or step that failed, and the end of the session prints a
  "queryguard: measurement stopped" section with the listener, the error and
  the line that raised it, once per session, with or without
  `--queryguard-report`.
- A lazy load or column load that autoflushed pending changes first was
  attributed to the flush's write: the UPDATE or DELETE carried the relationship
  name and the load's SELECT carried none, so one loop was also reported as a
  repeated statement. Writes no longer take the pending attribution (#59).
- Documentation: the limitations page, the FAQ and the README now say that a
  raw `sqlite3` or other DB-API connection and non-SQLAlchemy ORMs (Django ORM,
  Peewee, Tortoise ORM) are not seen, and that nothing reports it; the
  limitations page also points to the repeat-scope limits.
- The SQLite index check no longer says a table "is read in full" when the
  plan walks an existing index that the filter cannot use (`SCAN INDEX ix_...`).
  It says the table scans the whole index and that the filter does not match
  the start of any index; the suggested `CREATE INDEX` is unchanged.
- A SARIF result merged from several tests no longer shows one test's count in
  `message.text` next to the total in `properties.count`. The message now
  starts with `Seen in N tests: T times in total, up to M times in one test`,
  and `properties` gains `tests` and `peakCount` for such results. `count`,
  the fingerprints and the baseline identity are unchanged. See
  `docs/sarif.md`.
- The `repeated-statement` hint no longer says to fetch the rows with
  `WHERE id IN (...)` when the statement already uses `IN (...)`. It says the
  batched query ran several times and should be called once, or the calls
  combined.
- SARIF locations are now relative to the repository root when
  `GITHUB_WORKSPACE` is set and the pytest rootdir is inside it. Before, they
  were relative to the rootdir, so with the action's `working-directory` set to
  a subdirectory GitHub created alerts on paths that do not exist and showed no
  pull request annotations. Fingerprints and baseline identities are
  unchanged. See `docs/sarif.md`.
- The action skips the SARIF upload for runs triggered by Dependabot (read-only
  token), and a refused upload no longer fails the job.
- On SQLAlchemy 2.0.20 to 2.0.49 (and 2.1.0b1, 2.1.0b2) the plugin's
  `do_orm_execute` listener triggered SQLAlchemy issue 13301 (fixed in 2.0.50):
  the outer statement's `yield_per`, `autoflush`, `populate_existing` and
  `identity_token` options became load options of its `selectinload` and
  `immediateload` statements. A `yield_per` plus `selectinload` test failed
  with "Can't use the ORM yield_per feature in conjunction with unique()", and
  `autoflush=False` stopped the loader statement from flushing, only because
  the plugin was installed. On those versions the plugin now removes the
  inherited options from a loader statement before SQLAlchemy reads them, so
  the statement runs as it does without the plugin. When a `do_orm_execute`
  listener of your own takes part, the plugin changes nothing: your listener
  triggers the same issue without the plugin
  ([#169](https://github.com/MaciejZiel/pytest-queryguard/issues/169)).

- Tests: the accuracy corpus now checks that a scenario never fails without a
  finding in every run, not only in the pytest-xdist worker that happened to
  measure it.

- On SQLAlchemy 2.0.13 to 2.0.29 the column load of a joined-inheritance
  subclass (reading `manager.budget` after `select(Employee)`) was not
  recognised as a column load and was reported as a repeated statement. Before
  2.0.30 SQLAlchemy reports `is_select` and `is_column_load` as False for the
  `from_statement()` execution that loads those columns; the plugin now also
  reads the refresh state from the statement and the execution options. The
  oldest-versions job of the release gate now installs exactly
  `sqlalchemy==2.0.13`, the declared minimum, instead of the latest 2.0.x.
  Fixes #161.

- Free-threaded CPython (3.14t): statements, relationship and column loads and
  database time reported from several threads into one collector lost updates
  (`counted 15839, expected 16000`), which budgets and the truncation flag rely
  on. The collector now updates them under a lock on a build without the GIL
  and takes no lock with the GIL (`scripts/bench_overhead.py` shows no change
  on CPython 3.12). Tests: the memory test measures what the plugin itself
  allocated, because the whole-process figure on the free-threaded build
  reflects SQLAlchemy's late freeing, not a leak in the plugin. Free-threaded
  builds are still not in the CI matrix; `docs/getting-started.md` says so.
  Fixes #159.

- The repeat rules (repeated statement and small repeat) no longer count
  transaction-control statements: `BEGIN`, `COMMIT`, `ROLLBACK`, `SAVEPOINT`,
  `RELEASE`, `START TRANSACTION` and `SET TRANSACTION`. With SQLAlchemy's
  pysqlite `BEGIN` recipe a loop that commits each pass was reported with
  `BEGIN` as a repeated statement and the advice to fetch rows in one query.
  Budgets still count them. A `BEGIN` that opens a block
  (`BEGIN load_order(:id); END;`) is still counted, and a loop with a
  `SAVEPOINT` per row is still a write loop. The accuracy corpus gives the
  same result before and after. Fixes #158.

- Stability: with `-W error` or `filterwarnings = error`, an internal error
  no longer fails the user's test. `QueryGuardInternalWarning` was raised as
  an exception from the test call, from the terminal summary (which aborted
  it), from a `record_queries()` block and, for the unused eager-load check,
  from inside SQLAlchemy's `do_orm_execute`. Now it is always shown and
  recorded, never raised, and an "ignore" filter still hides it (#124).
- Stability: an error inside the `after_begin` listener no longer reaches the
  user's `Session.begin()`. Like every other listener, it now stops the
  measurement with a `QueryGuardInternalWarning` (#123).
- Stability: a baseline file nested too deep for the JSON parser is a usage
  error instead of an `INTERNALERROR`, and any error while writing the
  baseline is reported instead of crashing the end of the session (#125).
- PostgreSQL index check: an internal error after the check opened its
  savepoint (or, on an autocommit connection, its own transaction) left that
  savepoint or transaction open on the user's connection. In autocommit mode
  the user's later writes were then never committed, and inside a transaction
  `enable_seqscan` stayed `off`. The check now rolls them back before the
  error stops the plugin (#126).
- Stability: an error inside `pytest_configure`, the per-test start or end
  of the measurement, the pytest-xdist hand-over of the findings,
  `pytest_unconfigure` or the attribute-read hook of the unused eager-load
  check no longer ends the session with an `INTERNALERROR` or fails every
  test. It stops the measurement and is reported once, like an error in a
  listener; an error in the xdist hand-over marks the run incomplete, so the
  baseline is not rewritten and the SARIF summary says findings may be
  missing. `KeyboardInterrupt`, `SystemExit` and `asyncio.CancelledError` are
  still never swallowed. `docs/stability.md` describes the contract under
  "Failing open" (#127).

- Tests: the accuracy corpus now runs on PostgreSQL
  (`QUERYGUARD_CORPUS_PG_URL`). Two scenarios (`bug_23_get_or_create_loop`,
  `bug_30_flush_per_insert`) failed there because seeded rows with explicit ids
  did not advance the sequences; the corpus moves them after each setup. The
  results on PostgreSQL 17 match SQLite; see `docs/accuracy.md`. Fixes #120.

- SARIF: a finding whose call site is unknown (for example `session.execute`
  handed straight to a worker thread) is located at the test instead of being
  left out of the file, so code scanning shows it. The terminal text, the
  baseline and the ignore rules are unchanged. `docs/limitations.md` explains
  the cause and the workaround
  ([#132](https://github.com/MaciejZiel/pytest-queryguard/issues/132)).

- GitHub Action: the `package` input now defaults to the plugin in the action's
  own checkout instead of the name `pytest-queryguard`, so the installed plugin
  matches the pinned ref and accepts `--queryguard-sarif` (pip used to pick the
  stable 0.3.0 while 0.4 was a release candidate)
  ([#106](https://github.com/MaciejZiel/pytest-queryguard/issues/106)).

- The index check notice for a database it cannot check (SQL Server, Oracle)
  said the check needs PostgreSQL 16, although SQLite, MySQL and MariaDB are
  supported. It now lists every supported database, and the help texts of
  `--queryguard-index-check` and `queryguard_index_check` name MySQL and
  MariaDB too. Fixes #154.

- PostgreSQL index check: a finding for an index the plan walks end to end (a
  filter on the trailing column of a composite key, a whole index with a filter,
  a whole-index bitmap) now says `t scans the whole index ix: the ... does not
  match the start of any index`, as the SQLite check does, instead of saying the
  table is read in full. The closing note no longer calls such a scan sequential
  ([#130](https://github.com/MaciejZiel/pytest-queryguard/issues/130)).

- A lazy load keeps its relationship when another statement runs between the
  load and its `SELECT`: statements of other `AsyncSession`s running
  concurrently (`asyncio.gather`), the `BEGIN` of SQLAlchemy's pysqlite recipe
  or any statement of a `begin` or `after_begin` listener. The attribution is
  now parked on the `Session` that issued the load instead of on the thread, so
  these loops are no longer reported a second time as a repeated statement.
  Fixes #97 and #99.

- The relationship-load finding shows a lazy load as its sample SQL. It used the
  first statement with the relationship's label, which was the `selectinload`
  query when that ran first; that query is now used only when no lazy load was
  recorded ([#131](https://github.com/MaciejZiel/pytest-queryguard/issues/131)).

- The SARIF level of an unused-eager-load finding now follows the test outcome:
  with `queryguard_unused_eager_load = warn` it is `warning`, not `error`.
  Fixes #98.

- A statement that starts with a comment (sqlcommenter tags, a `-- note` in a
  `text()`) is now classified like the statement after the comment. A loop of
  commented single-row `INSERT`s was reported as a repeated statement with the
  advice to fetch the rows in one query, and the write-loop rule never saw it.
  Fixes #96.

- The MySQL and MariaDB index check no longer fails on a literal `%` in a
  statement that was run with the `no_parameters` execution option: the
  `EXPLAIN` was formatted by PyMySQL (`query % args`) although the statement
  itself was not, so it was reported as "could not be explained" (a literal
  `%%` was sent as `%`). Fixes #116.

- `--queryguard-update-baseline` no longer rewrites the baseline, and
  `--queryguard-sarif` no longer writes the file, after an interrupted run (a
  collection error, Ctrl-C, a usage error or an internal pytest error). The
  summary says why. Fixes #95.

- Tests: the public API snapshot recorded a wrong return type for
  `QueryLog.slowest()` and did not assert it. It now records
  `tuple[Query, ...]` and checks every member of `QueryLog` the same way.

- `--queryguard-update-baseline` writes a usable baseline when a test lives in
  a directory whose name is not valid UTF-8: such characters are written as
  `\udcXX` escapes (other text stays readable), so the entry matches on the
  next run instead of the write failing. Fixes #89.

- PostgreSQL index check: `NOT IN (subquery)` on PostgreSQL 17 and 18 no
  longer gets `fix: CREATE INDEX ON t (col)` for the compared column, which was
  wrong even for a primary key
  ([#83](https://github.com/MaciejZiel/pytest-queryguard/issues/83)). Plan
  conditions with `CASE`, `AT TIME ZONE`, `EXTRACT(... FROM ...)`, `count(*)`,
  `COLLATE` or an array subscript are now parsed instead of matched as text, so a
  column inside them is no longer suggested as a plain index and gets the
  expression-index note
  ([#84](https://github.com/MaciejZiel/pytest-queryguard/issues/84)). A row
  comparison such as `(a, b) > ($1, $2)` no longer asks for an expression index
  ([#85](https://github.com/MaciejZiel/pytest-queryguard/issues/85)).

- PostgreSQL index check: a server newer than the tested releases (16 to 18)
  is still checked and now gets one notice in the session summary that its
  version is untested. Older servers keep their one "not checked" notice. The
  tested versions are listed in `docs/index-check.md`.
- The index check no longer rolls back a transaction the application opened by
  hand on an AUTOCOMMIT connection (`exec_driver_sql("BEGIN")`, with pg8000 or
  asyncpg). The `autocommit` flag is the driver's setting, not the server's
  state, so the check now tries a savepoint first and starts a transaction of
  its own only when none is open (SQLSTATE 25P01). Fixes #91.
- Documentation: the index check page was published as raw Markdown from the
  partial-index paragraph on, including its SQLite section
  ([#117](https://github.com/MaciejZiel/pytest-queryguard/issues/117)). A test
  now fails the build for any page left like that.
- Documented that PostgreSQL evaluates `IMMUTABLE` functions with constant
  arguments at plan time, so the index check and the advisor can call such a
  function again and its non-transactional side effects (for example
  `nextval()`) are not rolled back with the savepoint. Fixes #92.
- The repeated-statement and small-repeat rules no longer count schema
  reflection: `PRAGMA` statements, reads of `sqlite_master`, `sqlite_schema` and
  their temp variants, and reads of `pg_catalog` or `information_schema`
  (`MetaData.reflect()`, `Inspector.has_table()`, `create_all()`, Alembic's
  version-table check). `max_queries` budgets still count them. See
  `docs/how-detection-works.md` for the exact rule
  ([#129](https://github.com/MaciejZiel/pytest-queryguard/issues/129)).
- PostgreSQL index check: a sequential scan whose filter is only not-equal
  comparisons against parameters (`<> $1`, `NOT IN` with bound values) is no
  longer reported as a missing index. No index serves them, so the advice
  could not be followed, even on an indexed column; the SQLite check never
  reported them. A filter with one more servable condition, or a not-equal
  against a constant in the SQL (which a partial index serves), is still
  reported ([#133](https://github.com/MaciejZiel/pytest-queryguard/issues/133)).

- Baseline: a file nested more than 100 levels deep is now rejected by a
  depth check before parsing, so the "nested too deep" usage error no longer
  depends on the interpreter hitting its recursion limit (which on Python 3.10
  could surface as a second, unraisable `RecursionError`).

- `--queryguard-update-baseline` on a partial run (`-k`, `-m`, `--deselect`,
  `--lf`, `--sw`, `--ignore`, a node id or a path narrower than `testpaths`, with
  or without pytest-xdist) no longer drops the entries of tests that did not
  run. Only the entries of tests that ran are replaced, the others are kept, and
  the summary says how many were kept. A deleted test's entries are removed on
  the next full run. `docs/baseline.md` describes which runs are full
  and which are partial. Fixes #148.

- `--queryguard-update-baseline` with `--collect-only` emptied the baseline file.
  It now leaves the file alone and says why.
- `session.refresh()` of an object whose collection is mapped `lazy="selectin"`
  or `lazy="subquery"` (also reached through a `lazy="joined"` relationship)
  was reported as a relationship-load N+1 with a `selectinload` hint for the
  strategy that is already mapped; two creates followed by a refresh each were
  enough to fail a test, on sync and async sessions. The loads SQLAlchemy runs
  for a refresh or an expired reload are no longer counted as lazy loads. A
  `lazy="select"` collection read per parent and an `immediateload()` option
  are reported as before.
- The refresh a repository runs after each create or update (advanced-alchemy's
  `auto_refresh`) was a column-load finding as soon as a test created five
  rows. A `session.refresh()` of an object that the latest flush of its
  Session inserted or changed is no longer counted (one object per class and
  flush); a write loop itself stays the opt-in write-loop rule's finding. A
  loop of refreshes of objects that were only read or flushed without a net
  change, a refresh of every row of one bulk insert and a second refresh of
  the same write are reported as before.
  On litestar-fullstack this removes the column-load finding of two tests that
  create ten tokens each; their repeated-statement findings remain.
- `session.refresh(obj, ["name"])` in a loop was labelled as an expired load
  with an `expire_on_commit=False` hint; it is now a refresh (`Book (refresh)`)
  with the refresh hint.
- In `warn` mode, a finding turned into an error by `filterwarnings`
  (`error::pytest_queryguard.QueryGuardWarning` or a blanket `error`) no longer
  prints about 50 lines of the plugin's hook wrapper source before the finding;
  the failure shows the finding message, as in `fail` mode.
- Under pytest-xdist, a collection error without `-x` no longer rewrites the
  baseline or writes the `--queryguard-sarif` file: the run is handled as the
  interrupted run a serial run is, with the same reason. Closes #194.
- An internal error in the budget markers, the `allow_missing_index` check or
  the end of a measurement in the `pytest_runtest_call` hook no longer fails
  the user's test: it ends measurement with a `QueryGuardInternalWarning` and
  the "queryguard: measurement stopped" section, as other internal errors do.
  The collection-report and pytest-xdist worker hooks are guarded the same
  way. A `KeyboardInterrupt` or `SystemExit` at the end of a measurement still
  stops the run, and leaves no collector or eager-load tracker behind.
  Closes #197.
- Under pytest-xdist, a run stopped by `-x` or `--maxfail` is handled as in a
  serial run instead of as an interrupted one: the baseline is not updated
  because the run stopped early (it no longer blames a collection error,
  Ctrl-C or an internal error), and the `--queryguard-sarif` file is written.
  A collection error under `-x`, serial or with pytest-xdist, is still
  reported as an interrupted run. Closes #187.
- The GitHub Action example in `docs/ci.md` no longer fails on a first run:
  the `baseline` input is commented out until the file exists. The examples
  use `actions/checkout@v7` and `actions/setup-python@v7` (Node 24) and run
  on `push` to `main` plus `pull_request`, so a pull request does not run
  twice.
- A table name in a missing-index finding is now matched the same way in
  `queryguard_index_ignore_tables`, `allow_missing_index`, `queryguard_ignore`
  and `allow_nplusone(..., kind="missing-index")`: a bare name, `schema.table`
  (also for a table the report prints without its schema), quoted or unquoted
  identifiers (unquoted as written, case-sensitive; quoted exactly, may hold
  dots). The documented
  `allow_nplusone("public.audit_log", kind="missing-index")` now matches.
  Every spelling that matched before still does, and the labels in reports and
  baselines are unchanged. This can leave out missing-index findings that were
  reported before: in `queryguard_ignore` and `allow_nplusone`, a bare name
  (`audit_log`) now also matches the table in a schema off the search path
  (label `audit.audit_log`), `schema.table` matches a table printed without
  its schema, and an unquoted name matches a table whose label is quoted
  (`UserAccount` for `"UserAccount"`). Findings of other kinds match as
  before.
- `docs/sarif.md` and `docs/baseline.md` listed five finding kinds; the SARIF
  file lists six rules, `unused-eager-load` included.
- The budget failure says `expected at most 1 query` (it said `1 queries`);
  other limits are unchanged.
- Documentation: the recipes, `python-api.md` and the migration guide now run as
  written (found in a walk of the 0.4.0rc3 docs); `tests/test_doc_examples.py`
  runs more of the recipe pages as projects.

## [0.4.0rc4] - 2026-10-13

### Fixed

- `session.refresh()` of an object whose collection is mapped `lazy="selectin"`
  or `lazy="subquery"` (also reached through a `lazy="joined"` relationship)
  was reported as a relationship-load N+1 with a `selectinload` hint for the
  strategy that is already mapped; two creates followed by a refresh each were
  enough to fail a test, on sync and async sessions. The loads SQLAlchemy runs
  for a refresh or an expired reload are no longer counted as lazy loads. A
  `lazy="select"` collection read per parent and an `immediateload()` option
  are reported as before.
- The refresh a repository runs after each create or update (advanced-alchemy's
  `auto_refresh`) was a column-load finding as soon as a test created five
  rows. A `session.refresh()` of an object that the latest flush of its
  Session inserted or changed is no longer counted (one object per class and
  flush); a write loop itself stays the opt-in write-loop rule's finding. A
  loop of refreshes of objects that were only read or flushed without a net
  change, a refresh of every row of one bulk insert and a second refresh of
  the same write are reported as before.
  On litestar-fullstack this removes the column-load finding of two tests that
  create ten tokens each; their repeated-statement findings remain.
- `session.refresh(obj, ["name"])` in a loop was labelled as an expired load
  with an `expire_on_commit=False` hint; it is now a refresh (`Book (refresh)`)
  with the refresh hint.
- In `warn` mode, a finding turned into an error by `filterwarnings`
  (`error::pytest_queryguard.QueryGuardWarning` or a blanket `error`) no longer
  prints about 50 lines of the plugin's hook wrapper source before the finding;
  the failure shows the finding message, as in `fail` mode.
- Under pytest-xdist, a collection error without `-x` no longer rewrites the
  baseline or writes the `--queryguard-sarif` file: the run is handled as the
  interrupted run a serial run is, with the same reason. Closes #194.
- **Behavior change:** a serial run with `--continue-on-collection-errors`
  and a collection error also no longer rewrites the baseline or writes the
  `--queryguard-sarif` file, where it did before. The tests of the module that
  did not collect never ran: a rewritten baseline would drop their entries and
  an uploaded report would close their alerts. The summary gives the
  interrupted-run reason; the GitHub Action skips the upload with a warning.
- An internal error in the budget markers, the `allow_missing_index` check or
  the end of a measurement in the `pytest_runtest_call` hook no longer fails
  the user's test: it ends measurement with a `QueryGuardInternalWarning` and
  the "queryguard: measurement stopped" section, as other internal errors do.
  The collection-report and pytest-xdist worker hooks are guarded the same
  way. A `KeyboardInterrupt` or `SystemExit` at the end of a measurement still
  stops the run, and leaves no collector or eager-load tracker behind.
  Closes #197.
- Under pytest-xdist, a run stopped by `-x` or `--maxfail` is handled as in a
  serial run instead of as an interrupted one: the baseline is not updated
  because the run stopped early (it no longer blames a collection error,
  Ctrl-C or an internal error), and the `--queryguard-sarif` file is written.
  A collection error under `-x`, serial or with pytest-xdist, is still
  reported as an interrupted run. Closes #187.
- The GitHub Action example in `docs/ci.md` no longer fails on a first run:
  the `baseline` input is commented out until the file exists. The examples
  use `actions/checkout@v7` and `actions/setup-python@v7` (Node 24) and run
  on `push` to `main` plus `pull_request`, so a pull request does not run
  twice.
- A table name in a missing-index finding is now matched the same way in
  `queryguard_index_ignore_tables`, `allow_missing_index`, `queryguard_ignore`
  and `allow_nplusone(..., kind="missing-index")`: a bare name, `schema.table`
  (also for a table the report prints without its schema), quoted or unquoted
  identifiers (unquoted as written, case-sensitive; quoted exactly, may hold
  dots). The documented
  `allow_nplusone("public.audit_log", kind="missing-index")` now matches.
  Every spelling that matched before still does, and the labels in reports and
  baselines are unchanged. This can leave out missing-index findings that were
  reported before: in `queryguard_ignore` and `allow_nplusone`, a bare name
  (`audit_log`) now also matches the table in a schema off the search path
  (label `audit.audit_log`), `schema.table` matches a table printed without
  its schema, and an unquoted name matches a table whose label is quoted
  (`UserAccount` for `"UserAccount"`). Findings of other kinds match as
  before.
- `docs/sarif.md` and `docs/baseline.md` listed five finding kinds; the SARIF
  file lists six rules, `unused-eager-load` included.
- The budget failure says `expected at most 1 query` (it said `1 queries`);
  other limits are unchanged.
- Documentation found in a walk of the 0.4.0rc3 docs, each snippet run in a
  fresh environment: the Flask-SQLAlchemy, SQLModel, Litestar and
  pytest-asyncio recipes show `pytest.ini` with `pythonpath = .` (copied
  literally, they failed with `No module named 'app'`) and the models and test
  files they were missing; `python-api.md` and `migrating-from-queryspy.md`
  import the fixed modules where they expect the fixed behaviour;
  `python-api.md` documents `column_load_threshold`, `small_repeat_threshold`
  and `write_loop_threshold` of `assert_no_nplusone()`; the legacy recipe puts
  its settings in the example's `pytest.ini`, which pytest reads instead of a
  `pyproject.toml`; the JUnit XML of the CI recipe, a line number in the
  budgets recipe, a test file name in the FastAPI recipe, a missing file on the
  getting-started page and the stale `0.4.0rc2` install lines are corrected.
  `tests/test_doc_examples.py` now runs the SQLModel, Litestar and
  pytest-asyncio pages as projects too (skipped where the package is not
  installed).

## [0.4.0rc3] - 2026-10-13

### Added

- `QueryGuardDeprecationWarning`, a `DeprecationWarning` subclass exported from
  `pytest_queryguard`, is the class the deprecation policy uses. It is not a
  `QueryGuardWarning`, so `filterwarnings = ["error::pytest_queryguard.QueryGuardWarning"]`
  (and `ignore::` for findings) neither turns a deprecation into a failure nor
  hides it. `docs/stability.md` names it in step 1 of the policy. Nothing is
  deprecated yet.

### Changed

- The repeated-statement rules no longer report pagination and chunked `IN`
  loads when the bound parameters show them: only the `OFFSET` or an integer
  keyset bound changes, in one direction, by at least a page of more than one
  row (the walk may be repeated once); or one `IN` list of at least 50 values
  changes, the lists share no value and all but one have the same length.
  `LIMIT 1 OFFSET n` walks, a page per item, a keyset bound per row, string
  and date keyset bounds, per-item `IN` lists, a paginated lookup inside a
  loop and identical repeats are still reported. Closes #60.
  The accuracy corpus has five more CLEAN scenarios for these shapes and five
  more BUG scenarios that imitate them; on its 141 scenarios the default
  repeated-statement precision goes from 80% to 97% (90% to 97% without the
  five CLEAN ones), recall is unchanged at 95%, no BUG scenario changes in any
  configuration, and the release gate's minimum precision for the kind is now
  90%. See "Pagination and chunks" in `docs/how-detection-works.md`.

- `docs/stability.md` has an "Experimental" section: the
  `queryguard_unused_eager_load` option, the `unused-eager-load` finding kind
  and the missing-index check on MySQL and MariaDB are exempt from the
  deprecation policy and may change or go in a minor release with a changelog
  entry. The policy had no such tier although the option was labelled
  experimental.

- `max_queries()` and `max_db_time()` now yield the public `QueryLog` (the
  object `record_queries()` yields) instead of the internal `QueryCollector`,
  and are annotated `AbstractContextManager[QueryLog]`. `with max_queries(3) as log:`
  gives `log.count`, `log.queries`, `log.slowest()` and `log.assert_no_nplusone()`.
  Code that read `.statements` of the yielded object, an undocumented
  attribute, must use `log.queries` or `log.count`.

- The declared minimum SQLAlchemy is now 2.0.13 (`sqlalchemy>=2.0.13`). The
  suite and the async helpers need `AsyncAttrs`, which does not exist in
  earlier 2.0 releases, so the old `>=2.0` bound promised more than was tested.

- The options with a fixed set of values now agree on case. `queryguard_mode`,
  `queryguard_hypopg` and `--queryguard-mode` used to reject `FAIL`, `ON` and
  `Warn` while `queryguard_unused_eager_load` and `queryguard_repeat_scope`
  accepted any case; all of them now ignore case and surrounding spaces, and
  unknown values are still a usage error.

- Lower the per-statement overhead by about a sixth in the in-memory SQLite
  micro-benchmark: one shared `CallSite` per source location, one write check
  per statement and a cache for `normalize()`. Behaviour is unchanged. New
  `docs/performance.md` and `scripts/bench_suite.py` (a whole-suite benchmark
  for file-based SQLite or PostgreSQL).

- `allow_nplusone` with patterns now also applies to missing-index findings.
  `kind="missing-index"` was accepted but never matched anything and was then
  reported as a stale ignore; it now works like the same pattern in
  `queryguard_ignore`. The marker without arguments still leaves the index
  check alone (`allow_missing_index` skips it).

- The accuracy corpus has 23 more scenarios (133 in all): GraphQL-style
  resolvers and DataLoaders, `asyncio.gather` and Celery-style fan-out,
  caching repositories, `with_for_update`, nested transactions, `yield_per`,
  large `selectinload`, ORM bulk statements, upserts, and BUG scenarios that
  look like pagination, batching or polling. `docs/accuracy.md` and the release
  gate's minimum scenario count are updated; the plugin is unchanged.

- Tests and CI: a release gate (`.github/workflows/release-gate.yml`,
  `scripts/release_gate.py`) that checks the test matrix, the accuracy
  minimums, the documentation examples, clean installs, the upgrade from the
  previous release, the overhead budget, the package contents and the
  changelog before a release is tagged. See `docs/releasing.md`.

- PostgreSQL index check: a statement on a partitioned table is now one
  finding on the partitioned table, with `CREATE INDEX ON <table> (...)` (which
  creates the index on every partition) and its hypopg verification, instead of
  one finding and one `CREATE INDEX` per partition
  ([#86](https://github.com/MaciejZiel/pytest-queryguard/issues/86)). An
  ignore or baseline entry that named a partition no longer matches: replace
  the partition's name with the partitioned table's in
  `queryguard_index_ignore_tables`, `allow_missing_index` and `missing-index`
  patterns, and record the baseline again with `--queryguard-update-baseline`
  (see `docs/index-check.md`). A statement that names a partition itself is
  reported under the partitioned table too; inheritance children
  (`INHERITS`) are still reported one by one. Each partition is looked up once
  per session (`pg_partition_root()`), only for statements with findings. The
  advisor's reason for an invalid existing index now also names
  `CREATE INDEX ON ONLY` a partitioned table whose partitions are not all
  attached.

- The classes and functions in `pytest_queryguard.__all__` now report
  `pytest_queryguard` as their `__module__`. Tracebacks and logs show
  `pytest_queryguard.NPlusOneDetected` instead of
  `pytest_queryguard.api.NPlusOneDetected`, so the printed name matches the
  documented import path and the submodules can move without changing it. A
  `filterwarnings` entry that names the submodule
  (`error::pytest_queryguard.warnings.QueryGuardWarning`) keeps working.

- The keyword of the `max_queries` marker is now `limit`, as in the
  `max_queries(limit)` function: `@pytest.mark.max_queries(limit=3)`. The
  keyword `n=` of earlier releases is still accepted, without a warning. It
  may be removed in a future minor release, but only after a deprecation
  period with a warning, as described in `docs/stability.md`. The positional
  form is unchanged.

- Every finding in a report now has a `kind: ...  label: ...` line, the pair that
  `queryguard_ignore`, `allow_nplusone` and the baseline match on, for example
  `kind: relationship-load  label: Order.customer`. For a repeated statement or
  a write loop the line replaces the separate line with the normalized
  statement (a statement longer than 200 characters is cut and ends with `*`,
  so the printed label still works as a pattern). An ignore pattern that is
  the exact label now matches even when the label holds `[` or `]`. The
  line is in the SARIF message too
  ([#105](https://github.com/MaciejZiel/pytest-queryguard/issues/105)).

### Fixed

- On SQLAlchemy 2.0.20 to 2.0.49 (and 2.1.0b1, 2.1.0b2) the plugin's
  `do_orm_execute` listener triggered SQLAlchemy issue 13301 (fixed in 2.0.50):
  the outer statement's `yield_per`, `autoflush`, `populate_existing` and
  `identity_token` options became load options of its `selectinload` and
  `immediateload` statements. A `yield_per` plus `selectinload` test failed
  with "Can't use the ORM yield_per feature in conjunction with unique()", and
  `autoflush=False` stopped the loader statement from flushing, only because
  the plugin was installed. On those versions the plugin now removes the
  inherited options from a loader statement before SQLAlchemy reads them, so
  the statement runs as it does without the plugin. When a `do_orm_execute`
  listener of your own takes part, the plugin changes nothing: your listener
  triggers the same issue without the plugin
  ([#169](https://github.com/MaciejZiel/pytest-queryguard/issues/169)).

- Tests: the accuracy corpus now checks that a scenario never fails without a
  finding in every run, not only in the pytest-xdist worker that happened to
  measure it.

- On SQLAlchemy 2.0.13 to 2.0.29 the column load of a joined-inheritance
  subclass (reading `manager.budget` after `select(Employee)`) was not
  recognised as a column load and was reported as a repeated statement. Before
  2.0.30 SQLAlchemy reports `is_select` and `is_column_load` as False for the
  `from_statement()` execution that loads those columns; the plugin now also
  reads the refresh state from the statement and the execution options. The
  oldest-versions job of the release gate now installs exactly
  `sqlalchemy==2.0.13`, the declared minimum, instead of the latest 2.0.x.
  Fixes #161.

- Free-threaded CPython (3.14t): statements, relationship and column loads and
  database time reported from several threads into one collector lost updates
  (`counted 15839, expected 16000`), which budgets and the truncation flag rely
  on. The collector now updates them under a lock on a build without the GIL
  and takes no lock with the GIL (`scripts/bench_overhead.py` shows no change
  on CPython 3.12). Tests: the memory test measures what the plugin itself
  allocated, because the whole-process figure on the free-threaded build
  reflects SQLAlchemy's late freeing, not a leak in the plugin. Free-threaded
  builds are still not in the CI matrix; `docs/getting-started.md` says so.
  Fixes #159.

- The repeat rules (repeated statement and small repeat) no longer count
  transaction-control statements: `BEGIN`, `COMMIT`, `ROLLBACK`, `SAVEPOINT`,
  `RELEASE`, `START TRANSACTION` and `SET TRANSACTION`. With SQLAlchemy's
  pysqlite `BEGIN` recipe a loop that commits each pass was reported with
  `BEGIN` as a repeated statement and the advice to fetch rows in one query.
  Budgets still count them. A `BEGIN` that opens a block
  (`BEGIN load_order(:id); END;`) is still counted, and a loop with a
  `SAVEPOINT` per row is still a write loop. The accuracy corpus gives the
  same result before and after. Fixes #158.

- Stability: with `-W error` or `filterwarnings = error`, an internal error
  no longer fails the user's test. `QueryGuardInternalWarning` was raised as
  an exception from the test call, from the terminal summary (which aborted
  it), from a `record_queries()` block and, for the unused eager-load check,
  from inside SQLAlchemy's `do_orm_execute`. Now it is always shown and
  recorded, never raised, and an "ignore" filter still hides it (#124).
- Stability: an error inside the `after_begin` listener no longer reaches the
  user's `Session.begin()`. Like every other listener, it now stops the
  measurement with a `QueryGuardInternalWarning` (#123).
- Stability: a baseline file nested too deep for the JSON parser is a usage
  error instead of an `INTERNALERROR`, and any error while writing the
  baseline is reported instead of crashing the end of the session (#125).
- PostgreSQL index check: an internal error after the check opened its
  savepoint (or, on an autocommit connection, its own transaction) left that
  savepoint or transaction open on the user's connection. In autocommit mode
  the user's later writes were then never committed, and inside a transaction
  `enable_seqscan` stayed `off`. The check now rolls them back before the
  error stops the plugin (#126).
- Stability: an error inside `pytest_configure`, the per-test start or end
  of the measurement, the pytest-xdist hand-over of the findings,
  `pytest_unconfigure` or the attribute-read hook of the unused eager-load
  check no longer ends the session with an `INTERNALERROR` or fails every
  test. It stops the measurement and is reported once, like an error in a
  listener; an error in the xdist hand-over marks the run incomplete, so the
  baseline is not rewritten and the SARIF summary says findings may be
  missing. `KeyboardInterrupt`, `SystemExit` and `asyncio.CancelledError` are
  still never swallowed. `docs/stability.md` describes the contract under
  "Failing open" (#127).

- Tests: the accuracy corpus now runs on PostgreSQL
  (`QUERYGUARD_CORPUS_PG_URL`). Two scenarios (`bug_23_get_or_create_loop`,
  `bug_30_flush_per_insert`) failed there because seeded rows with explicit ids
  did not advance the sequences; the corpus moves them after each setup. The
  results on PostgreSQL 17 match SQLite; see `docs/accuracy.md`. Fixes #120.

- SARIF: a finding whose call site is unknown (for example `session.execute`
  handed straight to a worker thread) is located at the test instead of being
  left out of the file, so code scanning shows it. The terminal text, the
  baseline and the ignore rules are unchanged. `docs/limitations.md` explains
  the cause and the workaround
  ([#132](https://github.com/MaciejZiel/pytest-queryguard/issues/132)).

- GitHub Action: the `package` input now defaults to the plugin in the action's
  own checkout instead of the name `pytest-queryguard`, so the installed plugin
  matches the pinned ref and accepts `--queryguard-sarif` (pip used to pick the
  stable 0.3.0 while 0.4 was a release candidate)
  ([#106](https://github.com/MaciejZiel/pytest-queryguard/issues/106)).

- The index check notice for a database it cannot check (SQL Server, Oracle)
  said the check needs PostgreSQL 16, although SQLite, MySQL and MariaDB are
  supported. It now lists every supported database, and the help texts of
  `--queryguard-index-check` and `queryguard_index_check` name MySQL and
  MariaDB too. Fixes #154.

- PostgreSQL index check: a finding for an index the plan walks end to end (a
  filter on the trailing column of a composite key, a whole index with a filter,
  a whole-index bitmap) now says `t scans the whole index ix: the ... does not
  match the start of any index`, as the SQLite check does, instead of saying the
  table is read in full. The closing note no longer calls such a scan sequential
  ([#130](https://github.com/MaciejZiel/pytest-queryguard/issues/130)).

- A lazy load keeps its relationship when another statement runs between the
  load and its `SELECT`: statements of other `AsyncSession`s running
  concurrently (`asyncio.gather`), the `BEGIN` of SQLAlchemy's pysqlite recipe
  or any statement of a `begin` or `after_begin` listener. The attribution is
  now parked on the `Session` that issued the load instead of on the thread, so
  these loops are no longer reported a second time as a repeated statement.
  Fixes #97 and #99.

- The relationship-load finding shows a lazy load as its sample SQL. It used the
  first statement with the relationship's label, which was the `selectinload`
  query when that ran first; that query is now used only when no lazy load was
  recorded ([#131](https://github.com/MaciejZiel/pytest-queryguard/issues/131)).

- The SARIF level of an unused-eager-load finding now follows the test outcome:
  with `queryguard_unused_eager_load = warn` it is `warning`, not `error`.
  Fixes #98.

- A statement that starts with a comment (sqlcommenter tags, a `-- note` in a
  `text()`) is now classified like the statement after the comment. A loop of
  commented single-row `INSERT`s was reported as a repeated statement with the
  advice to fetch the rows in one query, and the write-loop rule never saw it.
  Fixes #96.

- The MySQL and MariaDB index check no longer fails on a literal `%` in a
  statement that was run with the `no_parameters` execution option: the
  `EXPLAIN` was formatted by PyMySQL (`query % args`) although the statement
  itself was not, so it was reported as "could not be explained" (a literal
  `%%` was sent as `%`). Fixes #116.

- `--queryguard-update-baseline` no longer rewrites the baseline, and
  `--queryguard-sarif` no longer writes the file, after an interrupted run (a
  collection error, Ctrl-C, a usage error or an internal pytest error). The
  summary says why. Fixes #95.

- Tests: the public API snapshot recorded a wrong return type for
  `QueryLog.slowest()` and did not assert it. It now records
  `tuple[Query, ...]` and checks every member of `QueryLog` the same way.

- `--queryguard-update-baseline` writes a usable baseline when a test lives in
  a directory whose name is not valid UTF-8: such characters are written as
  `\udcXX` escapes (other text stays readable), so the entry matches on the
  next run instead of the write failing. Fixes #89.

- PostgreSQL index check: `NOT IN (subquery)` on PostgreSQL 17 and 18 no
  longer gets `fix: CREATE INDEX ON t (col)` for the compared column, which was
  wrong even for a primary key
  ([#83](https://github.com/MaciejZiel/pytest-queryguard/issues/83)). Plan
  conditions with `CASE`, `AT TIME ZONE`, `EXTRACT(... FROM ...)`, `count(*)`,
  `COLLATE` or an array subscript are now parsed instead of matched as text, so a
  column inside them is no longer suggested as a plain index and gets the
  expression-index note
  ([#84](https://github.com/MaciejZiel/pytest-queryguard/issues/84)). A row
  comparison such as `(a, b) > ($1, $2)` no longer asks for an expression index
  ([#85](https://github.com/MaciejZiel/pytest-queryguard/issues/85)).

- PostgreSQL index check: a server newer than the tested releases (16 to 18)
  is still checked and now gets one notice in the session summary that its
  version is untested. Older servers keep their one "not checked" notice. The
  tested versions are listed in `docs/index-check.md`.
- The index check no longer rolls back a transaction the application opened by
  hand on an AUTOCOMMIT connection (`exec_driver_sql("BEGIN")`, with pg8000 or
  asyncpg). The `autocommit` flag is the driver's setting, not the server's
  state, so the check now tries a savepoint first and starts a transaction of
  its own only when none is open (SQLSTATE 25P01). Fixes #91.
- Documentation: the index check page was published as raw Markdown from the
  partial-index paragraph on, including its SQLite section
  ([#117](https://github.com/MaciejZiel/pytest-queryguard/issues/117)). A test
  now fails the build for any page left like that.
- Documented that PostgreSQL evaluates `IMMUTABLE` functions with constant
  arguments at plan time, so the index check and the advisor can call such a
  function again and its non-transactional side effects (for example
  `nextval()`) are not rolled back with the savepoint. Fixes #92.
- The repeated-statement and small-repeat rules no longer count schema
  reflection: `PRAGMA` statements, reads of `sqlite_master`, `sqlite_schema` and
  their temp variants, and reads of `pg_catalog` or `information_schema`
  (`MetaData.reflect()`, `Inspector.has_table()`, `create_all()`, Alembic's
  version-table check). `max_queries` budgets still count them. See
  `docs/how-detection-works.md` for the exact rule
  ([#129](https://github.com/MaciejZiel/pytest-queryguard/issues/129)).
- PostgreSQL index check: a sequential scan whose filter is only not-equal
  comparisons against parameters (`<> $1`, `NOT IN` with bound values) is no
  longer reported as a missing index. No index serves them, so the advice
  could not be followed, even on an indexed column; the SQLite check never
  reported them. A filter with one more servable condition, or a not-equal
  against a constant in the SQL (which a partial index serves), is still
  reported ([#133](https://github.com/MaciejZiel/pytest-queryguard/issues/133)).

- PostgreSQL index check: a server newer than the tested releases (16 to 18)
  is still checked and now gets one notice in the session summary that its
  version is untested. Older servers keep their one "not checked" notice. The
  tested versions are listed in `docs/index-check.md`.
- Documentation: the index check page was published as raw Markdown from the
  partial-index paragraph on, including its SQLite section
  ([#117](https://github.com/MaciejZiel/pytest-queryguard/issues/117)). A test
  now fails the build for any page left like that.
- Baseline: a file nested more than 100 levels deep is now rejected by a
  depth check before parsing, so the "nested too deep" usage error no longer
  depends on the interpreter hitting its recursion limit (which on Python 3.10
  could surface as a second, unraisable `RecursionError`).

- `--queryguard-update-baseline` on a partial run (`-k`, `-m`, `--deselect`,
  `--lf`, `--sw`, `--ignore`, a node id or a path narrower than `testpaths`, with
  or without pytest-xdist) no longer drops the entries of tests that did not
  run. Only the entries of tests that ran are replaced, the others are kept, and
  the summary says how many were kept. A deleted test's entries are removed on
  the next full run. `docs/baseline.md` describes which runs are full
  and which are partial. Fixes #148.

- `--queryguard-update-baseline` with `--collect-only` emptied the baseline file.
  It now leaves the file alone and says why.

## [0.4.0rc2] - 2026-10-12

### Added

- Community files: `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`
  (Contributor Covenant 2.1), issue forms and a pull request template.
- The documentation site has a description, canonical URL and Open Graph tags
  on every page, a favicon, `sitemap.xml`, `robots.txt`, a social preview
  image, a client-side search (no third-party code), "Edit this page" links
  and Previous/Next links. Every page needs a `description` in
  `docs/site.toml`; the strict build fails without one.
- A "Recipes" section in the documentation: FastAPI (`TestClient` and
  `httpx.AsyncClient` with dependency-overridden sessions), Flask-SQLAlchemy,
  SQLModel, Litestar, pytest-asyncio configuration pitfalls, budgets for hot
  endpoints, adopting the plugin on a legacy codebase and CI. A new example,
  `examples/fastapi-dependency-override`, is run by the test suite.
- Missing-index check for MySQL and MariaDB, behind the same switch
  (`queryguard_index_check`, `--queryguard-index-check`). Each distinct
  `SELECT`, `UPDATE` and `DELETE` is explained with `EXPLAIN` and the real bound
  parameters (never `ANALYZE`); a table read in full (`type: ALL`, or an index
  read in full on a column that leads no index) although the statement filters
  or joins on its columns is a `missing-index` finding with a `CREATE INDEX`
  hint and an Alembic call. The suggestion is not verified: neither server has
  hypothetical indexes. Tested with PyMySQL and aiomysql on MySQL 8.4 and
  MariaDB 11.8 on a developer machine; CI does not run a MySQL or MariaDB
  server. See `docs/index-check-mysql.md`.

### Changed

- Tests: an accuracy corpus under `tests/corpus`: 110 small scenarios of realistic ORM
  code (views, serializers, FastAPI, Flask-SQLAlchemy, SQLModel, repositories,
  pagination, bulk writes, async), each marked BUG or CLEAN, run through the
  plugin in its default configuration and with each opt-in rule. Limitations it
  found are in `docs/limitations.md`; `scripts/accuracy_report.py` prints the
  precision and recall.

### Fixed

- A lazy load or column load that autoflushed pending changes first was
  attributed to the flush's write: the UPDATE or DELETE carried the relationship
  name and the load's SELECT carried none, so one loop was also reported as a
  repeated statement. Writes no longer take the pending attribution (#59).
- Documentation: the limitations page, the FAQ and the README now say that a
  raw `sqlite3` or other DB-API connection and non-SQLAlchemy ORMs (Django ORM,
  Peewee, Tortoise ORM) are not seen, and that nothing reports it; the
  limitations page also points to the repeat-scope limits.
- The SQLite index check no longer says a table "is read in full" when the
  plan walks an existing index that the filter cannot use (`SCAN INDEX ix_...`).
  It says the table scans the whole index and that the filter does not match
  the start of any index; the suggested `CREATE INDEX` is unchanged.
- A SARIF result merged from several tests no longer shows one test's count in
  `message.text` next to the total in `properties.count`. The message now
  starts with `Seen in N tests: T times in total, up to M times in one test`,
  and `properties` gains `tests` and `peakCount` for such results. `count`,
  the fingerprints and the baseline identity are unchanged. See
  `docs/sarif.md`.
- The `repeated-statement` hint no longer says to fetch the rows with
  `WHERE id IN (...)` when the statement already uses `IN (...)`. It says the
  batched query ran several times and should be called once, or the calls
  combined.
- Added `queryguard_repeat_scope` (`test` or `session`, default `test`) and
  `QueryLog.assert_no_nplusone(repeat_scope=...)` for false positives in tests
  that make several requests. The repeat, relationship,
  column-load and write-loop rules counted over the whole test, so a test that
  made several independent requests (a test client, for example, with one
  `Session` per request) reported the same authentication or organisation
  lookup as a loop. With `session`, each `Session`, or each connection checkout
  for statements no `Session` ran, is counted on its own. The default is
  unchanged, but a finding whose count spans several Sessions or checkouts now
  says so and names the option. See `docs/several-sessions.md`.
- SARIF locations are now relative to the repository root when
  `GITHUB_WORKSPACE` is set and the pytest rootdir is inside it. Before, they
  were relative to the rootdir, so with the action's `working-directory` set to
  a subdirectory GitHub created alerts on paths that do not exist and showed no
  pull request annotations. Fingerprints and baseline identities are
  unchanged. See `docs/sarif.md`.
- The action skips the SARIF upload for runs triggered by Dependabot (read-only
  token), and a refused upload no longer fails the job.

## [0.4.0rc1] - 2026-10-12

Release candidate: install it with `pip install --pre pytest-queryguard`.

This release adds baselines, SARIF output and a GitHub Action, per-finding
ignores, new finding kinds (column loads, write loops, unused eager loads), query
timing and `max_db_time`, an index advisor, and a missing-index check for SQLite.
It also adds a documentation site and runnable examples. Three things can change the
result or the output of an existing test run without any new option: the `column-load`
kind is on by default, the index advisor is on by default with the index check, and
the index check now also runs on SQLite. They are listed first under "Changed" as
behavior changes, with how to turn them off, as the pre-1.0 clause of the
[stability policy](docs/stability.md) requires.

### Added

**Missing indexes**

- Missing-index check for SQLite, behind the same switch as PostgreSQL
  (`queryguard_index_check`, `--queryguard-index-check`). Each distinct
  `SELECT`, `WITH`, `UPDATE` and `DELETE` in a test body is explained with
  `EXPLAIN QUERY PLAN` and its real bound parameters; the statement is not run,
  so data and transaction are untouched. A scanned table that the statement
  filters or joins on, or an automatic index, is a `missing-index` finding with
  a `CREATE INDEX ix_<table>_<columns> ON <table> (<columns>)` hint. `LIKE`,
  views, `ORDER BY` without a filter, primary keys and unrecognized plan shapes
  are left alone. Works with `sqlite3`, aiosqlite, SQLAlchemy ORM and Core, and
  pytest-xdist; findings honor `queryguard_ignore`, `allow_missing_index`,
  `queryguard_index_ignore_tables`, the baseline and SARIF. See
  `docs/index-check-sqlite.md`. `scripts/bench_overhead.py --index-check` times it.
- Index advisor. Each missing-index finding now suggests an index: a
  `CREATE INDEX` built from the equality, range and `ORDER BY` columns of the
  plan node, a partial index when a constant predicate is selective, and the
  Alembic `op.create_index` call. When the hypopg extension is installed, the
  suggestion is verified inside the check's savepoint: whether the planner
  would use the index and the statement's estimated cost before and after;
  hypothetical indexes are reset, also after errors, on sync drivers and
  asyncpg. No new index is suggested when the table already has one for these
  columns (invalid, partial, or valid but unused), and the finding says why.
  Without hypopg the suggestion stays and one notice says how to install it.
  New options `queryguard_index_advisor` (default `true`, used with the index
  check) and `queryguard_hypopg` (`auto`, `on` or `off`). Findings carry the
  suggestion in an additive `advice` field. See `docs/index-advisor.md`.

**New findings and rules**

- Column-load detection, a new finding kind `column-load`. Instances that each
  load a deferred column, or whose attributes were expired by `commit()`,
  `expire()` or `expire_all()`, on their own are reported with the right
  advice: `undefer(Model.column)` (or removing `defer()`, `load_only()` or
  `deferred()`), or `expire_on_commit=False`; `session.refresh()` in a loop
  gets its own hint. The ini option `queryguard_column_load_threshold`
  (default 5 instances) and the `column_load_threshold` argument of
  `QueryLog.assert_no_nplusone()` set the limit. The kind works with
  `queryguard_ignore` and `allow_nplusone`.
- Write-loop detection, opt in: `queryguard_write_loop_threshold` (off by
  default) and the `write_loop_threshold` argument of
  `QueryLog.assert_no_nplusone()`. The same `INSERT`, `UPDATE` or `DELETE`
  shape run row by row from one source line is reported as the new finding kind
  `write-loop`, with a hint to use `session.add_all()`, `executemany` or a bulk
  statement. `executemany` and multi-row `VALUES` statements are not counted.
  The statements of one ORM flush count as one write, so `add_all()` and one
  `flush()` is not reported even where the dialect sends one `INSERT` per row;
  a `flush()` or `commit()` per row is. The flush is found through SQLAlchemy's
  `Session._flush` frame; if that changes, the rule counts every statement.
  The kind works with `queryguard_ignore`, `allow_nplusone` and
  `queryguard_fail_on`.
- Experimental detection of unused eager loads, off by default:
  `queryguard_unused_eager_load` (`off`, `warn` or `fail`). A relationship a
  test loads with `selectinload()`, `joinedload()`, `subqueryload()` or
  `immediateload()` and never reads is reported as the new finding kind
  `unused-eager-load` (label `Class.relationship`), with a hint to remove the
  option. While the option is `off` nothing is installed. Opt out with
  `allow_nplusone(kind="unused-eager-load")` or `queryguard_ignore`. See
  `docs/unused-eager-load.md` for how it works and its limits.
- `queryguard_small_repeat_threshold`, an ini option (off by default) and the
  `small_repeat_threshold` argument of `QueryLog.assert_no_nplusone()`: a
  statement that ran a few times (below `queryguard_repeat_threshold`) from one
  source line, each time with different parameters, is reported as a repeated
  statement. Identical parameters, runs from several lines, writes and
  pagination are left alone. The default threshold of 5 is unchanged, so a batch
  of two to four statements is not flagged unless this is turned on.

**Adopting it on an existing project**

- Baseline file for existing findings: `queryguard_baseline` (ini) or
  `--queryguard-baseline=PATH`, and `--queryguard-update-baseline` to write it.
  A finding whose identity (kind, label, file and function; rootdir-relative
  paths, no line numbers or counts) is in the file does not fail or warn, for
  every finding kind. New findings are reported as before, and entries that
  matched nothing in the run are listed as stale baseline entries. The file is
  JSON with a `version` field, sorted for clean diffs; a missing, corrupt or
  unknown-version file is a usage error. Works with pytest-xdist: workers apply
  the file, the controller merges what they saw and writes it on an update.
  See `docs/baseline.md`.
- SARIF output: `--queryguard-sarif=PATH` writes the findings of the run as
  SARIF 2.1.0 for GitHub code scanning (the `--queryguard-report` flag is
  unchanged). One rule per finding kind with a fix hint, one result per
  finding identity with a rootdir-relative location, `partialFingerprints`
  derived from the identity, and `error` or `warning` level following
  `queryguard_fail_on`. Ignored and baselined findings are left out. Works with
  pytest-xdist: workers send their findings to the controller, which writes the
  file once. See `docs/sarif.md`.
- A composite GitHub Action in `action/`: installs the plugin, runs pytest with
  `--queryguard-sarif`, uploads the file with `github/codeql-action/upload-sarif`
  (pinned by commit SHA) and fails the step when pytest fails. Inputs:
  `pytest-args`, `python-version`, `package`, `baseline`, `fail`,
  `working-directory`, `sarif-file`, `upload`, `category`. The upload is skipped
  for pull requests from forks. A CI job runs the action on
  `examples/action-sample`. See `docs/ci.md`.

**Ignoring and gating**

- `queryguard_ignore`, an ini option that leaves single findings out of the
  reports. One pattern per line, `KIND LABEL [file=GLOB] [function=GLOB]`,
  matched with `fnmatch`. The kinds are `relationship-load`,
  `repeated-statement`, `column-load`, `write-loop`, `unused-eager-load` and
  `missing-index`; `file` is relative to the rootdir.
- `allow_nplusone` takes label patterns and the keywords `kind`, `file` and
  `function` to ignore only the findings they match. Without arguments it
  still ignores the whole test.
- Ignore patterns that matched no finding in the run are listed in the
  terminal summary as stale ignores (not with pytest-xdist).
- `queryguard_fail_on`, an ini option with finding kinds (`fnmatch` patterns,
  one per line). In `fail` mode only these kinds fail a test; the other kinds
  are reported as `QueryGuardWarning`. Empty means every kind fails, as before.
  Budgets are not affected.

**Timing and budgets**

- Per-statement timing. `Query.duration` is the time in seconds the database
  and the driver took for a statement, measured around the cursor call;
  `QueryLog.db_time` is the total and `QueryLog.slowest(n)` the slowest
  statements.
- `max_db_time(seconds)`, a context manager, and the `max_db_time` marker:
  fail when the statements take longer than the limit in total. The failure
  lists the slowest statements. A `max_queries` failure now shows the total
  database time and its slowest statements as well.

**Documentation and examples**

- A documentation site built from `docs/` (`scripts/build_docs.py`, with the new
  `docs` dependency group: Markdown and Pygments): getting started, how detection
  works, a configuration reference that a test keeps in step with the options, FAQ,
  limitations, a comparison with other tools and a migration guide from nplusone.
  The README is now a short overview that links to it.
- `examples/` with runnable projects (sync N+1, async FastAPI, PostgreSQL and SQLite
  missing indexes, baseline and SARIF), each with a README and a smoke test in CI.

### Changed

**Behavior changes**

- The `column-load` finding kind is on by default (threshold 5, see
  `queryguard_column_load_threshold`). A test that loops over deferred columns, or
  over objects expired by `commit()`, passed before and now fails in `fail` mode.
  To keep such tests green while you fix them, use a baseline, `queryguard_fail_on`
  without `column-load` (it then only warns), or
  `queryguard_ignore = ["column-load *"]`.
- Those loops used to be reported as a `repeated-statement` with the wrong advice
  (`WHERE id IN (...)`); they are no longer counted by that rule, so a baseline or
  ignore entry for such a finding has to use the new kind.
- The index advisor is on by default whenever the index check is on
  (`queryguard_index_advisor = false` turns it off). Missing-index findings now carry
  a `fix: CREATE INDEX ...` line and an `alembic:` line, and without the hypopg
  extension one notice at the end of the session says how to install it
  (`queryguard_hypopg = off` silences it).
- `queryguard_index_check` now also checks SQLite. A project that turned the check
  on and ran its tests on SQLite used to get a "not checked" notice and now gets
  findings, which fail tests in `fail` mode.
- Durations below one millisecond are printed in microseconds (`37 us`) instead of
  `0.0 ms` in the report and in budget failures.
- The hint for a lazy-loaded many-to-one or one-to-one relationship is now
  `joinedload(...)`; collections still get `selectinload(...)`.
- A test stores at most 10000 statements and 10000 relationship loads for its
  reports (`queryguard_max_events`, `0` for no limit). Past the limit they are only
  counted.
- When a listener raises, the plugin still stops measuring so that it never breaks
  the suite, but it warns and prints a "queryguard: measurement stopped" section
  instead of staying silent (see Fixed).

Everything else is off by default and changes nothing until you turn it on:
`queryguard_small_repeat_threshold`, `queryguard_write_loop_threshold`,
`queryguard_unused_eager_load`, `queryguard_baseline`, `queryguard_fail_on` (empty:
every kind fails, as before) and `--queryguard-sarif`.

**Internals**

- The missing-index check reads the conditions of a plan node with a tokenizer
  and parser (`pytest_queryguard.predicates`) instead of regular expressions
  over the text. PostgreSQL's JSON plan has no structured form of a predicate,
  so the relation, alias, schema and index still come from the plan's fields and
  the condition text is parsed into a tree; the old text matching stays as the
  fallback for syntax the parser does not know. Findings do not change. The
  parsing is tested on real `EXPLAIN (GENERIC_PLAN, VERBOSE, FORMAT JSON)` plans
  captured from PostgreSQL 14 to 18 (`scripts/capture_plans.py`).
- The measurement registers one more SQLAlchemy listener (`after_cursor_execute`)
  to time statements. `scripts/bench_overhead.py` puts the added cost of all the
  measurement, not of this listener alone, at about 20 to 55 microseconds per
  statement on in-memory SQLite (roughly +30 to +40 %); against a real database
  round trip it is small. See the FAQ.
- Internal: detectors now hand their results to a stable `Finding` model
  (`kind`, `label`, `file`, `function`, `message`, `severity`) before the
  report is rendered. The messages and the public API are unchanged.
- The check that decides whether a stack frame belongs to SQLAlchemy, pytest or
  the standard library is now cached per file name. Measured with
  `scripts/bench_overhead.py`, this cuts the per-statement overhead of the
  measurement by about half.

### Fixed

- The index check no longer reports a missing index, and the advisor no longer
  recommends a redundant one, for a statement that an existing partial index
  may serve. The generic plan cannot see bound values, so it could not know
  that `status = $1` matches `... WHERE status = 'open'`. Such statements are
  skipped and counted in the session summary. A missing index on them is not
  found; see the limitations.
- Loops over deferred or expired columns were reported as a repeated statement
  with the wrong advice (`WHERE id IN (...)`). They are now a `column-load`
  finding and no longer count towards the repeated-statement rule.
- When a listener raises, the plugin still stops measuring so that it never
  breaks the suite, but it no longer does so silently. The warning names the
  listener or step that failed, and the end of the session prints a
  "queryguard: measurement stopped" section with the listener, the error and
  the line that raised it, once per session, with or without
  `--queryguard-report`.

## [0.3.0] - 2026-10-11

### Added

- The missing-index check now covers async engines on asyncpg. asyncpg
  prepares statements, and PostgreSQL would take the `$n` inside `EXPLAIN`
  as the `EXPLAIN`'s own parameters. So on asyncpg the `EXPLAIN` runs in a
  PL/pgSQL `DO` block on the test's own connection and savepoint, and the
  plan comes back through a transaction-local setting. Findings, notices
  and guarantees are those of sync engines. Other async drivers are still
  skipped with a notice. See `docs/design-0.3.md`.
- On a database without PL/pgSQL, the asyncpg index check now reports one
  notice for the session instead of one failed `EXPLAIN` per statement.
- `pytest_queryguard.record_queries()`, a context manager that records the
  SQL statements a block sends and yields a `QueryLog`. The log has the
  statements as `Query` objects (`sql`, `callsite`, `relationship`), their
  `count`, and `assert_no_nplusone()`, which applies the N+1 rules to the
  block and raises `NPlusOneDetected`. `Query`, `QueryLog`, `CallSite` and
  `NPlusOneDetected` are exported for type hints. It works in pytest and
  outside it, and changes nothing else. A `QueryLog` is also sized, iterable and
  indexable (`len()`, `for`, `log[i]`, `log[a:b]`) but not a
  `collections.abc.Sequence`.
- `QueryGuardInternalWarning` (exported since 0.2.0) is now documented. A
  `record_queries()` block that ends after an internal error (including one
  recorded before the block, which had already stopped the recording) issues
  it, saying that the log may be incomplete, and
  `QueryLog.assert_no_nplusone()` raises `RuntimeError` instead of passing
  on such a log. This also applies outside pytest and with
  `--queryguard-mode=off`, where the plugin itself reports nothing.

### Changed

- The index check reads the first column of each btree index and the
  `varchar` columns of each table from the catalog once per engine for the
  whole session instead of once per test. Any statement that may change
  them (`CREATE`, `DROP`, `ALTER`, `REINDEX`, `TRUNCATE` and other DDL,
  seen in tests and fixtures alike) clears the cache, as does the end of a
  transaction that ran one. On 100 tests with 10 indexed statements each
  against PostgreSQL 17, the check went from about 2.3 ms to 1.2 ms per
  statement (#5). The same statement is explained again after such DDL, so
  a rolled-back index change no longer hides a finding. Clearing is safe
  while a plan is being read, also from another thread or when a
  connection is garbage collected, and when a two-phase transaction with
  DDL ends. A statement that cannot be planned is counted once in the
  "could not be explained" summary even when DDL made the check retry it.
- `QueryLog.assert_no_nplusone()` is documented as ignoring the
  `queryguard_*` ini options: a project with a custom
  `queryguard_repeat_threshold` passes `repeat_threshold` explicitly.
- Paths in the `NPlusOneDetected` message, and in the `QueryBudgetExceeded`
  message of a `with max_queries(n):` block, are relative to pytest's rootdir,
  as in the plugin's own failures, instead of the working directory (which is
  still the base outside pytest).

### Fixed

- The index check now judges temporary tables: trailing columns of their
  multicolumn indexes and `varchar` casts were silently skipped, because
  PostgreSQL names the schema `pg_temp` in plans and `pg_temp_<n>` in the
  catalog.
- The index check now reports a partial index read in full as a bitmap.
  With an index such as `(id) WHERE deleted_at IS NULL` and
  `WHERE deleted_at IS NULL AND email = ?`, the plan before `ANALYZE` was
  skipped, while the plan after it was reported (#4).

## [0.2.0] - 2026-10-11

### Added

- Missing-index check for PostgreSQL 16 and newer, off by default. With
  `--queryguard-index-check` or `queryguard_index_check = true`, each
  distinct `SELECT` in a test body is planned with
  `EXPLAIN (GENERIC_PLAN, VERBOSE, FORMAT JSON)` and `enable_seqscan` off,
  inside a savepoint on the statement's own connection. The test fails (or
  warns, in `warn` mode) when the plan reads a whole table to apply a
  filter, walks a whole index for an equality filter, or uses an index
  condition that leaves out a btree index's first column (PostgreSQL 16
  and 17). The message names the table, the condition, the SQL and the call
  site and, when the column is obvious, gives a `CREATE INDEX` hint.
- `--no-queryguard-index-check` to turn the check off for one run.
- `queryguard_index_ignore_tables` ini option and
  `@pytest.mark.allow_missing_index` marker to exclude small lookup tables
  or whole tests from the index check.
- A session summary for statements the index check could not check:
  other databases, PostgreSQL before 16, async engines, statements
  PostgreSQL could not plan, and a check stopped by a broken savepoint.
- CI job that runs the test suite against PostgreSQL 15, 16 and 17.

### Changed

- `IN` lists whose placeholders carry multi-word, schema-qualified or
  quoted type casts (`$1::DOUBLE PRECISION`, `$1::public.mood`) or use
  `CAST(? AS INTEGER)` now collapse to one form for N+1 detection.

## [0.1.0] - 2026-10-11

First release.

### Added

- Automatic N+1 detection for SQLAlchemy 2 in every test, with no
  configuration. Two rules: one relationship lazy-loaded for several parent
  objects, and one normalized `SELECT` repeated several times.
- Failure messages that point to the line of application code that caused
  the N+1, show the SQL involved and, for relationship loads, suggest a
  `selectinload` fix.
- `@pytest.mark.max_queries(n)` marker (also `max_queries(n=...)`) that fails
  a test running more than `n` SQL statements and lists every statement with
  its call site.
- `pytest_queryguard.max_queries()` context manager, which raises
  `QueryBudgetExceeded` and also works outside pytest.
- `@pytest.mark.allow_nplusone` marker to skip N+1 detection for one test.
- Ini options `queryguard_mode` (`fail`, `warn`, `off`),
  `queryguard_relationship_threshold` and `queryguard_repeat_threshold`,
  readable from `[tool.pytest.ini_options]`, `pytest.ini`, `setup.cfg` and
  pytest 9's native `[tool.pytest]` table.
- `--queryguard-mode` command-line option that overrides the ini value.
- `--queryguard-report` command-line option that prints the ten tests with
  the most queries at the end of the session.
- Support for `AsyncSession`, FastAPI's `TestClient` and code that queries
  from other threads.
- Only the test function body is measured; fixture setup and teardown are not.
- An internal error disables the plugin with a single warning instead of
  failing the test suite.

[Unreleased]: https://github.com/MaciejZiel/pytest-queryguard/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/MaciejZiel/pytest-queryguard/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/MaciejZiel/pytest-queryguard/compare/v0.5.0rc3...v0.5.0
[0.5.0rc3]: https://github.com/MaciejZiel/pytest-queryguard/compare/v0.5.0rc2...v0.5.0rc3
[0.5.0rc2]: https://github.com/MaciejZiel/pytest-queryguard/compare/v0.5.0rc1...v0.5.0rc2
[0.5.0rc1]: https://github.com/MaciejZiel/pytest-queryguard/compare/v0.4.1...v0.5.0rc1
[0.4.1]: https://github.com/MaciejZiel/pytest-queryguard/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/MaciejZiel/pytest-queryguard/compare/v0.4.0rc4...v0.4.0
[0.4.0rc4]: https://github.com/MaciejZiel/pytest-queryguard/compare/v0.4.0rc3...v0.4.0rc4
[0.4.0rc3]: https://github.com/MaciejZiel/pytest-queryguard/compare/v0.4.0rc2...v0.4.0rc3
[0.4.0rc2]: https://github.com/MaciejZiel/pytest-queryguard/compare/v0.4.0rc1...v0.4.0rc2
[0.4.0rc1]: https://github.com/MaciejZiel/pytest-queryguard/compare/v0.3.0...v0.4.0rc1
[0.3.0]: https://github.com/MaciejZiel/pytest-queryguard/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/MaciejZiel/pytest-queryguard/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/MaciejZiel/pytest-queryguard/tree/v0.1.0
