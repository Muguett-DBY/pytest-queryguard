# Running queryguard in CI and in GitHub code scanning

pytest-queryguard already fails the test run on a new N+1 query, so any CI
that runs pytest enforces it. To also see the findings in the GitHub UI, as
code scanning alerts with a file, a line and a fix hint, use the action in
this repository or upload the [SARIF file](sarif.md) yourself.

## The action

```yaml
name: Tests
on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  tests:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      security-events: write   # needed to upload to code scanning
      actions: read            # compare query counts with the base branch
      pull-requests: write     # comment the comparison on the pull request
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"
      - run: pip install -e ".[test]"        # your project and its test dependencies
      - uses: MaciejZiel/pytest-queryguard/action@v0.8.0   # a release tag, or a commit SHA
        with:
          pytest-args: tests/   # no -x: see below
          # baseline: queryguard-baseline.json   # add once the file exists, see below
```

The `push` trigger is limited to `main` so that a pull request does not run
the workflow twice, once for the push and once for the pull request.

`pytest-args` has no `-x`. A run that stops at the first failure writes a
JSON file with `"complete": false`, which the comparison with the base branch
cannot fully use (see [Query counts as JSON](#query-counts-as-json)), and it
keeps `--queryguard-update-baseline` and the budget file update from writing
(see [baseline.md](baseline.md)).

The example has no `baseline` input on purpose. A `baseline` path that does
not exist makes pytest stop with `baseline file ... does not exist; create it
with --queryguard-update-baseline`, and the action never creates the file. To
use a baseline, create it locally with
`pytest --queryguard-baseline=queryguard-baseline.json --queryguard-update-baseline`
(see [baseline.md](baseline.md)), commit it, and then uncomment the
`baseline` line.

You do not need to set `package`. Left empty, the action installs the plugin
from its own checkout, so the plugin version is the one that ref ships: pin the
action to a release tag or a commit and you get the plugin of that release. A
fixed default such as `pytest-queryguard` would let pip pick an older release
(for example 0.3.0, which has no `--queryguard-sarif`), and pytest would stop
with `unrecognized arguments`. Set
`package` only to install something else, such as `pytest-queryguard==0.7.0`
or a path. The action needs pytest-queryguard 0.6.0 or newer: it passes
`--queryguard-sarif` and `--queryguard-json` and runs
`python -m pytest_queryguard.compare`, which older releases do not have. The
`trend` input needs 0.8.0 or newer (`python -m pytest_queryguard.trend`).

The action installs `pytest-queryguard` into the active Python, runs
`pytest --queryguard-sarif=queryguard.sarif`, uploads the file with
[`github/codeql-action/upload-sarif`](https://github.com/github/codeql-action)
(pinned by commit SHA inside the action) and then fails the step if pytest
failed. Install your own project first: the action does not know how.

| Input | Default | Meaning |
|---|---|---|
| `pytest-args` | empty | Arguments for pytest, as a list of shell words: quotes group words (`-k "a or b"`). The value is split by Python's `shlex` and is not evaluated by a shell, so `$(...)`, backticks and `$VAR` are passed to pytest as literal text. |
| `python-version` | empty | Set up this Python. Empty: use the `python` on PATH. |
| `package` | empty | What to `pip install`. Empty: the plugin from the action's own checkout, matching the pinned ref. Otherwise a requirement like `pytest-queryguard>=0.6` (the oldest release the action works with; 0.8.0 for `trend`), or a path. |
| `baseline` | empty | Baseline file, see [baseline.md](baseline.md). Empty: `queryguard_baseline` from your config. |
| `fail` | `true` | `true`: fail the step when pytest fails. `false`: never fail, only report. |
| `working-directory` | `.` | Where to run pytest. Locations in the SARIF file are still relative to the repository root (see [SARIF output](sarif.md#paths-and-the-repository-root)). |
| `sarif-file` | `queryguard.sarif` | Output file, relative to `working-directory`. |
| `upload` | `true` | Upload to code scanning. `false`: only write the file. |
| `category` | `pytest-queryguard` | Code scanning category; use one per job when several jobs upload. It also names the job's query-count artifact and its pull-request comment. |
| `json-file` | `queryguard.json` | The [per-test query counts](#query-counts-as-json), relative to `working-directory`; uploaded as the artifact `queryguard-json-<category>`. Empty: none. |
| `compare` | `true` | On a pull request, compare the counts with the base branch and write the result to the job summary. |
| `comment` | `true` | Also post the comparison as a pull-request comment, updated in place on later pushes. |
| `trend` | `false` | Since 0.8.0, experimental. A number of runs N (2 to 100; `true` means 10): on pushes to the default branch and on pull requests, show how the counts moved over the last N runs, see [below](#query-counts-over-the-last-runs). |
| `github-token` | `github.token` | Token for the comparison, the trend and the comment. |

Outputs: `exit-code` (pytest's), `sarif-file`, `json-file` and `trend-report`
(since 0.8.0: the trend's Markdown file, empty when there is none).

### Query counts on pull requests

On every run the action writes the [query counts](#query-counts-as-json) and
uploads them as an artifact. On a pull request it downloads the file of the
latest finished run of the same workflow on the base branch (a push or
scheduled run, never a pull-request run: a fork's pull request from its own
`main` would otherwise count as the base), compares the
two, and writes the result to the job summary and to one pull-request
comment:

```
### pytest-queryguard: queries compared with the base branch

412 tests compared: 3120 -> 3187 statements (+67). 2 run more, 1 fewer; 1 new findings, 0 fixed.

#### New findings
| Kind | Label | Where | Times | Tests |
| relationship-load | `Order.items` | `app/orders.py in list_orders` | 50 | `tests/test_orders.py::test_list` |

#### Tests that run more statements
| Test | Before | After | Change |
| `tests/test_orders.py::test_list` | 3 | 53 | +50 |
```

A reviewer sees that a change made a test run more queries even when no
rule fired yet (a new loop of two, a test under its budget). The comparison
never fails the job.

- The base branch needs one run of the workflow with this version of the
  action first (a push to `main`); until then the summary says so.
- Findings are matched by kind, label, file and function, so a finding that
  only moved to another line is neither new nor fixed. Baselined findings
  are left out.
- Without `actions: read` the job summary says the runs could not be
  listed; without `pull-requests: write`, or on a pull request from another
  repository (a fork) or Dependabot, there is no comment and the job summary
  still has the report. The action only edits its own comment.
- `json-file: ""` turns the file, the artifact and the comparison off. Do not
  pass `--queryguard-json` in `pytest-args` as well: the action reads the file
  at `json-file`.
- Several jobs (for example one per database) keep apart by `category`.
- The same report locally: `python -m pytest_queryguard.compare base.json head.json`.

### Query counts over the last runs

Since 0.8.0; experimental (see [Stability](stability.md#experimental)).

The comparison shows one step; `trend: "10"` shows the last ten runs, as an
APM shows a query count over time in production. On a push to the default
branch and on a pull request, the action downloads the query-count artifacts
of the latest finished runs of the same workflow on the (base) branch, adds
the file of this run as the newest, and appends the trend to the job summary.
On a pull request with a comment it is also folded into that comment, as a
collapsed `<details>` block below the comparison.

```
### pytest-queryguard: query counts over the last 5 runs

5 runs, statements 3120 -> 3187 (+67) `▁▁▂▂█`. 3 tests changed their count, 2 new, 0 not in the latest run; 1 new findings, 0 gone.

| Run | Tests | Statements | |
| `1a2b3c4` | 410 | 3120 |  |
| ... | | | |
| `9f8e7d6 (this run)` | 412 | 3187 |  |

#### Tests whose count changed most
| Test | Runs | First | Last | Change |
| `tests/test_orders.py::test_list` | `▁▁▁▁█` | 3 | 53 | +50 |
```

- Each run is named by its commit. A run that stopped early or whose
  measurement failed (`complete: false`) is marked `incomplete`: its total is
  partial, and tests missing from it did not run.
- A test's change is its count in the latest run against its first run in the
  window, with one sparkline character per run (`·` where the test did not
  run), scaled between its lowest and highest count. A renamed test is one
  new test and one that is no longer in the latest run.
- New and gone findings compare the oldest run with the newest, as the
  comparison does; `Since` is the run from which on a new finding is in
  every run.
- Pull-request runs are not history: only runs of other events (push,
  schedule) on the branch count, one per commit. Runs without
  the artifact (expired after 30 days, or a job that stopped before writing
  it) or with a file that cannot be read are skipped, so the window may have
  fewer runs. The
  trend needs `actions: read` and never fails the job; without the
  permission it has this run only and the log has a warning.

The same report locally, from files given oldest first:

```
python -m pytest_queryguard.trend mon.json tue.json wed.json --top 20
python -m pytest_queryguard.trend runs/*.json --test "tests/test_orders.py::*" --format text
```

`--format` is `markdown` (the default), `text` or `json`; `--test` keeps the
tests whose node id contains the pattern, or matches it when it is a glob;
`--label` names the runs (once per file, default: the file names). The
sparklines use Unicode blocks; `--ascii` prints `_-~=+*#@` instead, and the
console output falls back to it by itself when the console cannot print the
blocks (a Windows console with cp1252). The exit status is 2 when a file
cannot be read or is not a `--queryguard-json` file (`--skip-unreadable`
leaves it out instead) or the `--output` file cannot be written, otherwise 0.

A typical rollout: run once with `fail: "false"` to see the alerts, create a
[baseline](baseline.md) for what you cannot fix yet, then switch to
`fail: "true"` (the default).

## Permissions and forks

- Uploading needs `security-events: write` on the job (or the workflow).
  Without it, the upload step fails with "Resource not accessible by
  integration".
- **Pull requests from forks get a read-only token**, and GitHub does not
  allow them to upload. The action skips the upload for them and still runs
  pytest, so the test result is unaffected; the findings are in the job log.
  The same holds for runs started by Dependabot (`github.actor` is
  `dependabot[bot]`), which also have a read-only token: the upload is skipped
  for them too.
- The upload step has `continue-on-error: true`: an upload that GitHub refuses
  (for example a read-only token in a case the action does not recognize, such
  as `pull_request_target`, or code scanning turned off) shows as a failed step
  and a warning, but does not fail a job whose tests passed. The flip side is
  that a missing `security-events: write` also does not fail the job; look at
  the step in the log if alerts do not appear. Set `upload: "false"` for
  events where you do not want an upload at all.
- Code scanning is free for public repositories. Private repositories need
  GitHub Code Security.

## Without the action

```yaml
      - run: pytest --queryguard-sarif=queryguard.sarif
        continue-on-error: true
      - uses: github/codeql-action/upload-sarif@v4
        if: always()
        with:
          sarif_file: queryguard.sarif
          category: pytest-queryguard
```

The upload step prints one warning per finding (`Calculated fingerprint ... but found
existing inconsistent fingerprint value`). It is noise: the upload succeeds and alerts
are matched correctly. See [SARIF output](sarif.md) for why.

## Query counts as JSON

`pytest --queryguard-json=queryguard.json` writes one entry per test: how many
statements it ran, the same split by verb, the database time and the findings
it found (after ignores). Keep the file from the main
branch as an artifact and compare a pull request's file with it to see which
tests started running more queries before anything fails.

```json
{
  "format": 1,
  "tool": {"name": "pytest-queryguard", "version": "0.8.0"},
  "complete": true,
  "tests": {
    "tests/test_orders.py::test_list_orders": {
      "statements": 51,
      "by_verb": {"select": 51},
      "db_time": 0.0007,
      "truncated": false,
      "findings": [
        {"kind": "relationship-load", "label": "Order.customer", "count": 50,
         "file": "app/orders.py", "function": "list_orders", "line": 10,
         "stack": [{"file": "tests/test_orders.py", "function": "test_list_orders",
                    "line": 7}],
         "baselined": false,
         "message": "Order.customer was lazy-loaded 50 times\nfirst at app/orders.py:10 in list_orders\n..."}
      ],
      "max_queries_marker": false
    }
  }
}
```

- `statements` counts the statements of the test function, as budgets do:
  the setup and teardown of fixtures are not measured. `by_verb` sorts the stored ones into `select`, `insert`,
  `update`, `delete`, `transaction` and `other`; `truncated` is true when the
  test ran more than `queryguard_max_events` statements, and then `by_verb`
  covers only the first of them.
- `max_queries_marker` is true for a test with its own `max_queries` marker.
- `findings` lists what the test found after ignores. `stack` holds the
  lines of your code that called `file:line`, innermost first, as the
  terminal report's `called from` lines (`queryguard_stack_depth`). Findings in the
  baseline are listed too, with `"baselined": true`, so the file is the same
  whether or not the baseline is being updated. `message` (since 0.8.0; the
  sample above is from `main`) is the finding's
  text as the terminal report shows it, without the indent; like every
  message, its wording is not stable. `db_time` is null when no
  statement was timed.
- `complete` is false when the run stopped early (`-x`, Ctrl-C, a collection
  error, a crashed pytest-xdist worker) or measurement failed: tests missing
  from such a file did not run, they did not stop querying.
- With pytest-xdist the controller writes one file with the tests of every
  worker. With `queryguard_mode = off` nothing is measured and `tests` is
  empty.
- `format` changes only when an existing field changes meaning; new fields
  may be added.

## How this repository tests the action

The `action` job in `.github/workflows/ci.yml` runs the local action
(`uses: ./action`) on `examples/action-sample`, a project with one N+1 query:
once with `fail: "false"` (the SARIF file is checked, including that its URI
starts with `examples/action-sample/`, the working directory), once expecting
the step to fail, and once with the finding in a baseline. The first run
also has `trend: "5"`; when the trend wrote a report, the job checks that it
has this run's row. None of them
uploads: a sample upload with no results would make every pull request report
"1 configuration not found" in its checks.
