# Performance

What pytest-queryguard costs when it is on, how that was measured, and what was
done to lower it. The numbers come from one laptop and one afternoon; treat them
as an order of magnitude and run the scripts on your own suite.

## What the plugin does per statement

With measurement on, every statement passes through a few listeners:

- `before_cursor_execute`: finds the call site (a walk up the stack to the first
  frame outside SQLAlchemy, pytest, the standard library and site-packages),
  tells lazy loads and column loads apart from ordinary queries by the ORM's
  options on the statement, builds one event and starts a timer.
- `after_cursor_execute`: stops the timer.

Once per test the collected statements are normalized and grouped by the
detectors. The missing-index check, the index advisor and eager-load tracking
are off by default and cost nothing until turned on; their costs are in the
[FAQ](faq.md#does-it-slow-my-tests-down).

Part of the cost is not the plugin's own code. Registering any listener on
these events makes SQLAlchemy take its slower event-dispatch path for every
statement. In the benchmark below, four listeners that do nothing add about 6
microseconds per Core statement and about 8 per ORM statement, before any
plugin code runs. That part cannot be removed without giving up the events.

## Method

- Machine: Intel Core i5-13500H, 16 threads, Linux 7.0, Python 3.13.13,
  SQLAlchemy 2.1.1, pytest 9.1.1. Measurements were pinned to one core
  (`taskset`) and each configuration ran in a fresh process.
- Micro-benchmark: `scripts/bench_overhead.py --statements 5000 --runs 15`.
  Core `select` by key and ORM `select` by primary key against in-memory
  SQLite, measurement off and on, the two interleaved, median of 15 runs.
- Whole suite: `scripts/bench_suite.py --runs 7`. A generated suite of 300
  tests; each test runs two Core statements, four ORM queries (one with
  `selectinload`, one lazy load), an insert with a commit, and a delete with a
  commit. It runs against a file-based SQLite database and against PostgreSQL
  16.15 in a local Docker container (driver pg8000 1.31.5), once with
  `-p no:queryguard` and once with the plugin (default settings, mode `warn`).
  The time is taken inside pytest, from session start to session finish, so
  start-up and imports are not counted.
- Before and after are the same scripts run against `main` as of 0.4.0rc2 (the
  "before" checkout) and against the branch with the changes below.

Absolute times on this machine drifted by up to a factor of two between
sessions (the same micro-benchmark gave 41 and 71 microseconds per Core
statement with measurement off on different days), so only numbers measured
back to back are compared below.

## Micro-benchmark: in-memory SQLite

Added time per statement with measurement on, median of 15 runs, two rounds
each (both rounds shown):

| Statement | Measurement off | Before: added | After: added |
|---|---|---|---|
| Core `select` by key | 41 us | +10.5 us (+25 %), +11.3 us (+28 %) | +8.8 us (+21 %), +9.1 us (+22 %) |
| ORM `select` by primary key | 76 us | +38.1 us (+50 %), +37.0 us (+48 %) | +30.0 us (+39 %), +31.9 us (+42 %) |

The added cost fell by about 17 % in both loops. The percentages are large
because an in-memory SQLite statement is almost free. In the same session a
no-op listener floor of about +6 us (Core) and +8 us (ORM) is included in every
"added" figure.

Where the plugin's own part goes, per statement, measured by timing the
listeners in place: about 4 us in the `before_cursor_execute` listener (call
site, event, bookkeeping), 2 us in `do_orm_execute` and under 1 us in the timer
listener. Those figures are from the 0.4.0 work. After 0.6.0 the plugin dropped
its `do_orm_execute` listener: with any listener of that event SQLAlchemy
prepares each ORM statement twice, which cost more than the listener itself.
Strict mode and the unused eager-load check, both off by default, still add
one, so a run with either pays that cost again.

## A realistic case: a suite of 300 tests

| Backend | Wall clock, off | Wall clock, on | Added |
|---|---|---|---|
| File-based SQLite (local disk) | 38.0 to 39.0 ms/test | 37.9 to 39.6 ms/test | between -0.3 % and +2.2 % |
| PostgreSQL 16 (local container) | 12.6 to 13.8 ms/test | 12.6 to 13.2 ms/test | between -9 % and 0 % |

These are four independent runs of seven repetitions for each backend, two
against the code before and two after the changes. The differences are
smaller than the run-to-run noise: single runs on the same checkout differed by
10 % or more (fsync on a laptop disk, the database container), and the
plugin often came out "faster" than the run without it. The honest reading is
that on a suite that does real database work the overhead is below what this
setup can resolve, in the order of a few percent at most.

The CPU time of the test process (which leaves out waiting for the disk and the
server) is less noisy and gives a rough upper figure: between -10 % and +7 % of
the suite's CPU time, which is the same conclusion with a wider error bar.

A profile of the same suite (`cProfile`, 300 tests) puts the time spent in the
plugin's own functions at about 0.2 s in total, around 0.6 ms per test with the
profiler's own inflation, and that figure fell by roughly 10 % with the
changes below.

Two limits of this measurement: the suite is small and made up for the
benchmark, and the databases were on the same machine, so each statement was
cheaper than it would be over a network. Over a network the plugin's share
gets smaller, not larger.

## What changed

- Call sites are shared: one immutable `CallSite` per source location instead of
  a new object for every statement, and the frame classification is read
  inline from its cache instead of through a function call per frame.
- A statement is classified as a write once per execution, and without copying
  the statement text when it does not start with whitespace.
- `normalize()` remembers its most recent 2048 results; a suite repeats the
  same statement texts, and the detectors normalized each one for every test.

Behaviour is unchanged: the existing tests, the public API snapshot and the
accuracy corpus give the same results, and each change has a regression test.

## What did not help

- Registering the cursor listener with `retval=True` to skip SQLAlchemy's
  wrapper: no measurable change.
- Disabling Python's garbage collector during the loop: no change, so the
  retained events are not what costs time.

## How the overhead grew before this

The relative ORM overhead in the micro-benchmark rose from about 35 % to about
50 % between the overhead gate's first commit and 0.4.0rc2. There is no single
commit behind it: each feature that looks at every statement added one to
three microseconds (column-load detection, the ORM flush tag, the Session
scope, the autoflush attribution), and an in-memory SQLite statement is so
cheap that each step shows as a few percentage points. Run to run the same
commit varies by about 5 points, so a bisect needs repeated, pinned runs.

## Comparison with other plugins

pytest-queryguard 0.5.0, pytest-querycount 0.4.0 and queryspy 0.6.0, measured
on 2026-11-05 with Python 3.14.4, SQLAlchemy 2.1.1 and pytest 9.1.1 on the
machine above, by `scripts/compare/run.py`:

- Whole suite: the generated 300-test suite of `scripts/bench_suite.py`
  against file-based SQLite, 9 rounds. Each tool ran with the settings that
  make it report N+1: pytest-queryguard with `--queryguard-mode=warn`,
  queryspy with `--queryspy-strict` (stack capture on, its default),
  pytest-querycount with a `no_n_plus_one(threshold=3)` marker on every test.
- Per statement: a Core `select` by key, an ORM `select` by primary key and
  one lazy load, against in-memory SQLite (`scripts/compare/bench_micro.py`),
  15 processes per tool, the fastest of 5 blocks of 2000 statements in each,
  then the median.

Each tool has its own virtual environment with the same Python, pytest and
SQLAlchemy; every run is a fresh process pinned to one core, the tools'
runs are interleaved and their order reversed every round. Two complete
runs, one after the other (the cells show both when they differ):

| | pytest-queryguard | pytest-querycount | queryspy |
|---|---|---|---|
| 300-test suite, added time | +1.8 to +2.4 % | +27 to +29 % | +40 to +43 % |
| Core `select`, added per statement | +25 to +27 us | +108 to +112 us | +13 us |
| ORM `select`, added per statement | +55 to +66 us | +184 to +187 us | +172 to +176 us |
| Lazy load, added per statement | +100 to +102 us | +189 us | +208 to +209 us |

queryspy adds the least to a Core statement; on ORM statements, lazy loads
and the whole suite pytest-queryguard adds the least. The absolute
per-statement figures differ from the sections above because they are
another session; as said in [Method](#method), only numbers measured back to
back compare. A first, informal measurement on 2026-11-01 used a development
build of pytest-queryguard and earlier versions of the other two.

## Reproduce

```bash
uv run python scripts/compare/run.py           # the comparison above
uv run python scripts/compare/run.py --queryguard .   # this checkout instead of 0.5.0
uv run python scripts/bench_overhead.py --statements 5000 --runs 15
uv run python scripts/bench_suite.py --runs 7
uv run python scripts/bench_suite.py --runs 7 --backend postgres \
    --url postgresql+pg8000://user:password@localhost:5432/bench
```

`bench_suite.py --src` measures another checkout, so two versions can be
compared on the same machine; `scripts/bench_gate.py` is the CI check that
guards the micro-benchmark ratio.
