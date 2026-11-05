# Recipe: CI

pytest-queryguard fails the pytest run, so every CI system that runs pytest
enforces it. This page is about where the findings show up. For code scanning
alerts with the GitHub Action and the SARIF file, see
[CI and GitHub Action](ci.md).

## GitHub Actions

Plain pytest, with a JUnit file kept as an artifact:

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
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"
      - run: pip install -e ".[test]"        # your project, with pytest-queryguard in its test extra
      - run: pytest --junitxml=report.xml
      - uses: actions/upload-artifact@v7
        if: always()
        with:
          name: junit
          path: report.xml
```

The finding is in the job log as a normal pytest failure. With the action
(see [CI and GitHub Action](ci.md)) it also becomes an annotated code scanning
alert.

## GitLab CI

GitLab shows JUnit failures in the merge request when the file is declared as
a report:

```yaml
tests:
  image: python:3.12
  script:
    - pip install -e ".[test]"
    - pytest --junitxml=report.xml
  artifacts:
    when: always
    reports:
      junit: report.xml
```

The text of a finding is in the failure message of the test case in that
file, so it appears in the merge request's test report. This is what the
file holds for the N+1 test of the sync example (generated with
`pytest --junitxml=report.xml tests/test_orders.py -k list_orders`; the time, timestamp and
hostname are replaced):

```xml
<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1" time="0.043" timestamp="..." hostname="..."><testcase classname="tests.test_orders" name="test_list_orders" time="0.043"><failure message="Failed: queryguard: N+1 detected&#10;&#10;  Order.customer was lazy-loaded 50 times&#10;  first at app/orders.py:10 in list_orders&#10;  parents loaded at app/orders.py:9 in list_orders&#10;  kind: relationship-load  label: Order.customer&#10;  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?&#10;  fix: load it with the parent query, e.g.&#10;       select(Order).options(joinedload(Order.customer))">queryguard: N+1 detected

  Order.customer was lazy-loaded 50 times
  first at app/orders.py:10 in list_orders
  parents loaded at app/orders.py:9 in list_orders
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))</failure></testcase></testsuite></testsuites>
```

GitLab's code quality and SAST report formats are not produced; the SARIF
output is for GitHub code scanning. The GitLab snippet was written from
GitLab's documentation and the JUnit file above was produced by running
pytest; the pipeline itself was not run on GitLab.

## pre-commit

pre-commit does not fit. A hook there runs on the files of a commit, and
pytest-queryguard looks at the SQL that a test run sends, so there is nothing
it can check in a changed file without running the tests against a database.
Run it in CI and in your normal test command, and use a `pytest` hook in
`repo: local` only if you already run your suite from pre-commit.

## Keep the baseline honest

Run the full suite with `--queryguard-update-baseline` in a scheduled job and
open a pull request when the file shrinks. Do not let that job commit to
the main branch; the baseline should change only through review (see
[Adopting on a legacy codebase](recipes-legacy.md)).
