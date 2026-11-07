# Stability and versioning policy

pytest-queryguard follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
This page says what that covers, how deprecations work, and which tests
enforce it.

## What is public

The public API is everything below. Changing it incompatibly needs a major
release (see "Version numbers"); `tests/test_public_api.py` fails when it
changes by accident.

- The names in `pytest_queryguard.__all__`, their signatures, and the public
  attributes and methods of the classes in it (`QueryLog`, `Query`,
  `CallSite`, `QueryGuardFixture`, the exceptions and the warning classes). A new keyword argument
  with a default, or a new attribute, is not a breaking change.
  Each of these reports `pytest_queryguard` as its `__module__`, so tracebacks,
  reprs, pickles and `filterwarnings` entries use the documented import path
  (`pytest_queryguard.NPlusOneDetected`, not the submodule it lives in).
- The pytest integration: the ini options (`queryguard_*`), the command-line
  flags (`--queryguard-*`, `--no-queryguard-*`), their defaults and accepted
  values, the markers (`max_queries`, `max_db_time`, `queryguard_max_queries`,
  `queryguard_max_db_time`, `queryguard_ignore` and its alias `allow_nplusone`,
  `allow_missing_index`) and the `queryguard` fixture.
- The meaning of the finding kinds and of the labels and patterns used by
  the `queryguard_ignore` option and marker, so that an existing ignore keeps
  matching the same finding.
