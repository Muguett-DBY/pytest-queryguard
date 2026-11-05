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
      - uses: MaciejZiel/pytest-queryguard/action@COMMIT_SHA   # pin a release tag or a commit SHA
        with:
          pytest-args: -x tests/
          # baseline: queryguard-baseline.json   # add once the file exists, see below
```

The `push` trigger is limited to `main` so that a pull request does not run
the workflow twice, once for the push and once for the pull request.

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
`package` only to install something else, such as `pytest-queryguard==0.4.0`
or a path.

The action installs `pytest-queryguard` into the active Python, runs
`pytest --queryguard-sarif=queryguard.sarif`, uploads the file with
[`github/codeql-action/upload-sarif`](https://github.com/github/codeql-action)
(pinned by commit SHA inside the action) and then fails the step if pytest
failed. Install your own project first: the action does not know how.

| Input | Default | Meaning |
|---|---|---|
| `pytest-args` | empty | Arguments for pytest, as a list of shell words: quotes group words (`-k "a or b"`). The value is split by Python's `shlex` and is not evaluated by a shell, so `$(...)`, backticks and `$VAR` are passed to pytest as literal text. |
| `python-version` | empty | Set up this Python. Empty: use the `python` on PATH. |
| `package` | empty | What to `pip install`. Empty: the plugin from the action's own checkout, matching the pinned ref. Otherwise a requirement like `pytest-queryguard>=0.4`, or a path. |
| `baseline` | empty | Baseline file, see [baseline.md](baseline.md). Empty: `queryguard_baseline` from your config. |
| `fail` | `true` | `true`: fail the step when pytest fails. `false`: never fail, only report. |
| `working-directory` | `.` | Where to run pytest. Locations in the SARIF file are still relative to the repository root (see [SARIF output](sarif.md#paths-and-the-repository-root)). |
| `sarif-file` | `queryguard.sarif` | Output file, relative to `working-directory`. |
| `upload` | `true` | Upload to code scanning. `false`: only write the file. |
| `category` | `pytest-queryguard` | Code scanning category; use one per job when several jobs upload. It also names the job's query-count artifact and its pull-request comment. |
| `json-file` | `queryguard.json` | The [per-test query counts](#query-counts-as-json), relative to `working-directory`; uploaded as the artifact `queryguard-json-<category>`. Empty: none. |
| `compare` | `true` | On a pull request, compare the counts with the base branch and write the result to the job summary. |
| `comment` | `true` | Also post the comparison as a pull-request comment, updated in place on later pushes. |
| `github-token` | `github.token` | Token for the comparison and the comment. |

Outputs: `exit-code` (pytest's), `sarif-file` and `json-file`.

### Query counts on pull requests

On every run the action writes the [query counts](#query-counts-as-json) and
uploads them as an artifact. On a pull request it downloads the file of the
latest finished run of the same workflow on the base branch, compares the
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
  "tool": {"name": "pytest-queryguard", "version": "0.6.0"},
  "complete": true,
  "tests": {
    "tests/test_orders.py::test_list_orders": {
      "statements": 51,
      "by_verb": {"select": 51},
      "db_time": 0.0003,
      "truncated": false,
      "findings": [
        {"kind": "relationship-load", "label": "Order.customer", "count": 50,
         "file": "app/orders.py", "function": "list_orders", "line": 12,
         "stack": [{"file": "tests/test_orders.py", "function": "test_list_orders",
                    "line": 7}],
         "baselined": false}
      ]
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
  whether or not the baseline is being updated. `db_time` is null when no
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
the step to fail, and once with the finding in a baseline. None of them
uploads: a sample upload with no results would make every pull request report
"1 configuration not found" in its checks.
