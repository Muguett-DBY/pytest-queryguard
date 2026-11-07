# Comparison with other tools

This page compares pytest-queryguard with other tools that look for N+1
queries or count queries. It is written by the author of pytest-queryguard,
so read it with that in mind: the matrix is built from the competitors' own
documentation and from running their packages, every cell says which, and the
measurements are ones I ran myself, on scenarios I wrote.

- Facts about other tools were read on **2026-10-12** and re-checked on
  **2026-10-13**: no tool had a newer release on PyPI since, the tools with a
  public repository had no new commit on its default branch (fastapi-nplusone
  and pytest-capquery list none on PyPI), and the cells below that changed say
  so.
- pytest-queryguard: the measurements were made on the `next` branch at commit
  `b64d19c`, before 0.4.0rc1, and describe 0.4.0 (2026-10-14). 0.4.1 changed only the database time of
  statements that finish in another thread, which no row below measures, and
  0.5.0 added the in-application mode without changing the plugin.
- Versions re-checked on **2026-11-05** on PyPI: queryspy 0.6.0 and
  django-zeal 2.3.0 had been released since. Their changelogs list no change
  to a cell of the matrix except the one marked "since 2.3.0": queryspy 0.5.1
  and 0.6.0 change only the supported Python versions (0.6.0 drops 3.10), and
  django-zeal 2.3.0 adds an opt-in check for unused `zeal_ignore` blocks and
  fixes in its N+1 detection. The cells marked (v) were verified on queryspy
  0.5.0 and were not run again on 0.6.0.
- The [head-to-head](#head-to-head-in-github-actions) numbers (accuracy
  and overhead of queryspy 0.6.0, pytest-querycount 0.4.0,
  n-plus-one-sniffer and nplusone 1.0.0) were measured on **2026-11-07** in
  GitHub Actions.
- Marks in the matrix: **(v)** verified by running the package or reading its
  installed source on that date (for queryspy, version 0.5.0; see above); **(r)** read in the package's PyPI
  description only, not run. Anything I could not check is marked as such.

## Who is who

