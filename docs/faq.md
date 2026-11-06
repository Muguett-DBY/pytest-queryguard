# FAQ

The questions below are the ones to ask before adding a test-time check to a
large suite: what it costs, how often it is wrong, how to silence it, and
where it does not fit. The answers marked as measured link to how.

## How does this relate to SQLAlchemy's `raiseload`?

`raiseload` is a loader option (`relationship(lazy="raise")`, or
`.options(raiseload(Order.customer))` per query) that makes SQLAlchemy raise
`InvalidRequestError` the moment code touches a relationship that was not
loaded. It prevents lazy loading everywhere the option applies, in tests and in
production, and it needs a change in the model or the query.

pytest-queryguard changes nothing in your code. It watches the statements
a test sends and reports patterns, including ones `raiseload` does not
cover: the same statement repeated in a loop with `session.get()`, Core and
`text()` statements, loops over deferred or expired columns, row-by-row writes
and missing indexes. The two work together: `raiseload` blocks a lazy load, so
a blocked access raises instead of reaching the database, and queryguard has
no lazy load to count at that point. Use `raiseload` where you want a hard
rule in the model, and queryguard where you want to find the places that need
one.

## Two parents each lazy-load one relationship. Is that really a finding?

Yes, by default. The relationship rule counts distinct parent objects, and the
default threshold is 2, so `order_a.customer` and `order_b.customer` loaded
separately is reported as `Order.customer was lazy-loaded 2 times`. That is the
smallest N+1, and test databases hold a few rows, so a higher default would
miss loops in tests that only create a couple of objects. If two is too strict
for your code, raise it:

```toml
[tool.pytest.ini_options]
queryguard_relationship_threshold = 3
```

or silence one place, as in the next question.

## How often is it wrong?

A finding means that a pattern happened in the test: the same relationship
loaded for several parents, or the same `SELECT` run five times or more. It
does not measure production latency. The patterns that are reported although
the code is fine, all listed on the [Limitations](limitations.md) page, are:

- pagination or a batched `IN (...)` load that runs five or more pages or
  chunks;
- a test that makes several independent requests, each repeating the same
  lookup (use `queryguard_repeat_scope = session`, see below);
- two parent objects that each load a relationship once (the default
  threshold of 2, see the next question);
- `IN (...)` queries with lists of different lengths: every length is one
  statement shape, so five such queries in one test are reported.

What it misses: statements outside SQLAlchemy, statements in fixtures, and a
self-referential walk up a parent chain. The [accuracy page](accuracy.md)
measures precision and recall per rule on a corpus of realistic scenarios, and
the [comparison](comparison.md) runs the same scenarios through other tools.

## A finding is a false positive. How do I silence it?

From the narrowest to the widest:

- a few lines of code, in a test or in the application:
  `with pytest_queryguard.ignore("relationship-load"):`;
- one finding, in one test: `@pytest.mark.allow_nplusone("Order.customer",
  function="list_orders")`;
- one finding, in the whole suite: a line in `queryguard_ignore`, for example
  `relationship-load Order.customer function=list_orders`;
- a finding you cannot fix yet: put it in a [baseline](baseline.md);
- a kind you do not want to fail tests: `queryguard_fail_on`, which makes the
  other kinds warnings;
- a table that is fine to scan (index check): `queryguard_index_ignore_tables`
  or `@pytest.mark.allow_missing_index`.

See [Ignoring findings](ignoring.md). Patterns that match nothing are listed
as stale ignores, so they do not outlive the code.

## My integration test makes several requests and fails with repeated-statement

By default the repeat rules count everything one test runs. A test that calls
an API five times, with a test client for example, runs the same lookups
(authentication, the organisation, a webhook) once per request, each in its own
SQLAlchemy `Session`. Nothing loops, but the count is five. The failure says so
when it can: `ran 9 times across 8 different Sessions or connection checkouts`,
with a note naming the option. Set it to count each `Session` on its own:

```toml
[tool.pytest.ini_options]
queryguard_repeat_scope = "session"
```

A loop inside one request, or any code that runs the same statement five times
in one `Session`, is still reported. The same applies to lazy loads, column
loads and write loops. See [Tests with several sessions or
requests](several-sessions.md) for how the scope is chosen and where it stops
being exact.

## Does it work with pytest-xdist?

Yes for detection, budgets, the baseline and SARIF: each worker measures its
own tests, and the controller merges what the workers saw. The session report
(`--queryguard-report`) needs one process and prints
`queryguard report: not available with pytest-xdist`. The summary sections of
the index check and the stale-ignore list are printed by the process that runs
the tests, so they are not shown under xdist, except for stale `ignore()`
blocks, which the workers send to the controller. The test suite runs the plugin
under `-n 2` and `-n 4` in CI.

