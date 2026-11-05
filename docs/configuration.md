# Configuration reference

| Option | Values | Default | Meaning |
|---|---|---|---|
| `queryguard_mode` | `fail`, `warn`, `off` | `fail` | `fail` fails a test with N+1. `warn` reports it as a `QueryGuardWarning` and lets the test pass. `off` turns detection off; tests without a budget are not measured. |
| `queryguard_relationship_threshold` | integer ≥ 2 | `2` | Number of different parent objects for which one relationship must be lazy-loaded to count as N+1. |
| `queryguard_repeat_threshold` | integer ≥ 2 | `5` | Number of times one normalized `SELECT` must run to count as N+1. Schema reflection (`PRAGMA`, `sqlite_master`, `pg_catalog`, `information_schema`) is not counted, see [how detection works](how-detection-works.md#repeated-statement). |
| `queryguard_small_repeat_threshold` | `0` or integer ≥ 2, below the repeat threshold | `0` (off) | Number of runs of one statement from one source line, each with different parameters, that count as N+1 although they are below `queryguard_repeat_threshold`. Pagination and writes are not counted. |
| `queryguard_repeat_scope` | `test`, `session` | `test` | What the repeat, relationship, column-load and write-loop rules count together. `test` adds up everything one test runs. `session` counts each SQLAlchemy `Session` (or, for a connection no `Session` opened, each connection checkout) on its own, which is what a test that makes several independent requests needs. See [Tests with several sessions or requests](several-sessions.md). |
| `queryguard_write_loop_threshold` | `0` or integer ≥ 2 | `0` (off) | Number of single-row `INSERT`, `UPDATE` or `DELETE` statements of one shape from one source line that count as a write loop (kind `write-loop`). |
| `queryguard_strict` | bool | `false` | Raise `NPlusOneDetected` at the lazy load that makes an N+1, with the test's traceback, instead of reporting it when the test ends. See [A test failed](when-a-test-fails.md#strict-mode-fail-at-the-access). |
| `queryguard_cartesian_load` | `off`, `warn`, `fail` | `warn` | Reports a SELECT that joins two collections side by side, whose rows grow as their product (kind `cartesian-load`, see [How detection works](how-detection-works.md#cartesian-load)); `warn` only warns, `fail` fails the test in `fail` mode. |
| `queryguard_unused_eager_load` | `off`, `warn`, `fail` | `off` | Experimental (exempt from the [deprecation policy](stability.md#experimental)). Reports relationships a test eager-loads and never reads (kind `unused-eager-load`); `warn` only warns, `fail` fails the test in `fail` mode. See [docs/unused-eager-load.md](unused-eager-load.md). |
| `queryguard_fail_on` | kinds or `fnmatch` patterns, one per line | empty (all kinds) | The finding kinds that fail a test in `fail` mode; the other kinds are reported as `QueryGuardWarning` and the test passes. For example `relationship-load` alone. Budgets always fail. |
| `queryguard_column_load_threshold` | integer ≥ 2 | `5` | Number of different instances of one class that must load deferred or expired columns on their own to count as N+1. |
| `queryguard_max_events` | integer ≥ 0 | `10000` | Statements (and relationship loads) stored per test for reports. Beyond it they are only counted, and the report says so. `0` stores everything. |
| `queryguard_ignore` | patterns, one per line | empty | Findings to leave out of reports: `KIND LABEL [file=GLOB] [function=GLOB]`, see [Opting out](ignoring.md). |
| `queryguard_baseline` | path | empty | Baseline file, relative to the rootdir: findings listed in it do not fail or warn, see [Baseline](baseline.md). |
| `queryguard_budgets` | path | empty | Budget file, relative to the rootdir: each test listed in it fails above its number of queries, see [Budgets](budgets.md#lock-todays-counts-the-budget-file). |
| `queryguard_max_queries` | integer | empty | Default query budget of every test without a `max_queries` marker or an entry in the budget file. |
| `queryguard_index_check` | `true`, `false` | `false` | Checks each `SELECT` for a missing index (PostgreSQL 16 or newer, and `SELECT`, `UPDATE` and `DELETE` on SQLite, MySQL and MariaDB). |
| `queryguard_index_advisor` | `true`, `false` | `true` | Suggests an index for each missing-index finding, see [Index advisor](index-advisor.md). Used with the index check. |
| `queryguard_hypopg` | `auto`, `on`, `off` | `auto` | Verifies the suggested indexes with the hypopg extension: `auto` when it is installed, `on` also creates it inside the check's savepoint, `off` never uses it. |
| `queryguard_index_ignore_tables` | table names or `fnmatch` patterns, one per line | empty | Tables the index check ignores. A pattern matches `table` or `schema.table`, quoted or not, for example `countries`, `"UserAccount"`, `lookup_*` or `audit.*`; the spellings are the same as in `queryguard_ignore` and `allow_nplusone`, see [Ignoring findings](ignoring.md). |

Any invalid value stops pytest with a usage error.

The options with a fixed set of values (`queryguard_mode`, `queryguard_repeat_scope`, `queryguard_unused_eager_load`, `queryguard_cartesian_load`, `queryguard_hypopg`) and `--queryguard-mode` ignore case and surrounding spaces: `WARN`, `Warn` and `warn` mean the same.

In `pyproject.toml`:

```toml
[tool.pytest.ini_options]
queryguard_mode = "warn"
queryguard_relationship_threshold = 3
queryguard_repeat_threshold = 10
queryguard_index_check = true
queryguard_index_ignore_tables = ["countries", "lookup_*"]
```

With pytest 9 you can use the native TOML table instead:

```toml
[tool.pytest]
queryguard_mode = "warn"
queryguard_relationship_threshold = 3
queryguard_repeat_threshold = 10
queryguard_index_check = true
queryguard_index_ignore_tables = ["countries", "lookup_*"]
```

`pytest.ini` and `setup.cfg` work as usual.

The warning classes, `pytest_queryguard.QueryGuardWarning` for N+1 and
missing indexes in `warn` mode,
`pytest_queryguard.QueryGuardInternalWarning` for an internal error and
`pytest_queryguard.QueryGuardDeprecationWarning` for an option or name that a
later release may remove (a `DeprecationWarning`, not a `QueryGuardWarning`),
work with pytest's usual warning filters. For example, to turn the findings
of `warn` mode back into failures for one job:

```toml
[tool.pytest.ini_options]
queryguard_mode = "warn"
filterwarnings = ["error::pytest_queryguard.QueryGuardWarning"]
```

A blanket `filterwarnings = error` has the same effect: in `warn` mode it makes
every finding fail its test, like `fail` mode. The failure shows the finding
without the plugin's own source.

An "error" filter never applies to `QueryGuardInternalWarning`: a bug in the
plugin must not fail a test, so with `-W error` or `filterwarnings = error`
that warning is still shown in the warnings summary, not raised. An "ignore"
filter hides it as usual; the "queryguard: measurement stopped" section of the
summary is printed either way.

Command-line options:

| Option | Meaning |
|---|---|
| `--queryguard-mode=fail\|warn\|off` | Overrides `queryguard_mode` for this run. |
| `--queryguard-report` | Prints the 10 tests with the most queries at the end of the session. |
| `--queryguard-baseline=PATH` | Baseline file for this run; overrides `queryguard_baseline`. |
| `--queryguard-sarif=PATH` | Writes the findings of this run to `PATH` as SARIF 2.1.0. |
| `--queryguard-json=PATH` | Writes each test's statement counts (total and by verb), database time and findings to `PATH` as JSON, to compare runs. See [CI](ci.md#query-counts-as-json). |
| `--queryguard-update-baseline` | Writes the findings of this run to the baseline file instead of reporting them. |
| `--queryguard-strict` | Turns `queryguard_strict` on for this run. |
| `--queryguard-budgets=PATH` | Budget file for this run; overrides `queryguard_budgets`. |
| `--queryguard-update-budgets` | Writes each test's query count of this run to the budget file. |
| `--queryguard-index-check` | Turns the missing-index check on for this run. |
| `--no-queryguard-index-check` | Turns the missing-index check off for this run, whatever `queryguard_index_check` says. |


## Markers

| Marker | Meaning |
|---|---|
| `max_queries(limit)` | Fails the test if it runs more than `limit` SQL statements. See [Budgets and timing](budgets.md). |
| `max_db_time(seconds)` | Fails the test if its statements take more than that many seconds in total. |
| `allow_nplusone(*labels, kind=..., file=..., function=...)` | Leaves out findings: all of them without arguments, or only those that match the `fnmatch` patterns. See [Ignoring findings](ignoring.md). |
| `allow_missing_index(*tables)` | Skips the missing-index check for the test, or only for the given tables. `allow_nplusone` without arguments does not affect the index check; with patterns it also matches missing-index findings, as in `allow_nplusone("public.audit_log", kind="missing-index")`. |

## Fixture

| Fixture | Meaning |
|---|---|
| `queryguard` | `queryguard.max_queries(limit)` and `queryguard.num_queries(expected)` blocks that count the statements of the test's own thread or task, and `queryguard.queries`, the statements of the test function. See [Budgets and timing](budgets.md#budgets-in-a-test-with-the-queryguard-fixture). |
