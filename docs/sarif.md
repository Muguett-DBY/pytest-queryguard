# SARIF output

```console
pytest --queryguard-sarif=queryguard.sarif
```

writes the findings of the run as [SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html),
the format GitHub code scanning reads. The existing `--queryguard-report`
flag is unrelated and unchanged. The path is relative to the directory pytest
was started from.

## What is in the file

- One rule per finding kind (`queryguard/relationship-load`,
  `queryguard/repeated-statement`, `queryguard/column-load`,
  `queryguard/write-loop`, `queryguard/unused-eager-load`,
  `queryguard/unused-columns`, `queryguard/cartesian-load`,
  `queryguard/missing-index`), each with a description and a fix hint. All
  rules are always listed, the experimental ones too when their option is
  `off`.
- One result per finding identity (kind, label, file, function), so the same
  finding from several tests is one result, with its counts added up and the
  earliest line.
- When a result merges the finding from several tests, `properties.count` is
  the total over all of them, `properties.tests` the number of tests and
  `properties.peakCount` the largest count in one test. The message then starts
  with the same numbers (`Seen in 20 tests: 289 times in total, up to 10 times
  in one test.`) and continues with the text of the test with the earliest
  line, which names that test's own count. A finding from one test has only
  `count`, and its message is that test's.
- The location is the file and the line of the first occurrence; the function
  is a logical location. See "Paths and the repository root" below for what
  the path is relative to.