| Tool | Version (release date) | First release on PyPI | Targets | License |
|---|---|---|---|---|
| pytest-queryguard | 0.8.0 (2026-11-06) | 2026-10-10 | SQLAlchemy 2 | MIT |
| [queryspy](https://github.com/sqla-native/queryspy) | 0.6.0 (2026-10-22) | 2026-08-04 | SQLAlchemy 2.0+, Python 3.11+ | MIT |
| [pytest-querycount](https://github.com/ldavidsm/pytest-querycount) | 0.4.0 (2026-09-27) | 2026-09-27 | SQLAlchemy 2.x | MIT |
| [nplusone](https://github.com/jmcarp/nplusone) | 1.0.0 (2018-05-21) | 2015-06-30 | Django, SQLAlchemy, Peewee | MIT |
| [django-zeal](https://github.com/taobojlen/django-zeal) | 2.3.0 (2026-10-14) | 2024-07-20 | Django | MIT |
| [fastapi-nplusone](https://pypi.org/project/fastapi-nplusone/) | 0.1.0 (2026-02-10) | 2026-02-06 | FastAPI with SQLAlchemy | BSD-3-Clause |
| [pytest-capquery](https://pypi.org/project/pytest-capquery/) | 0.3.3 (2026-04-04) | 2026-04-01 | SQLAlchemy, Python 3.13+ | CC BY-NC-SA 4.0 (see below) |
| [django-nplus1](https://github.com/oliverhaas/django-nplus1) | 0.6.2 (2026-10-04) | 2026-04-05 | Django | MIT |
| [django-query-doctor](https://github.com/hassanzaibhay/django-query-doctor) | 2.3.0 (2026-08-01) | 2026-03-14 | Django | MIT |
| [n-plus-one-sniffer](https://github.com/async-workflows/n-plus-one-sniffer) | not on PyPI (repository created 2026-07-18) | not on PyPI | SQLAlchemy 2.0+ | MIT (repository) |
| [orm-lens](https://github.com/FROWNINGdev/orm-lens) | 1.17.1 (2026-11-05) | 2026-07-12 as django-orm-lens, 2026-10-31 as orm-lens | Django; SQLAlchemy models in preview since 2026-10-31 | MIT |

Release dates come from the PyPI JSON API on 2026-10-12, re-checked on 2026-10-13 and 2026-11-05;
the rows of pytest-queryguard, django-nplus1, django-query-doctor and
n-plus-one-sniffer were read on 2026-11-07 (PyPI and GitHub). Most of these
projects are young: pytest-queryguard, queryspy and pytest-querycount shipped
their first release in the last three months, so expect all of them to change. This
table will be out of date; check the projects' own pages.

orm-lens is a different kind of tool: it reads the source of Django and
SQLAlchemy models and the code that queries them (static analysis of the
syntax tree, with no database and without starting the application), and has
a command line and an MCP server for AI coding agents. Its `nplusone` command
looks for N+1 patterns in the code; it does not run tests or watch the
statements a test sends, so it is not in the matrix below. Read from its PyPI
description on 2026-11-05, not run. pytest-queryguard has an experimental
[static check](static-check.md) of its own (since 0.8.0), which reads the
mappers by importing your model modules and reports only lazy relationships
read in a loop over a query inside one function, and an experimental
[MCP server](mcp.md), whose tools run the test suite and return the findings
the tests produce. The two static checks were not compared.

Three tools found on 2026-11-07 are not in the matrix below. What follows was
read in their READMEs on that date; none of them was run:

- **django-nplus1**: a Django-only fork of nplusone. Besides per-request
  detection, its README describes a "corpus mode"
  (`--nplus1-eager-corpus`) that reports an eager load only when no test of
  the whole pytest session read it, and a Celery integration that gives each
  task its own detection scope. pytest-queryguard's
  [unused-eager-load](unused-eager-load.md) check is per test; its
  [in-application mode](in-app.md#celery) measures each Celery task on its own.
- **django-query-doctor**: Django; runs every query through analyzers for N+1,
  duplicate queries, missing indexes, "fat SELECT" (all columns loaded, few
  used), `len(qs)` instead of `count()` and `bool(qs)` instead of
  `exists()`, query complexity, and an analysis of the source of DRF
  `SerializerMethodField` methods. It works as middleware, a context manager,
  in tests and in CI. pytest-queryguard has no check for the
  `len()`/`count()` kind of misuse or for query complexity.
- **n-plus-one-sniffer**: SQLAlchemy 2.0, installed from GitHub (not on
  PyPI). A context manager and a pytest plugin (a `no_n_plus_one` marker and an
  `n_plus_one` fixture) that flag a statement fingerprint repeated more than a
  threshold (3 by default) and suggest `selectinload`, `joinedload` or
  `subqueryload`. Its README does not mention relationship names, budgets or
  a baseline.

pytest-capquery's PyPI metadata has no license field. Its README badge and the
`LICENSE` file in its GitHub repository are Creative Commons
Attribution-NonCommercial-ShareAlike 4.0, which is not an open-source license
and restricts commercial use. Read it before you adopt it.

## Feature matrix

| | pytest-queryguard | queryspy 0.6.0 | pytest-querycount 0.4.0 | nplusone 1.0.0 | django-zeal 2.3.0 | fastapi-nplusone 0.1.0 | pytest-capquery 0.3.3 |
|---|---|---|---|---|---|---|---|
| Works with SQLAlchemy 2 | yes | yes (v) | yes (v) | no: import fails on 2.1.4, silent or errors on 2.0.54 and 1.4.54 (v) | not applicable (Django only) (r) | SQLAlchemy, version not stated (r) | SQLAlchemy, version not stated (r) |
| Fails tests with no marker or option | yes (v) | no: `--queryspy-strict` or `queryspy_fail_on`; budgets only with a marker or `queryspy_budget` (v) | no: markers, or `--querycount-max` (v) | not a pytest plugin (r) | needs a fixture or middleware setup (r) | logs a warning from a middleware (r) | needs a fixture; assertions in the test (r) |
| N+1 from lazy-loaded relationships | yes (v) | yes (v) | by repeated statement shape, without the relationship name (v) | yes, SQLAlchemy 1.3.24 only (v) | yes, Django (r) | by repeated normalized query per request (r) | by an expected query count in a block (r) |
| Repeated statements (`session.get()` loops, Core, `text()`) | yes (v) | `session.get()` loops yes; a Core `select()` loop and a loop with literal values not reported (v) | yes (v) | no (r) | Django only | yes (r) | by count (r) |
| Deferred and expired column loads | yes, with the right hint (v) | yes, with the right hint (v) | as a repeated statement, generic hint (v) | no (r) | `.only()` and `.defer()` (r) | no (r) | no (r) |
| Hint: `joinedload` or `selectinload` by cardinality | yes (v) | yes (v) | generic text (v) | no | Django hints (r) | no | no |
| Query budget (`max_queries`) | yes (v) | yes (v) | yes (v) | no | no | no | exact count in a block (r) |
| Budget failure lists every statement with its call site | yes (v) | no: the marker and `queryspy_budget` print only "expected at most N, got M"; the `assert_max_queries()` context manager adds the database time and N+1 findings, not the statements (v, 2026-10-13) | yes, with timing (v) | not applicable | not applicable | not applicable | prints a copy-paste assertion (r) |
| Time budget and per-query timing | `max_db_time`, timing in `QueryLog` (v) | timing and the slowest statement; no time-budget option in the installed 0.5.0 (v, 2026-10-13) | timing in the failure and the report (v) | no | no | no | no |
| Baseline for existing findings | yes (v) | yes (v) | no baseline; `--querycount-write-budgets` writes exact budgets into your tests (v) | no | allow-list in settings (r) | no | no |
| SARIF and a GitHub Action | SARIF yes; a composite action in this repository, `action/` (v) | SARIF yes (v: `_serialize.py` in the installed package); an action, `sqla-native/queryspy@v0` (r) | not found: no SARIF code in the installed package (v) | no | no | no | no |
| Missing-index check | PostgreSQL 16+, SQLite, and MySQL and MariaDB (tested in CI on MySQL 8.4 and MariaDB 11.8), with a `CREATE INDEX`; hypopg verification on PostgreSQL (v) | none: no EXPLAIN code in the installed package (v) | PostgreSQL only, `no_seq_scan`, with a `CREATE INDEX` suggestion (v) | no | no | no | no |
| Unused eager loads | experimental, off by default (v) | no, deliberately (r) | no | yes, for Flask-SQLAlchemy and Django per its README (r) | no (r) | no | no |
| Row-by-row write loops | opt-in (v) | not reported (v) | not reported (v) | no | no | no | no |
| Per-finding ignore | marker, ini patterns, baseline and an `ignore()` block (v) | marker and an `ignore()` block (v) | `ignore=` for the index check only (r) | whitelist and an `ignore` block (r) | allow-list and an ignore block; since 2.3.0 an opt-in check for unused `zeal_ignore` blocks (r) | no | no |
| In-app middleware and request panel | yes, experimental since 0.5.0: ASGI and WSGI, optional panel (v) | yes: ASGI and WSGI, optional panel (v: modules present) | no | Django, Flask-SQLAlchemy, WSGI (r) | Django middleware (r) | yes, FastAPI middleware (r) | no |
| Async SQLAlchemy | yes (v) | yes (v) | yes (v) | no | no | not stated | yes, through `sync_engine` (r) |
| pytest-xdist | tested in CI | not stated | summary per worker; budgets and N+1 unaffected (r) | not stated | not stated | not stated | not stated |
| Documentation site | this site | yes (r) | README | README | README | README | README |

"not stated" means I did not find a statement in the README, not that it does
not work.

## Where the others are ahead

- **In your running application.** queryspy ships ASGI and WSGI middleware and
  an optional request panel (modules `queryspy.asgi`, `queryspy.wsgi` are in
  the installed package), and fastapi-nplusone is a FastAPI middleware.
  nplusone and django-zeal run inside Django, Flask and WSGI apps.
  pytest-queryguard's in-application mode (see [In-application mode](in-app.md))
  is experimental and ships from 0.5.0.
- **Other databases and drivers in their test matrix.** The queryspy README
  says it is tested against SQLite, PostgreSQL and MySQL with psycopg,
  asyncpg, pymysql, aiomysql and aiosqlite. pytest-queryguard's CI runs the
  whole suite on SQLite (also through aiosqlite) and on PostgreSQL 14 to 18
  through pg8000 and asyncpg, once more with psycopg 3 (see the [FAQ](faq.md));
  on MySQL 8.4 and MariaDB 11.8, through PyMySQL and aiomysql, it runs only
  the tests of the index check.
- **Django.** django-zeal and nplusone cover Django; pytest-queryguard does not.
- **Writing budgets for you.** `pytest-querycount --querycount-write-budgets`
  edits your tests to add an exact `max_queries` marker to each, and
  `--querycount-max` applies one budget to every test. pytest-queryguard has a
  [baseline](baseline.md) instead, which records findings, not budgets.
- **Exact SQL assertions and snapshots.** pytest-capquery asserts the exact
  statements and transaction events of a block and keeps `.sql` snapshots;
  pytest-queryguard looks for patterns and does not assert exact SQL.
- **A missing-index check exists elsewhere too.** pytest-querycount has its
  own PostgreSQL check, based on the same idea (plan with sequential scans
  disabled); pytest-queryguard is not alone in having one.
- **Maturity.** The projects here are all young. nplusone has been around
  longest, but has not had a release since 2018.

## Common problems with other tools

The issue trackers of N+1 tools, in Python and in Ruby (Bullet and Prosopite,
for Rails), show problems that users run into again and again. Some of them
pytest-queryguard was built to avoid; the list below names only those, with
where the behaviour is documented. The linked issues are evidence that the
problem exists, not a judgement of the projects, several of which have fixed
or worked around them since.

- **Findings from test setup.** django-zeal fails tests in the setup phase
  when Factory Boy creates data
  ([django-zeal#19](https://github.com/taobojlen/django-zeal/issues/19)), and
  a Prosopite thread recommends wrapping only the part of a spec that is not
  setup ([prosopite#22](https://github.com/charkost/prosopite/issues/22)).
  pytest-queryguard measures only the test function body; fixtures are not
  measured (see [How detection works](how-detection-works.md) and the
  [FAQ](faq.md#the-code-i-want-to-check-runs-in-a-fixture)).
- **No useful location.** nplusone's most requested feature is the call stack
  of a finding ([nplusone#22](https://github.com/jmcarp/nplusone/issues/22));
  Bullet users ask for the same
  ([bullet#384](https://github.com/flyerhzm/bullet/issues/384)).
  pytest-queryguard reports the line of your code (`first at`), the callers
  (`called from`, `queryguard_stack_depth`) and, for a relationship, the line
  that loaded the parents; SQLAlchemy, pytest and installed packages are left
  out of the stack.
- **Cannot gate on some kinds only.** Bullet users ask to turn single
  detectors off ([bullet#481](https://github.com/flyerhzm/bullet/issues/481)),
  and a queryspy user measured that every repeated-statement finding of a large
  suite was noise while the lazy-load findings were not
  ([queryspy#14](https://github.com/sqla-native/queryspy/issues/14)).
  `queryguard_fail_on` lets some kinds fail and others only warn (see the
  [configuration reference](configuration.md)).
- **Ignore lists that rot.** django-zeal users asked for a check that
  allow-list entries name fields that exist, since a typo fails silently
  ([django-zeal#25](https://github.com/taobojlen/django-zeal/issues/25)), and
  for ignore blocks that ignore nothing to be reported
  ([django-zeal#48](https://github.com/taobojlen/django-zeal/issues/48); 2.3.0
  added an opt-in check). pytest-queryguard lists patterns, markers
  and `ignore()` blocks that matched nothing as "stale ignores", and an
  unknown kind in a pattern is a usage error (see
  [Ignoring findings](ignoring.md)).
- **Schema queries reported as N+1.** Prosopite reported PostgreSQL catalog
  lookups made by `upsert_all`
  ([prosopite#26](https://github.com/charkost/prosopite/issues/26)).
  pytest-queryguard's repeat rules skip schema reflection: statements that
  start with `PRAGMA` or read `pg_catalog`, `information_schema` or SQLite's
  schema tables (see [How detection works](how-detection-works.md); they still
  count for budgets).
- **Overhead.** django-zeal once doubled the run time of a 5,000-test suite
  ([django-zeal#24](https://github.com/taobojlen/django-zeal/issues/24),
  fixed in 1.4.1). pytest-queryguard's overhead is measured and published,
  with the script, in [Performance](performance.md).

A problem that pytest-queryguard shares: a test that hands every request one
`Session` makes the requests one unit of work, and queryspy#14 is also about
that. See
[A session per request in a test transaction](recipes-test-transaction.md).

## What I measured

### Head-to-head in GitHub Actions

Measured on **2026-11-07** by the
[head-to-head workflow](https://github.com/MaciejZiel/pytest-queryguard/actions/runs/42391426710) (`.github/workflows/head-to-head.yml`, the
adapters in `scripts/head_to_head/`), which anyone can run again from the
Actions tab; it also runs once a month. Every tool that runs inside pytest
and supports SQLAlchemy 2 is in it: queryspy 0.6.0, pytest-querycount
0.4.0, n-plus-one-sniffer (not on PyPI, installed from git at `d4790f8`,
reported as 0.1.0) and nplusone 1.0.0. orm-lens is not: it reads source code
and has no pytest mode. pytest-queryguard is 0.8.0 at the commit of the run.
Python 3.13.18, SQLAlchemy 2.1.1, pytest 9.1.1 on `ubuntu-latest` (x86_64,
4 CPUs).

How each tool is run, in its own job: its defaults plus what its own
documentation says makes a test fail on an N+1. queryspy with
`--queryspy-strict`; pytest-querycount and n-plus-one-sniffer with their
`no_n_plus_one` marker on every test (n-plus-one-sniffer also needs an
`nplusone_engine` fixture, which returns the `Engine` class so it sees every
engine a scenario creates); nplusone with its documented generic use,
`import nplusone.ext.sqlalchemy` and a `Profiler()` around each test.
A scenario counts as flagged when the tool's own exception or message is in
the failed test's report (or, for pytest-queryguard, when it recorded a
finding); a test that failed for any other reason is an **error**, never a
pass. A run with no tool at all passes every scenario.

**Accuracy**, on the 252 scenarios of the [accuracy corpus](accuracy.md)
that ran (123 BUG, 129 CLEAN):

| tool | BUG flagged | CLEAN flagged | errors | precision | recall (all BUG) | recall (BUG of kinds it claims) |
|---|---|---|---|---|---|---|
| pytest-queryguard, defaults | 106 | 2 | 0 | 98% | 86% | 97% |
| pytest-queryguard, all opt-in rules | 122 | 8 | 0 | 94% | 99% | 99% |
| queryspy 0.6.0 | 98 | 27 | 0 | 78% | 80% | 95% |
| pytest-querycount 0.4.0 | 103 | 27 | 0 | 79% | 84% | 100% |
| n-plus-one-sniffer `d4790f8` | 103 | 25 | 0 | 80% | 84% | 97% |
| nplusone 1.0.0, SQLAlchemy 2.1.1 | 0 | 0 | 252 | n/a | 0% | 0% |
| nplusone 1.0.0, SQLAlchemy 2.0.54 | 1 | 0 | 2 | 100% | 1% | 2% |

Precision and recall per finding kind, with `n/a` for the kinds a tool does
not claim (not 0%), and what each tool missed or flagged wrongly, are on the
[accuracy page](accuracy.md#comparison-with-other-tools) and in the run's
summary.

Read this table with its bias in mind: **the corpus was written by the
author of pytest-queryguard, for pytest-queryguard.** Its CLEAN scenarios
include the shapes pytest-queryguard was taught to leave alone (pagination,
`IN` chunks, `selectinload` batches, `yield_per`), three of them use its own
`ignore()` API, and the thresholds of the other tools were left at their
defaults. What the table does show: the three working plugins catch nearly
every N+1 shape they claim (pytest-querycount all of them), and each fails
25 to 27 of the 129 CLEAN scenarios, where pytest-queryguard fails 2 by
default. nplusone does not import on SQLAlchemy 2.1, and on 2.0 it misses
every lazy load of the corpus (2.0-style `select()` bypasses the
`Query.__iter__` it patches) and raises `AttributeError` on two
`subqueryload` scenarios.

**Overhead**, same run, one job, every tool in its own virtual environment
with the same Python, pytest and SQLAlchemy, the tools interleaved and their
order reversed every round, 15 rounds, each a fresh process pinned to one
core. Per statement: `scripts/compare/bench_micro.py` (in-memory SQLite),
the best round. The suite: the generated 300-test suite of
`scripts/bench_suite.py` against file SQLite, each tool set to look for N+1
in every test (pytest-querycount at `threshold=3`: its default of 2 fails
the suite, which reads one parent twice per test).

| | pytest-queryguard | queryspy | pytest-querycount | n-plus-one-sniffer |
|---|---|---|---|---|
| 300-test suite, added wall time (best of 15) | +0.8% | +28.9% | +18.7% | +28.1% |
| 300-test suite, added wall time (median of 15) | +1.7% | +28.8% | +18.5% | +29.0% |
| Core `select`, added per statement | +17.7 us | +9.5 us | +94.0 us | +16.4 us |
| ORM `select`, added per statement | +26.6 us | +100.5 us | +100.1 us | +14.0 us |
| Lazy load, added per statement | +37.2 us | +122.8 us | +112.4 us | +16.4 us |

n-plus-one-sniffer adds the least per statement (one `before_cursor_execute`
listener and a regular expression) but about 28% to the suite, so most of its cost
is outside the statements measured here; queryspy adds the least to a Core statement. pytest-queryguard
adds the least to the suite. nplusone has no overhead figure: it does not
import on SQLAlchemy 2.1. Shared CI runners are noisy, which is why both the
best and the median are given.

### N+1 and budgets on SQLite

The head-to-head above runs the 252 scenarios of the
[accuracy page](accuracy.md) under every tool. The smaller table below is
the first comparison, of 2026-10-12.

I ran 45 scenarios in `tests/scenarios` (SQLAlchemy ORM and Core, sync and
async, SQLite) against three packages, each in its own virtual environment on
2026-10-12: pytest-queryguard `next` at `b64d19c`, queryspy 0.5.0 and
pytest-querycount 0.4.0, with Python 3.13, SQLAlchemy 2.1.4 and pytest 9.1.1.
The first 41 scenarios are the ones from an earlier comparison; the four
scenarios `09d`, `09e`, `11d` and `11e` were added later for pytest-queryguard's own
tests.

How each tool was run: pytest-queryguard with its defaults;
queryspy with `--queryspy-strict` (without it, only the budgets in
the scenarios fail: 2 of 45 tests); pytest-querycount with `@pytest.mark.no_n_plus_one`
on every test that has no budget (without markers, all 45 pass). Budgets use the tool's
own marker. The "queryguard, small-repeat and write-loop on" column adds
`queryguard_small_repeat_threshold = 2` and `queryguard_write_loop_threshold = 2`.

"Right verdict" is my judgement from the scenario's name (`_bad` should fail,
`_ok` and `_legit` should pass); scenarios where I could not say are marked
`-`. The scenarios were written for pytest-queryguard, and some of them test
decisions it made on purpose (for example, three `session.get()` calls are
below its default threshold), so the table is biased towards the tool that
wrote it.

| Scenario | Right verdict | queryguard | queryguard, small-repeat and write-loop on | queryspy | pytest-querycount |
|---|---|---|---|---|---|
| `01_lazy_n1_bad` | fail | FAIL | FAIL | FAIL | FAIL |
| `02a_selectinload_ok` | pass | pass | pass | pass | pass |
| `02b_joinedload_ok` | pass | pass | pass | pass | pass |
| `02c_subqueryload_ok` | pass | pass | pass | pass | pass |
| `03_session_get_loop_bad` | fail | FAIL | FAIL | FAIL | FAIL |
| `03b_session_get_loop_3_bad` | fail | pass (wrong) | FAIL | FAIL | FAIL |
| `04_async_awaitable_attrs_bad` | fail | FAIL | FAIL | FAIL | FAIL |
| `04b_async_selectin_ok` | pass | pass | pass | pass | pass |
| `04c_async_get_loop_bad` | fail | FAIL | FAIL | FAIL | FAIL |
| `05a_repeat_x2` | - | pass | FAIL | FAIL | FAIL |
| `05b_repeat_x3_batch_legit` | - | pass | FAIL | FAIL | FAIL |
| `05c_repeat_x4` | - | pass | FAIL | FAIL | FAIL |
| `05d_repeat_x5` | - | FAIL | FAIL | FAIL | FAIL |
| `05e_repeat_x6` | - | FAIL | FAIL | FAIL | FAIL |
| `06_legit_diff_shapes_ok` | pass | pass | pass | pass | pass |
| `07a_budget_exact_ok` | pass | pass | pass | pass | pass |
| `07b_budget_over_by_1_bad` | fail | FAIL | FAIL | FAIL | FAIL |
| `08_m2m_lazy_bad` | fail | FAIL | FAIL | FAIL | FAIL |
| `08b_m2m_selectin_ok` | pass | pass | pass | pass | pass |
| `09_selfref_parent_walk_bad` | fail | pass (wrong) | pass (wrong) | pass (wrong) | pass (wrong) |
| `09b_selfref_joined_ok` | pass | pass | pass | pass | pass |
| `09c_selfref_kids_lazy_bad` | fail | FAIL | FAIL | FAIL | FAIL |
| `09d_many_to_one_lazy_bad` | fail | FAIL | FAIL | FAIL | FAIL |
| `09e_many_to_one_joinedload_ok` | pass | pass | pass | pass | pass |
| `10a_dynamic_loop_bad` | fail | FAIL | FAIL | FAIL | FAIL |
| `10b_writeonly_loop_bad` | fail | FAIL | FAIL | FAIL | FAIL |
| `10c_raiseload_ok` | pass | pass | pass | pass | pass |
| `11a_deferred_loop_bad` | fail | FAIL | FAIL | FAIL | FAIL |
| `11b_undefer_ok` | pass | pass | pass | pass | pass |
| `11c_expire_after_commit_refresh_bad` | fail | FAIL | FAIL | FAIL | FAIL |
| `11d_expire_after_commit_few_ok` | pass | pass | pass | FAIL (wrong) | FAIL (wrong) |
| `11e_expire_on_commit_false_ok` | pass | pass | pass | pass | pass |
| `12a_insert_flush_per_row` | - | pass | FAIL | pass | pass |
| `12b_add_all_one_flush_ok` | pass | pass | pass | pass | pass |
| `12c_core_insert_per_row` | - | pass | FAIL | pass | pass |
| `12d_core_executemany_ok` | pass | pass | pass | pass | pass |
| `12e_budget_counts_writes_bad` | fail | FAIL | FAIL | FAIL | FAIL |
| `13a_core_select_loop_bad` | fail | FAIL | FAIL | pass (wrong) | FAIL |
| `13b_count_loop_bad` | fail | FAIL | FAIL | FAIL | FAIL |
| `14a_pagination_3_pages_legit` | pass | pass | pass | FAIL (wrong) | FAIL (wrong) |
| `14b_two_parents_legit` | pass | FAIL (wrong) | FAIL (wrong) | FAIL (wrong) | FAIL (wrong) |
| `14c_same_parent_twice_ok` | pass | pass | pass | pass | pass |
| `14d_identity_map_get_ok` | pass | pass | pass | pass | pass |
| `14e_in_list_varying_len_legit` | pass | FAIL (wrong) | FAIL (wrong) | FAIL (wrong) | FAIL (wrong) |
| `14f_literal_binds_loop_bad` | fail | FAIL | FAIL | pass (wrong) | FAIL |

Score, on the 38 scenarios with a right verdict: pytest-queryguard 34,
with the two extra rules on 35, queryspy 31, pytest-querycount 33. On the 34 of those that
belong to the original 41: 30, 31, 28 and 30. These numbers measure
agreement with my labels, not how useful a tool is. What the table shows:

- All three agree on most scenarios: lazy-load loops, `selectinload`,
  `joinedload` and `subqueryload`, async loops, budgets, `raiseload`.
- By default pytest-queryguard does not report a few runs of one statement
  (`03b`, `05a` to `05c`): a deliberate choice. It is also why it passes the
  pagination scenario `14a` that the other two fail: three pages are below its
  repeat threshold of 5. (This table predates the rule that recognises
  pagination and chunked `IN` loads from their parameters, added on
  2026-10-13; `14a` passes with or without it, and `14e`, whose `IN` lists
  overlap, is still reported.) With the small-repeat rule on it reports
  `03b` and `05a` to `05c` too, and `03b` becomes correct.
- queryspy missed the Core `select()` loop (`13a`) and the loop with literal
  values (`14f`); the other two reported them.
- `11d` (a few objects refreshed after a `commit()`) is reported by queryspy
  and pytest-querycount and not by pytest-queryguard, whose column-load
  threshold is 5.
- Two parents that each lazy-load a relationship once (`14b`) are reported by
  all three. I labelled it "legit"; it is the smallest case of a loop, so you
  could label it the other way. It is the default threshold of pytest-queryguard
  (see the [FAQ](faq.md)).
- Nobody reports `09`, a self-referential walk up a parent chain where every
  node loads the next one.
- On a budget failure, pytest-queryguard and pytest-querycount list the
  statements; queryspy's marker prints only the counts.

### Missing indexes on PostgreSQL

14 scenarios on PostgreSQL 17.11 (docker, the official image), with
pytest-queryguard `--queryguard-index-check` through two drivers and
pytest-querycount `--querycount-no-seq-scan`. queryspy has no such check, so it is
not in the table. The tables have 20 to 40 rows. Python 3.13, SQLAlchemy 2.1.4.
The advisor ran without hypopg.

| Scenario | Right verdict | queryguard (pg8000) | queryguard (psycopg 3) | pytest-querycount (psycopg 3) |
|---|---|---|---|---|
| i01: equality on an unindexed FK column | fail | FAIL | FAIL | FAIL |
| i02: equality on an indexed column | pass | pass | pass | pass |
| i03: SELECT without a filter | pass | pass | pass | pass |
| i04: primary-key lookup | pass | pass | pass | pass |
| i05: lower(email) with an expression index | pass | pass | pass | pass |
| i06: lower(city) with no expression index | fail | FAIL | FAIL | FAIL |
| i07: filter on the second column of a two-column index | fail | FAIL | FAIL | pass (wrong) |
| i08: filter on the first column of that index | pass | pass | pass | pass |
| i09: LIKE prefix on an unindexed text column | fail | FAIL | FAIL | FAIL |
| i10: ORDER BY id LIMIT 5, no filter | pass | pass | pass | pass |
| i11: range on an unindexed timestamp | fail | FAIL | FAIL | FAIL |
| i12: filter on a 2-row lookup table, no index | - | FAIL | FAIL | FAIL |
| i13: join on an unindexed FK, no WHERE | pass | pass | pass | pass |
| i14: IN list on an unindexed FK column | fail | FAIL | FAIL | FAIL |

`i07` is a judgement call: the two-column index can serve the filter with a
full index scan, and PostgreSQL 18 may skip-scan it. pytest-queryguard reports it on
PostgreSQL 16 and 17 and pytest-querycount does not. On the 13 scenarios
with a right verdict, pytest-queryguard agreed with my labels on 13 with both
drivers, pytest-querycount on 12. Both report the 2-row lookup table (`i12`), which
is by design; both give you a way to exclude such tables.
pytest-queryguard's finding also includes an Alembic call for
simple cases and says "an expression index may be needed" for `i06`;
pytest-querycount's suggests `CREATE INDEX` for plain columns and gives no
suggestion for the expression.

### Overhead

See the [head-to-head](#head-to-head-in-github-actions) above for the
numbers of 2026-11-07 on a CI runner. The earlier measurement of 2026-11-05
on my machine (pytest-queryguard 0.5.0, pytest-querycount 0.4.0, queryspy
0.6.0), with its method and the script `scripts/compare/run.py`, is in
[Performance](performance.md#comparison-with-other-plugins): +1.8 to +2.4%
for pytest-queryguard on the same suite, +27 to +29% for pytest-querycount
and +40 to +43% for queryspy.

### What I did not measure

SQLite for the index check (the page for it has its own tests), MySQL,
PostgreSQL versions other than 17, async drivers in this comparison, memory,
other SQLAlchemy versions, and any real application. Speed was measured on
one machine, above; the overhead gate in `scripts/bench_overhead.py` covers
pytest-queryguard only.

## Choosing

- You want a zero-config gate in pytest with Core and `text()` coverage,
  per-statement failure messages and an index check: pytest-queryguard is
  built for that, and it has a baseline and SARIF for adoption.
- You also want N+1 findings in a running FastAPI, Starlette, Flask or WSGI
  app: both pytest-queryguard (its [in-application mode](in-app.md),
  experimental since 0.5.0) and queryspy have ASGI and WSGI middleware and a
  request panel; the two were not compared on running apps. I did not test
  running queryspy together with pytest-queryguard.
- You use queryspy in tests and want to move: see
  [Migrating from queryspy](migrating-from-queryspy.md).
- You want budgets written for you or one global ratchet: pytest-querycount
  (it and pytest-queryguard both register a `max_queries` marker; I did not
  test installing both).
- You are on Django: django-zeal, or django-nplus1 or django-query-doctor
  (read, not run; see above).
- You want to assert exact SQL: pytest-capquery, after checking its license.
- You have nplusone on an old SQLAlchemy: see
  [Migrating from nplusone](migrating-from-nplusone.md).
