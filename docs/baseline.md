# Baseline for existing findings

A project that adopts pytest-queryguard late usually has findings it cannot
fix at once. A baseline file records them, so that:

- a finding that is in the file does not fail a test and does not warn;
- every other finding behaves as usual, so new N+1 problems still fail;
- an entry that no longer matches anything is listed, so the file can be
  pruned as the code gets fixed.

The baseline applies to the pytest plugin and to every finding kind
(`relationship-load`, `repeated-statement`, `column-load`, `write-loop`,
`unused-eager-load`, `unused-columns`, `cartesian-load` and `missing-index`). `QueryLog.assert_no_nplusone()` does not read it.

## Create it

```console
pytest --queryguard-baseline=queryguard-baseline.json --queryguard-update-baseline
```

The run writes the file and does not fail on findings. Budgets
(`max_queries`, `max_db_time`) are not findings: a test over its budget still
fails on this run and is never recorded, so fix the code or raise the budget.
Run the whole suite to create the file, and again to prune it. Commit the file.

### Partial runs

An update run is either full or partial, and only a full run removes every
entry nothing produced any more.

A run is **full** when it collects the whole suite and keeps every test: no
arguments, or path arguments that cover every `testpaths` entry (`pytest`,
`pytest tests`, `pytest tests/`, `pytest .`), or the rootdir when `testpaths`
is not set. An `--ignore` in the ini `addopts` is part of how the suite is
defined, like `collect_ignore`, so a run with it is still full.

A run is **partial** when any of these applies:

- `-k`, `-m` or `--deselect` left a test out, including a `-m` in `addopts`;
- a plugin or a `conftest.py` hook removed tests from the collected items;
- `--lf`, `--sw` or `--pyargs` is given;
- `--ignore` or `--ignore-glob` is on the command line or in `PYTEST_ADDOPTS`;
- the arguments are node ids, or paths that leave out part of `testpaths`
  (`pytest tests/api` with `testpaths = tests`, or `pytest tests` with
  `testpaths = tests other`), or pytest runs from a subdirectory that does
  not hold the whole suite.

A partial run only replaces the entries of the tests that ran:

- a test that ran and produced a finding adds its entry;
- a test that ran and no longer produces a finding loses its entry;
- an entry whose test did not run is kept as it was.

The summary says how many entries were kept:

```
queryguard-baseline.json: 3 entries (0 added, 0 removed)
this run did not cover the whole suite: 2 entries kept because their tests did not run
```

An entry's `file` and `function` are the call site of its finding, usually
in application code (`app/orders.py`, `list_orders`). The rule above applies
only to an entry whose call site is inside a test function, which ties it to
that test. An entry for code outside a test (a helper or application module,
or an entry with no file) is kept on a partial run unless a finding of the
same identity shows up again; a full run prunes it. The same holds for a deleted or renamed
test: a partial run cannot tell it from one that did not run, so its entry is
removed on the next full run. Tests skipped before their call phase count as
not run. If the summary of your usual full run says it did not cover the whole
suite (a `-m` in `addopts`, for example), run once without that option to
prune the file.

Runs that stop early (`-x`, `--maxfail`, Ctrl-C, a crashed xdist worker),
runs with a collection error and `--collect-only` do not write the file at
all. A collection error counts also when the run goes on, with
`--continue-on-collection-errors` or under pytest-xdist: the tests of the
module that did not collect never ran, and a rewrite would drop their
entries. Fix the module, then update.

Then turn it on for everyone, in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
queryguard_baseline = "queryguard-baseline.json"
```

or `--queryguard-baseline=PATH` on the command line, which wins over the ini
option. The ini path is relative to pytest's rootdir, the command-line path to
the directory pytest was started from.

## What an entry is

An entry is the identity of a finding: kind, label, file and function.

```json
{
  "version": 1,
  "findings": [
    {
      "kind": "relationship-load",
      "label": "Order.customer",
      "file": "app/orders.py",
      "function": "list_orders"
    }
  ]
}
```

- `file` is relative to the rootdir, with forward slashes, so the file works
  on every operating system and from any directory.
- Line numbers and counts are not part of an entry. Moving the code, or
  loading more rows, keeps the finding baselined.
- A finding in another function, or with another label, is a different
  finding and is reported.
- `file` and `function` are `null` when the call site is unknown (some
  missing-index findings).
- A missing-index finding on a partitioned table names the partitioned table,
  not its partitions. A baseline recorded before that change, with a partition's
  name in the label, no longer matches: the finding is reported once under the
  table's name until the baseline is updated. Run once with
  `--queryguard-update-baseline`, or replace the partitions' entries in the
  file by one entry with the partitioned table's name in the label.
- Findings that the `queryguard_ignore` option or marker leaves out are not
  written to the baseline.
- Two parts of an entry depend on the environment. The label of a
  `repeated-statement` finding is the normalized SQL as SQLAlchemy renders it,
  and SQLAlchemy 2.0 renders column aliases (`customer.id AS customer_id`)
  where 2.1 does not, so a baseline recorded with one series may not match the
  other. The function name of a finding in a list comprehension is
  `<listcomp>` on Python 3.10 and 3.11 and the enclosing function from 3.12 on.
  Record the baseline with the versions your CI uses, or write code under
  baseline with plain loops. (Both were seen when the examples in
  `examples/baseline-sarif` ran in CI on several Python and SQLAlchemy
  versions.)
- The entries are sorted and the file ends with a newline, so running the
  update twice gives the same bytes and a diff shows only real changes.

## Stale entries

```
=========== queryguard: stale baseline entries ===========
1 baseline entry in queryguard-baseline.json matched no finding in this run:
  relationship-load Order.items (app/orders.py, list_orders)
They are fixed, or this run did not cover them. After a full run, prune them
with --queryguard-update-baseline.
```

This is a notice and does not fail the run. After a partial run (`pytest
tests/one_file.py`, `-k`, `--lf`, `-x`, Ctrl-C, a collection error or
`--collect-only`) the list holds only entries of tests that ran, as a partial
update would replace them; an entry of a function outside the tests that ran
is not listed. The list is not shown when measurement is off, or after an
internal error or a crashed worker.

## Errors

A missing file, invalid JSON, an unknown `version` or an entry with the wrong
shape stops pytest with a usage error that names the file and the problem.
`--queryguard-update-baseline` replaces a missing or unreadable file. It does
not write after `-x`/`--maxfail` stopped the run, after an internal error, after
a worker crashed, or after an interrupted run (a collection error, Ctrl-C, a
usage error or an internal pytest error), and says so in the summary. A
collection error counts as an interrupted run also with
`--continue-on-collection-errors`.

## pytest-xdist

Each worker reads the file and filters its own findings. The workers send the
identities they saw to the controller, which finds the stale entries and, on
an update, writes the file once. The output is the same as a serial run.