- Since 0.8.0: a `relationship-load` result whose parent query line is
  certain has a `fixes` entry: the loader option and its import as insertions (an empty
  `deletedRegion` with `insertedContent`), the same change as
  `--queryguard-write-patch`, see
  [A patch for the parent query](when-a-test-fails.md#a-patch-for-the-parent-query).
  Every SARIF file has them, no option is needed. GitHub code scanning ignores
  fixes; viewers such as the SARIF Viewer for VS Code offer to apply them.
- `level` is `error` for a kind that fails tests and `warning` otherwise (see
  `queryguard_mode` and `queryguard_fail_on`).
- `partialFingerprints` are derived from the finding identity only, not from
  line numbers or counts. `primaryLocationLineHash`, the value GitHub
  de-duplicates alerts by, is set from it, so a finding keeps its alert when
  the code around it moves.
- Results are sorted and the output is stable, so two runs on the same code
  give the same bytes.

`primaryLocationLineHash` is normally a hash of the source line, which GitHub
computes itself when it is missing. pytest-queryguard sets it from the finding
identity instead, because a finding here is a pattern over many statements and
not one line, and a hash of the line would change with every edit near it. The
`:1` suffix is only the occurrence index that GitHub's format expects; it is
not a real count. GitHub code scanning keeps the value and matches alerts by it:
in a test repository, moving a finding five lines down kept its alert (the
location was updated), and changing the text of the finding's own line on a pull
request kept the alert instead of opening a new one.

One side effect: `github/codeql-action/upload-sarif` calculates its own hash of
the source line, compares it with the value in the file, and prints one
`##[warning]` per result, for example `Calculated fingerprint of 14fb6813ff23b590:1
for file app/orders.py line 11, but found existing inconsistent fingerprint value
...`. These show up as annotations on the workflow run, once per finding, on every
run. They are noise, not an error: the upload succeeds and the alerts are correct.
There is no input of the upload step that silences them. The alternative, leaving
the field out so that GitHub computes it, would make an alert change identity
whenever the line it points to is edited, which is why it is not done.

## Paths and the repository root

GitHub reads `artifactLocation.uri` relative to the root of the repository.
pytest-queryguard knows files relative to the pytest rootdir, and the two
differ when pytest runs in a subdirectory (for example the action with
`working-directory: backend`, where the rootdir is `backend/`). Alerts for
`test_orders.py` would then point at a file that does not exist in the
repository and show no annotations on pull requests.

So when the environment variable `GITHUB_WORKSPACE` is set (GitHub Actions
sets it to the checkout directory) and the rootdir is inside it, the path of
the rootdir below the workspace is put in front of every URI:
`backend/test_orders.py`. Both paths are resolved first, so symlinks on either
side do not matter. When the variable is not set, or the rootdir is outside
the workspace, the URIs stay relative to the rootdir, as before. The URIs are
plain relative paths; no `uriBaseId` is written.

This changes only the locations. The finding identity, the baseline and
`partialFingerprints` are computed from the rootdir-relative path and do not
change, so existing baseline files keep working and the same finding gets the
same fingerprint whatever the working directory. For alerts this means: the
fingerprint no longer depends on where pytest was started, but the URI does,
so GitHub sees an alert as the same one only while the URI stays the same.
If you move pytest to another working directory, expect the old alerts to
close and new ones to open at the new paths. Keep one `category` per job so
uploads from different jobs do not mix. How GitHub combines the fingerprint
and the location has not been verified here.

## What is left out

- Findings ignored with the `queryguard_ignore` option or marker.
- Findings accepted by the [baseline](baseline.md). They are left out instead
  of being marked with SARIF `suppressions`: GitHub's SARIF documentation does
  not say what it does with suppressed results, and leaving them out cannot
  produce an alert. A baselined finding that GitHub already knows then closes
  as fixed, and opens again if it leaves the baseline.
- Findings without a location inside the rootdir (a call site in a file
  outside the project). GitHub shows a result only when it has a location. The
  terminal summary says how many were left out.

A finding whose call site is unknown is not left out: its result is located at
the test (the test's file and the line of its `def`, with the test's name as
the function), and its message says `reported at the test`. The call site is
unknown when no application frame is on the stack, for example when
`session.execute` itself is handed to a worker thread (see
[Limitations](limitations.md)). Only the SARIF result moves: the terminal text
still says `<unknown location>`, and the baseline and the ignore rules see the
finding as before. One result per test then, because the test is part of the
result's identity.

The `called from` lines of the terminal report (`queryguard_stack_depth`)
become a code flow on the result (`codeFlows`, one thread flow from the
outermost caller to the finding's line), which GitHub shows as the path to
the alert. Callers outside the rootdir are left out of it; a result with no
caller left has no code flow.

The file is written at the end of every run, also when no finding was made,
so an upload step always has something to upload. With pytest-xdist, the
workers send their findings to the controller, which writes the file once.

After an interrupted run (a collection error, Ctrl-C, a usage error or an
internal pytest error) the file is not written and an existing one is left as
it was: a report from the tests that did run would close the alerts of the
tests that did not. The summary says so.

A collection error counts as an interrupted run also when the run goes on,
with `--continue-on-collection-errors` or under pytest-xdist, so such a run
writes no file either. The [GitHub Action](ci.md) then skips the upload with
a warning; an `upload-sarif` step of your own fails because the file is
missing, unless a file from an earlier step is still there. Fix the module
that does not collect to get a report again.

An excerpt:

```json
{
  "ruleId": "queryguard/relationship-load",
  "ruleIndex": 0,
  "level": "error",
  "message": { "text": "Order.customer was lazy-loaded 3 times\nfirst at app/orders.py:8 in list_orders\n..." },
  "locations": [
    {
      "physicalLocation": {
        "artifactLocation": { "uri": "app/orders.py" },
        "region": { "startLine": 8 }
      },
      "logicalLocations": [{ "name": "list_orders", "kind": "function" }]
    }
  ],
  "partialFingerprints": {
    "primaryLocationLineHash": "6f1c...:1",
    "queryguardIdentity/v1": "6f1c..."
  },
  "codeFlows": [{ "threadFlows": [{ "locations": [
    { "location": {
        "physicalLocation": {
          "artifactLocation": { "uri": "tests/test_orders.py" },
          "region": { "startLine": 7 }
        },
        "message": { "text": "test_list_orders" } } },
    { "location": {
        "physicalLocation": {
          "artifactLocation": { "uri": "app/orders.py" },
          "region": { "startLine": 8 }
        },
        "message": { "text": "list_orders" } } }
  ] }] }]
}
```