- The pytest entry point name (`queryguard`).
- The files the plugin writes and reads, and the command-line tools and the
  GitHub Action that work with them, as listed under
  [Files, tools and the action](#files-tools-and-the-action).
- Supported versions: Python, SQLAlchemy and pytest releases listed in
  `pyproject.toml` and the CI matrix. Dropping a Python, SQLAlchemy or pytest
  series is announced in the changelog and happens in a minor release at the
  earliest after the series is end of life upstream.

### Files, tools and the action

Each file has a number for its format. A reader rejects a number it does not
know, with an error that says so. New keys may be added in a minor release
(read a file by the keys you need and ignore the others). Changing the meaning
of a key, removing one, or changing what identifies a finding is an
incompatible change: the number goes up, and from 1.0 on it needs a major
release.

| What | Format key | Public | Not public |
|---|---|---|---|
| Baseline file (`--queryguard-baseline`, see [Baseline](baseline.md)) | `"version": 1` | `findings`, each with `kind`, `label`, `file`, `function` (the identity of a finding) | the order of the entries |
| Budget file (`--queryguard-budgets`, see [Budgets](budgets.md#lock-todays-counts-the-budget-file)) | `"format": 1` | `budgets`: test node id to a number of queries | the order of the entries |
| Query counts (`--queryguard-json`, see [CI](ci.md#query-counts-as-json)) | `"format": 1` | the keys documented there: `tool`, `complete`, and per test `statements`, `by_verb`, `db_time`, `truncated`, `max_queries_marker`, `findings` (`kind`, `label`, `count`, `file`, `function`, `line`, `stack`, `baselined`) | `tool.version`, the order of tests and findings |
| SARIF file (`--queryguard-sarif`, see [SARIF](sarif.md)) | SARIF 2.1.0 | the rule ids `queryguard/<kind>`, one per finding kind; the fingerprint key `queryguardIdentity/v1` in `partialFingerprints` and the identity it hashes, so code scanning alerts stay the same alerts after an upgrade; the result `properties` `count`, `label`, `tests` and `peakCount` | rule names, descriptions and help, message text, `primaryLocationLineHash`, `codeFlows`, `fixes` (experimental, see below) |

A new fingerprint key (`queryguardIdentity/v2`) would close every open alert
and open it again, so it is treated as a removal: from 1.0 on, only in a major
release.

The command-line tools:

- `python -m pytest_queryguard.compare BASE HEAD [--output PATH] [--rows N]
  [--key NAME]` (see [CI](ci.md#query-counts-on-pull-requests)): its
  arguments, its exit status (0, or 2 when a file cannot be read) and the
  marker on the first line of its report (`<!-- pytest-queryguard compare -->`,
  with the `--key` before `-->`), by which the action finds its own comment,
  are public. The rest of the Markdown report is written for people and is
  not.
- `python -m pytest_queryguard.trend` and `python -m pytest_queryguard.lint`
  are experimental, see below.

The GitHub Action (`action/action.yml`, see [CI](ci.md#the-action)): its
inputs with their defaults and accepted values, its outputs (`exit-code`,
`sarif-file`, `json-file`), and the name of the artifact it uploads,
`queryguard-json-<category>`, which later runs of older and newer versions of
the action download, and the pull-request comment it updates in place (found
by the compare marker above). The action is versioned with the package: pin it to a
release tag or a commit. The `trend` input and the `trend-report` output are
experimental. What the job summary and the pull-request comment say is not
public.

## What is not public

- Everything in a module whose name starts with an underscore, and any name
  not exported from `pytest_queryguard`. The submodules (`collector`,
  `listeners`, `detectors`, `indexes`, `messages`, `plugin`, ...) are
  implementation details even though they can be imported.
- The wording and layout of failure messages, warnings, summaries and
  reports. They are written for people and improve between releases; do not
  parse them. Use `QueryLog`, the SARIF file, the baseline file or the
  `--queryguard-json` file when a program needs the data.
- Which statements SQLAlchemy emits for a given ORM call; that depends on the
  SQLAlchemy version.
- The index check beyond what the documentation promises: it is documented
  for PostgreSQL 16 and newer, SQLite, MySQL and MariaDB, and on older PostgreSQL servers
  it only prints a notice. It is tested on PostgreSQL 16 to 18; on a newer
  server it runs and prints a notice that the version is untested.

## Version numbers

- Patch (`x.y.Z`): bug fixes and documentation. No new public API, no change
  to defaults. A fix can make a test report a finding it missed before;
  that is a fix, not a breaking change.
- Minor (`x.Y.0`): new public API, new options with a default that keeps
  today's behaviour, new finding kinds that are off or reported as warnings
  first (from 1.0 on; see below for 0.x), deprecations, and dropping support for an end-of-life Python,
  SQLAlchemy or pytest series.
- Major (`X.0.0`): removals of deprecated API, changed defaults, and
  incompatible changes to anything listed under "What is public".

While the version is below 1.0, a minor release may also contain breaking
changes. They are still deprecated first whenever that is possible, and are
always listed under "Changed" or "Removed" in the changelog.

**Pre-1.0 clause for new checks.** While the version is 0.x, a minor release
may change the defaults of newly added detectors: switch a new check on, or let
it fail tests by default, instead of introducing it off or as a warning first.
Every such change is named in the introduction at the top of the changelog
entry and listed first under "Changed", in a "Behavior changes" group, with the
option that turns it off. The
0.4.0 changelog entry is an example: the `column-load` kind is on by default, the index
advisor is on whenever the index check is on, and the index check fails tests
on SQLite, MySQL and MariaDB. From 1.0 on, the rules above apply strictly: a new check is off or
only warns until a later minor release, and a change of default needs a major
release.

## Deprecation policy

1. The old name or behaviour keeps working and starts emitting a
   `QueryGuardDeprecationWarning` (a `DeprecationWarning` subclass, exported
   from `pytest_queryguard`) that names the replacement and the release in
   which it will be removed. It is not a `QueryGuardWarning`, so a
   `filterwarnings` entry that turns findings into errors, or ignores them,
   does not catch it; pytest shows `DeprecationWarning` in the warnings
   summary by default. Command-line flags and ini options print the same
   warning once per run.
2. The deprecation is listed under "Deprecated" in the changelog, and the
   documentation points to the replacement.
3. It stays for at least one minor release (two, from 1.0 on) and at least
   three months after the release that deprecated it, whichever is longer.
4. It is removed in the next major release (before 1.0: in a minor release),
   listed under "Removed".

A security or data-loss fix may skip these steps; the changelog says so.

A new finding kind, or a stricter version of an existing check, first appears
in a release where it can be turned off with an ini option, and the changelog
says how. Before 1.0 it may be on by default (see the pre-1.0 clause above).

## Experimental

Options, finding kinds, markers, tools and files listed here are public to use but exempt
from the deprecation policy: a minor release may change their values,
defaults, labels or messages, or remove them, with a changelog entry under
"Changed" or "Removed" and no deprecation period. Their ini options and
command-line flags are marked `EXPERIMENTAL` in `pytest --help`.

- `queryguard_unused_eager_load` and the `unused-eager-load` finding kind
  (rule `queryguard/unused-eager-load` in SARIF). See
  [Unused eager loads](unused-eager-load.md).
- `queryguard_unused_columns` and the `unused-columns` finding kind (rule
  `queryguard/unused-columns` in SARIF), with its thresholds. See
  [Unused columns](unused-columns.md).
- The missing-index check on MySQL and MariaDB (tested in CI on MySQL 8.4 and
  MariaDB 11.8; see [the MySQL and MariaDB page](index-check-mysql.md)).
- The in-application mode: every name in `pytest_queryguard.app` (`watch`,
  `unwatch`, `is_watched`, `QueryGuardASGI`, `QueryGuardWSGI`, `track`,
  `set_route`, `Settings`, `RequestReport`, `Finding`, `Query`, `CallSite`,
  `internal_error`, `opentelemetry_reporter`, `sentry_reporter`) and the
  panel's JSON. See
  [In-application mode](in-app.md).
- The fix patch (since 0.8.0): `queryguard_show_patch`,
  `--queryguard-show-patch`, `--queryguard-write-patch`, which findings get a
  patch, the content of the diff, and the `fixes` entries of the SARIF file.
  See [A patch for the parent query](when-a-test-fails.md#a-patch-for-the-parent-query).
- The query-count trend (since 0.8.0): `python -m pytest_queryguard.trend`,
  its options, its output (Markdown, text and JSON) and exit status, and the
  GitHub Action's `trend` input and `trend-report` output. It reads
  `--queryguard-json` files, whose format is public. See
  [CI](ci.md#query-counts-over-the-last-runs).
- The static check (since 0.8.0), `python -m pytest_queryguard.lint`: its options, what it
  reports, its text output, and the rule `queryguard/static-relationship-load`
  in its SARIF output. Its JSON output has a `format` key; a change to the
  documented keys increments it. See [Static check](static-check.md).
- The MCP server, `python -m pytest_queryguard.mcp`: its tools, their
  arguments and the fields of their results. See
  [MCP server](mcp.md).

Everything else listed under "What is public" follows the deprecation policy.
`tests/test_public_api.py` still snapshots the experimental ini options and
their defaults, and the names, signatures and fields of `pytest_queryguard.app` and
the keys of the panel's JSON, so a change to them is deliberate and visible in
review.

## Failing open

A bug inside pytest-queryguard must never change the outcome of a test
whose result the plugin does not decide, error a test, or end the session
with an `INTERNALERROR`:

- An exception (any `Exception`, including `MemoryError` and
  `RecursionError`) in a listener, a detector, a report writer, the index
  check or a pytest hook stops the measurement for the rest of the session.
  It is reported once: one `QueryGuardInternalWarning` in the warnings
  summary and a "queryguard: measurement stopped" section that names the
  step and the line that raised. Errors while writing the baseline or SARIF
  file are reported in their own section instead. A warnings "error" filter
  does not turn the warning into a failure. Tests that the plugin would have
  failed (N+1, missing index, budgets) may pass after that point.
- The unused eager-load and unused-columns checks turn only themselves off,
  each with its own warning.
- `KeyboardInterrupt`, `SystemExit` and `asyncio.CancelledError` are signals,
  not plugin bugs: they are never swallowed. The plugin still takes its
  collector off the stack, and an interrupted run does not rewrite the
  baseline.
- With pytest-xdist, an error while a worker hands its findings to the
  controller marks the run as incomplete: the baseline is not rewritten and
  the SARIF summary says that findings may be missing.
- With pytest-xdist, each worker stops and reports on its own. An error in
  a worker reaches the controller as its `QueryGuardInternalWarning` (pytest
  lists the same message once, under every test that raised it) and marks
  the run as incomplete; the "queryguard: measurement stopped" section is
  written only for an error in the controller process.

`tests/test_fault_injection.py` makes each of these steps raise in turn and
checks the outcome of every test of a small suite.

## How this is enforced

- `tests/test_public_api.py` snapshots `__all__`, signatures, dataclass
  fields, ini option names and defaults, command-line flags, markers and the
  entry point. A failing snapshot means: the change is breaking, or it is an
  addition that must be added to the snapshot on purpose. Update the snapshot
  in the same commit as the change, so reviewers see it.
- The snapshot does not yet cover the files, the compare tool and the action
  listed under [Files, tools and the action](#files-tools-and-the-action).
  Their own tests (`tests/test_baseline.py`, `test_budget_file.py`,
  `test_testjson.py`, `test_sarif.py`, `test_compare.py`,
  `test_action_args.py`) check how they behave; a change to them is reviewed
  against that section.
- CI runs the suite on Python 3.10 to 3.15,
  SQLAlchemy 2.0 and the latest release, the oldest and the latest pytest,
  PostgreSQL 14 to 18, Windows and macOS, and under pytest-xdist. A nightly
  job tries the SQLAlchemy development branch, pytest pre-releases and Python
  3.16; it is allowed to fail and tells maintainers about upcoming
  incompatibilities early.