## Which PostgreSQL drivers work?

N+1 detection and budgets hook SQLAlchemy events, not a driver, so they do not
depend on the driver. The index check needs a driver it can talk to through
SQLAlchemy: CI runs it with pg8000 (sync) and asyncpg (async), on PostgreSQL 14
to 18, and runs the whole suite once more with psycopg 3 as the sync driver,
on PostgreSQL 17. psycopg 3 and psycopg2 are LGPL-licensed, so the package does
not depend on them; psycopg 3 is installed only in that CI run, as a test
dependency that is not shipped. psycopg2 is not run in CI; it was checked by
hand during review and gave the same findings for plain statements, `%` in
SQL, expanding `IN`, autocommit and `stream_results`. On 2026-10-12 the 14
index scenarios of the [comparison](comparison.md) also gave the same results
through psycopg 3 (3.3.6) as through pg8000, on PostgreSQL 17.11.

Async drivers other than asyncpg (for example psycopg's async mode) are skipped
by the index check, with a notice in the session summary; N+1 detection and
budgets work on them, and CI runs psycopg's async mode too.

## Why was nothing reported for my test?

- the code under test ran in a fixture, not in the test body (see
  [the next question](#the-code-i-want-to-check-runs-in-a-fixture));
- the statements did not come through SQLAlchemy (a raw `sqlite3` or other
  DB-API connection, the Django ORM, Peewee, Tortoise ORM): the plugin does not
  see them, and `--queryguard-report` shows 0 statements for the test;
- `queryguard_mode = off`, or the finding is in the baseline or an ignore
  pattern;
- the count is below the threshold. `--queryguard-report` shows how many
  statements each test ran.

## The code I want to check runs in a fixture

Only the test body is measured, on purpose: seeding data is meant to run many
queries, and a session-scoped fixture would otherwise charge its setup to
whichever test happens to run first. When the fixture is the code under test,
wrap that code in [`record_queries()`](python-api.md) and check the log:

```python
import pytest

from pytest_queryguard import record_queries


@pytest.fixture
def report_data(session):
    with record_queries() as log:
        data = build_report_data(session)  # include the relationship reads
    log.assert_no_nplusone()
    return data
```

An N+1 in `build_report_data` then fails the setup of every test that uses
the fixture, with the usual message:

```text
E       pytest_queryguard.NPlusOneDetected: queryguard: N+1 detected
E         Order.customer was lazy-loaded 3 times
E         first at test_recipe.py:40 in build_report_data
```

Two things to keep in mind:

- only the block is recorded: a relationship the test reads later, after the
  fixture returned, is a lazy load outside it (and is measured with the test
  body instead);
- the block records statements from every thread while it runs, so keep
  unrelated background work, such as a server thread started by another
  fixture, out of it.

Thanks to @Muguett-DBY for
[the recipe](https://github.com/pytest-dev/pytest/discussions/15166).

## Does it slow my tests down?

It adds work per statement: a stack walk for the call site and a timer around
the cursor call. `scripts/bench_overhead.py` measures it, and CI compares it
with a stored baseline.

Measured with `scripts/bench_overhead.py` on an Intel Core i5-13500H, Python
3.13, SQLAlchemy 2.1.1, median of 15 runs of 5000 statements pinned to one core,
each configuration in a fresh process, against an **in-memory SQLite** database:

| Per statement | Measurement off | On | Added |
|---|---|---|---|
| Core `select` by key | 41 us | 50 us | +9 us (+22 %) |
| ORM `select` by primary key | 76 us | 107 us | +31 us (+40 %) |

Two rounds differ by a few points, so read these as "about a fifth to two
fifths" and not as exact figures. The percentages are large because an
in-memory SQLite statement is almost free: the added cost is a fixed 9 to 31
microseconds, and a statement that goes to a real database takes far longer, so
the share is much smaller there. These are micro-benchmarks. On a generated
suite of 300 tests against a file-based SQLite database and against PostgreSQL
the difference between running with and without the plugin was smaller than the
run-to-run noise; see [Performance](performance.md) for the method and numbers.

The options that are off by default cost nothing until you turn them on:

- The missing-index check (PostgreSQL) adds a few database round trips per
  distinct statement per test, about 1 ms per statement in the benchmark in
  [Missing-index check](index-check.md). On SQLite it adds, over the "on" column
  above, +13.5 us for a repeated statement shape, +31.5 us for a repeated ORM
  statement and +34.5 us for a new shape that must be explained
  (`scripts/bench_overhead.py --index-check`).
- Eager-load tracking (`queryguard_unused_eager_load`) wraps attribute reads:
  171 ns per attribute read with the option off, 207 ns with it on but idle
  (outside a test body), 306 ns while a test body is tracked
  (`scripts/bench_eager_overhead.py`).
- Unused-column tracking (`queryguard_unused_columns`) wraps the same reads
  and listens to each loaded row: about 170 ns per attribute read and 1.3 us
  per loaded row while a test body is tracked
  (`scripts/bench_columns_overhead.py`, see [Unused columns](unused-columns.md)).

The numbers depend on your machine and database.

## Does it work with Django, Peewee or raw SQL?

Not with Django's ORM, Peewee or Tortoise ORM: only SQLAlchemy 2 is supported (the
[comparison](comparison.md) lists tools for Django). Raw SQL that
goes through SQLAlchemy (`text()`, Core) is seen; a raw DB-API connection that
SQLAlchemy does not manage is not.

## Can it break my test suite?

It is designed not to. If one of its listeners raises, the plugin stops
measuring for the rest of the session instead of failing your tests, issues a
`QueryGuardInternalWarning` that names the listener, and prints a
"queryguard: measurement stopped" section at the end of the session with the
error and the line that raised it. A test then passes or fails on its own
assertions. Invalid configuration values are the exception: they stop pytest
at start-up with a usage error, before any test runs.

On SQLAlchemy 2.0.20 to 2.0.49, every `do_orm_execute` listener, the plugin's
included, makes the outer statement's `yield_per` and `autoflush` options
reach its `selectinload` statement (SQLAlchemy issue 13301): `yield_per` then
fails with "Can't use the ORM yield_per feature in conjunction with unique()".
The plugin undoes that for its own listeners, so a test does not fail only
because the plugin is installed. A listener of your own triggers the issue
with or without the plugin; upgrading to SQLAlchemy 2.0.50 or later fixes the
cause.

The checks do not change the SQL your code sends. The index check explains
statements without running them: on PostgreSQL inside a savepoint that is
rolled back, on SQLite and MySQL with `EXPLAIN`, so data and the test's
transaction are left as they were.

## How do I turn it off?

- For one run: `pytest -p no:queryguard` does not load the plugin at all
  (markers such as `max_queries` are then unknown, which pytest reports as a
  warning, or an error under `--strict-markers`); `pytest --queryguard-mode=off`
  loads it but stops N+1 detection, while budgets still apply.
- For one test: `@pytest.mark.allow_nplusone` without arguments.
- For the suite: `queryguard_mode = "off"` in the configuration.

## Does it work in a monorepo?

Yes. File paths in reports, the baseline and ignore patterns are relative to
pytest's rootdir, so they depend on where pytest is configured, not on the
directory you start it from. With one `pytest.ini` at the repository root and
`pythonpath = services/a services/b`, a finding reads
`first at services/a/app/orders.py:10` and the baseline stores that path. If
each service has its own configuration file and its own rootdir, keep one
baseline file per service: the same finding is `app/orders.py` there.

Call sites skip everything in `site-packages`. Install the monorepo's own
packages editable (`pip install -e`, `uv sync`), or a finding in a shared
library points to the test instead of the library line. For GitHub code
scanning from a service directory, the action's `working-directory` input
keeps SARIF paths relative to the repository root (see [SARIF](sarif.md)).

## Does it work with async tests?

Yes. `AsyncSession` runs on a sync engine underneath, which the plugin
watches, so an `async def` test run by pytest-asyncio or anyio is measured like
any other, and the call site is the `await` in your code. Lazy loads through
`awaitable_attrs` are reported as relationship loads. The
[pytest-asyncio recipe](recipes-pytest-asyncio.md) covers configuration
mistakes that look like database problems (strict mode, async fixtures, event
loop scopes), and [Getting started](getting-started.md#async-and-fastapi) has
an async FastAPI example. On SQLAlchemy 2.1 and newer install
`sqlalchemy[asyncio]` for greenlet.

## When should I not use it?

- Your data access does not go through SQLAlchemy 2: Django ORM, Peewee,
  Tortoise ORM, raw DB-API connections. The plugin sees nothing there, and
  says nothing. For Django see [django-zeal](comparison.md).
- You want to see N+1 queries in production: the
  [in-application mode](in-app.md) is for development and staging only. An
  APM tool fits production better.
- Most of the database work in your suite happens in fixtures, or the tests
  that matter are end-to-end tests against a deployed service: only the test
  function body is measured, in the same process.
- Every relationship already uses `lazy="raise"` and you only need that rule:
  `raiseload` gives a hard error with no plugin (see the first question).
  Repeated statements, budgets and the index check are still beyond it.
- You need to assert the exact SQL a block sends: that is a snapshot tool's
  job; this plugin looks for patterns.
