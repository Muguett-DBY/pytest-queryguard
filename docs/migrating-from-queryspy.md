# Migrating from queryspy

[queryspy](https://github.com/sqla-native/queryspy) and pytest-queryguard
both find N+1 queries and enforce query budgets for SQLAlchemy 2 in pytest.
This page maps each queryspy setting to its pytest-queryguard equivalent, as
of queryspy 0.5.0 (read in its installed package on 2026-10-13) and
pytest-queryguard 0.4.0. It is written by the author of pytest-queryguard;
[Comparison](comparison.md) has the measured differences, including the ones
where queryspy is ahead.

Before you move: queryspy also runs in your application, as ASGI and WSGI
middleware with a request panel. pytest-queryguard runs only in tests. If you
use the middleware, keep queryspy for it; running both plugins in one pytest
session was not tested (both read SQLAlchemy events, and both register a
marker of their own, so the names do not clash).

## The main difference in defaults

queryspy reports N+1 findings and fails no test on them unless you pass
`--queryspy-strict` or set `queryspy_fail_on` (its budgets fail either way).
pytest-queryguard fails a test on any finding with no configuration. To keep the queryspy behaviour while you
migrate, start in warn mode and switch to `fail` once the suite is clean or
baselined:

```toml
[tool.pytest.ini_options]
queryguard_mode = "warn"
```

## Configuration

| queryspy 0.5.0 | pytest-queryguard |
|---|---|
| `--queryspy-strict` | Nothing: the default `queryguard_mode = fail` fails on every finding kind |
| `queryspy_fail_on = none` (the default: report only) | `queryguard_mode = warn` |
| `queryspy_fail_on = n_plus_one` (every kind) | The default; `queryguard_fail_on` empty |
| `queryspy_fail_on = lazy_load, column_load` | `queryguard_fail_on = ["relationship-load", "column-load"]`; the other kinds become warnings |
| `queryspy_budget = 20` (every test) | No suite-wide budget option. Add the marker in `conftest.py`, see below, or set `max_queries` per test |
| `queryspy_capture_stacks = false` | No equivalent: the call site is always recorded |
| `--queryspy-report=queryspy.sarif` | `--queryguard-sarif=queryguard.sarif` ([SARIF](sarif.md)) |
| `--queryspy-report=queryspy.json` (JSON) | No JSON report. `--queryguard-report` prints the tests with the most statements; the baseline file lists findings as JSON |
| `--queryspy-baseline=PATH` | `--queryguard-baseline=PATH`, or `queryguard_baseline` in the configuration |
| `--queryspy-baseline-update` | `--queryguard-update-baseline` |
| The queryspy GitHub Action | The [action in this repository](ci.md) |

## Finding kinds and thresholds

| queryspy | pytest-queryguard | Default threshold here |
|---|---|---|
| `lazy_load` | `relationship-load` | 2 parent objects (`queryguard_relationship_threshold`) |
| `column_load` | `column-load` | 5 instances (`queryguard_column_load_threshold`) |
| `repeated_statement` | `repeated-statement` | 5 runs (`queryguard_repeat_threshold`) |
| not reported | `write-loop`, opt-in | off (`queryguard_write_loop_threshold`) |
| not reported | `missing-index`, opt-in | off (`queryguard_index_check`) |

queryspy uses one threshold of 2 for every kind. pytest-queryguard reports a
repeated statement and column reloads from 5, so a loop of 2 to 4 statements
that queryspy reported passes here. To get closer to queryspy, set
`queryguard_small_repeat_threshold = 2`, which reports 2 to 4 runs from one
source line with different parameters, or lower the thresholds themselves:

```toml
[tool.pytest.ini_options]
queryguard_small_repeat_threshold = 2
queryguard_column_load_threshold = 2
```

## Markers and helpers

| queryspy | pytest-queryguard |
|---|---|
| `@pytest.mark.queryspy(max_queries=3)` | `@pytest.mark.max_queries(3)` |
| `@pytest.mark.queryspy(allow_n_plus_one=True)` | `@pytest.mark.allow_nplusone`, or with patterns to allow only some findings: `@pytest.mark.allow_nplusone("Order.customer")` |
| `@pytest.mark.queryspy(threshold=3)` | No per-test threshold marker: set the ini option, or use `record_queries()` with `assert_no_nplusone(relationship_threshold=3, repeat_threshold=3)` |
| `@pytest.mark.queryspy(fail_on=...)` | No per-test equivalent; `queryguard_fail_on` applies to the suite |
| `with assert_max_queries(2):` | `with max_queries(2):` (raises `QueryBudgetExceeded`, an `AssertionError`) |
| `with assert_num_queries(2):` (exact) | `with record_queries() as log:` and then `assert len(log) == 2` |
| `with no_n_plus_one():` | `with record_queries() as log:` and then `log.assert_no_nplusone()` |
| `with record() as recorder:` | `with record_queries() as log:` ([Reading the queries](python-api.md)) |
| `with queryspy.ignore():` in application code | `with pytest_queryguard.ignore("*"):`, or name the kinds; threads started inside the block are not covered ([the `ignore()` block](ignoring.md#the-ignore-block)) |
| `QuerySpyMiddleware` (ASGI, WSGI), the panel | Not covered: tests only |

The helpers, side by side. They run against the fixed `fetch_customers`, the
one in `app/customers_fixed.py` of [the first test](getting-started.md#a-first-test),
which loads the three customers with one `IN` query; the `app.customers`
module of that page is the N+1 version, and both tests below fail against it:

```python
from pytest_queryguard import max_queries, record_queries

from app.customers_fixed import fetch_customers


def test_fetch_customers_is_one_query(session):
    with record_queries() as log:
        fetch_customers(session, [1, 2, 3])
    assert len(log) == 1  # queryspy: assert_num_queries(1)
    log.assert_no_nplusone(repeat_threshold=2)  # queryspy: no_n_plus_one()


def test_fetch_customers_budget(session):
    with max_queries(2):  # queryspy: assert_max_queries(2)
        fetch_customers(session, [1, 2, 3])
```

`assert_no_nplusone()` does not read the ini thresholds; pass them as
arguments. Its defaults are 2 for relationships, 5 for repeats and 5 for
column loads; the thresholds for small repeats and write loops are off unless
passed (see [Reading the queries](python-api.md)).

## A budget for every test

`queryspy_budget` has no ini equivalent. A hook in `conftest.py` gives every
test without its own budget the same one; a test with a `max_queries` marker
keeps its own:

```python
import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        if item.get_closest_marker("max_queries") is None:
            item.add_marker(pytest.mark.max_queries(20))
```

## The baseline

The two baseline files have different formats, and an entry is not the same
identity: pytest-queryguard keys on kind, label, file and function, with paths
relative to pytest's rootdir. Record a new one with
`pytest --queryguard-baseline=queryguard-baseline.json --queryguard-update-baseline`
on a full run ([Baseline](baseline.md)). One difference to know: queryspy
removes `AS` column aliases from labels, so one baseline works on SQLAlchemy
2.0 and 2.1; a pytest-queryguard `repeated-statement` label keeps them, so
record the baseline with the SQLAlchemy version your CI uses.

## Steps

1. `pip uninstall queryspy` (unless you keep its middleware), then
   `pip install pytest-queryguard`.
2. Translate the configuration with the tables above; start with
   `queryguard_mode = "warn"` if you ran queryspy without `--queryspy-strict`.
3. Replace `@pytest.mark.queryspy(...)` markers and the `queryspy` helpers.
4. Run the full suite, read the findings ([A test failed: now
   what](when-a-test-fails.md)), and record a baseline for those you keep.
5. Switch to `queryguard_mode = "fail"` (or remove the line) and update the
   CI job ([CI and GitHub Action](ci.md)).
