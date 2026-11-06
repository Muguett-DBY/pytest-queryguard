# Ignoring findings

Four mechanisms leave findings out, from the narrowest to the widest: an
[`ignore()` block](#the-ignore-block) (a few lines of code), the
`allow_nplusone` marker (one test), `queryguard_ignore` (the whole suite) and
a [baseline](baseline.md) (everything that exists today). Budgets are never
affected by them.

## The marker and `queryguard_ignore`

```python
@pytest.mark.allow_nplusone
def test_legacy_report(session): ...
```

The test is not checked for N+1. A `max_queries` marker on the same test is
still enforced.

To leave out one finding instead of the whole test, give the marker patterns
(`fnmatch`, case-sensitive) for the finding's label, and optionally `kind`,
`file`, `function` and [`stack`](#matching-the-call-stack). A label written out exactly as the report prints it
matches too, even when it holds `[` or `]`, which `fnmatch` would read as a
character class:

```python
@pytest.mark.allow_nplusone("Order.customer", function="list_orders")
def test_legacy_report(session): ...
```

The patterns match every kind of finding, `missing-index` included, so
`@pytest.mark.allow_nplusone("public.audit_log", kind="missing-index")` leaves
out one table's finding. The bare marker leaves out N+1 findings only;
`allow_missing_index` is the shortcut for skipping the index check for the test
or for some tables.

A table name is matched the same way in all four places that take one
(`allow_nplusone(..., kind="missing-index")`, `queryguard_ignore`,
`allow_missing_index` and `queryguard_index_ignore_tables`), so one spelling
works everywhere. A pattern is a table name or `schema.table`, and each part is
an identifier:

- unquoted (`audit_log`, `UserAccount`, `audit_*`): compared as written
  (case-sensitive, `UserAccount` does not match `useraccount`), with `fnmatch`
  wildcards;
- double-quoted (`"UserAccount"`, `public."UserAccount"`, `"my.table"`):
  compared exactly, with no wildcards, and may hold dots (`""` is a quote).

A bare name matches the table in any schema; `public.audit_log` matches the
table `audit_log` in the schema `public` also when the report prints it as
`audit_log` because that schema is on the search path. The label as printed
(`"UserAccount"`) matches too, and so does any `fnmatch` pattern on `table` or
`schema.table` (`audit*` also matches every table in the schema `audit`). Schemas are
known on PostgreSQL only; on SQLite and MySQL only bare names match.

In `queryguard_ignore` and `allow_nplusone`, this makes a bare name match the
table in every schema: `missing-index audit_log` also leaves out the finding
labelled `audit.audit_log`.

The same patterns work for the whole suite in `queryguard_ignore`, one per
line as `KIND LABEL [file=GLOB] [function=GLOB] [stack=GLOB]`:

```toml
[tool.pytest.ini_options]
queryguard_ignore = [
    "relationship-load Order.customer",
    "repeated-statement SELECT * FROM settings*",
    "missing-index public.audit_log",
]
```

Every finding in the report has a line such as
`kind: relationship-load  label: Order.customer`; write the kind, a space and
the label as the pattern. The kinds are `relationship-load` (the label is the relationship, as in
`Order.customer`), `repeated-statement` (the normalized statement),
`column-load` (the class and its deferred columns, as in `Book.summary`, or
the class and the cause for expired attributes, as in `Book (expired)`),
`write-loop` (the normalized `INSERT`, `UPDATE` or `DELETE`),
`unused-eager-load` (the relationship, as in `Order.customer`),
`unused-columns` (the class, as in `Account`) and
`missing-index` (the table, with its schema as in the report; see below for
the spellings a table name accepts). `file` is
relative to pytest's rootdir, with forward slashes. Findings that no longer
exist are easy to forget, so a pattern that matched nothing in the run is
listed in the terminal summary under "stale ignores" (not shown with
pytest-xdist, where the workers do the matching).

### Matching the call stack

`file` and `function` match the line the finding is reported at, its `first
at`. When that line is in a helper called from many places, `stack` leaves the
finding out only when it comes through one of them. Here `names()` makes an
N+1 that is accepted in the nightly admin export, and nowhere else:

```python
# app/orders.py
def names(session):
    return [order.customer.name for order in session.scalars(select(Order))]


# app/admin/export.py
from app import orders


def export_orders(session):
    return write_csv(orders.names(session))
```

```toml
[tool.pytest.ini_options]
queryguard_ignore = [
    "relationship-load Order.customer stack=app/admin/*",
]
```

or, for one test, `@pytest.mark.allow_nplusone("Order.customer",
stack="app/admin/export.py:export_orders")`. A test that calls `names()` from
a view still fails (unless it went through the export first, see below).

`stack` matches when any frame of the stack the report shows matches it: the
`first at` line and each `called from` line under it. A pattern without a
colon matches the frame's file (`app/admin/*`, `vendor/*`); one with a colon
also matches `file:function` (`app/admin/export.py:export_orders`,
`*:export_*`). The file is written as the report prints it, like for `file`:
relative to the rootdir, with forward slashes. The kind, the label and any `file` and `function` must match too.

The limits are those of the report:

- Only `queryguard_stack_depth` callers are recorded (3 by default), and a
  caller further out is not matched. Raise the option when the frame you
  name is not among the `called from` lines; with `0`, `stack` matches the
  `first at` line only. A pattern that matches nothing is listed as stale.
- The frames are the lines of your code: SQLAlchemy, pytest, the standard
  library and installed packages (`site-packages`) are never in the stack, so
  `stack` can exclude code vendored into the repository but not an installed
  library.
- The stack is recorded the first time a line runs in a test. When one test
  reaches the helper from two places, the finding has the stack of the first,
  and only that one is matched: a test that runs the export and then the view
  has its N+1 left out, also the loads made through the view.

The stack is not part of a finding's identity, so a [baseline](baseline.md)
entry still matches by kind, label, file and function.

## The `ignore()` block

To leave out what a few lines of code do, wherever they are called from,
wrap them in `ignore()` with the kinds to leave out:

```python
from pytest_queryguard import ignore


def export_report(session):
    with ignore("relationship-load", reason="one-off export, runs at night"):
        for order in session.scalars(select(Order)):
            write_row(order, order.customer.name)
```

The block can be in a test or in the application code, where
`pytest_queryguard` must then be importable; outside a measured test the
block only sets a context variable. It also works as a decorator, on a
function or a coroutine function:

```python
@ignore("repeated-statement", "write-loop")
def migrate_legacy_rows(session): ...
```

The kinds are the ones above, or `fnmatch` patterns for them (`"*"` for every
kind); an unknown kind raises `ValueError`. `reason` is for the reader only.

A finding is left out when every statement or load it counts ran inside a
block for its kind. One of them outside the block is enough to report the
finding in full, with every load counted, as if the block were not there.
This catches the usual slip, a lazy load that happens after the block:

```python
def test_export(session):
    with ignore("relationship-load"):
        orders = export_report(session)  # lazy-loads order.customer
    assert orders[1].customer.name == "Ann"  # one more lazy load, outside
```

Here the assertion lazy-loads one more customer outside the block, so the N+1
is reported with all its loads and the block is listed as stale. Put every
access to the relationship inside the block (the assertion too), or load it
with the query instead.

In [strict mode](when-a-test-fails.md#strict-mode-fail-at-the-access) a lazy
load inside the block never raises; a later one outside it still can. The
block applies to `record_queries()` and `log.assert_no_nplusone()` and to the
[in-application mode](in-app.md) too. Budgets (`max_queries`) still count the
statements of the block. A pattern or marker that matches the same finding
as a block is used too, and neither is listed as stale.

When a test runs more statements or loads than `queryguard_max_events`, the
plugin stops storing them and only counts the rest, so it cannot tell whether
those ran inside the block: the blocks then leave nothing out in that test,
and the findings are reported as without them.

What "inside" means: the block is kept in a context variable, so it covers the
thread or the asyncio task that entered it, and the tasks created inside it
(a task starts with a copy of its creator's context), including
`asyncio.to_thread()`. It does not cover:

- a thread started inside the block (`threading.Thread`, a
  `ThreadPoolExecutor`, `loop.run_in_executor()`): a new thread starts with an
  empty context, unless it runs the work with
  `contextvars.copy_context().run(...)` or the interpreter makes threads
  inherit the context (`-X thread_inherit_context=1`, the default on the
  free-threaded build of Python 3.14);
- a task or a thread that already existed, such as the server thread of a
  live-server test: the block of the test does not reach the requests it
  serves. Put the block in the code that handles the request instead, or use
  the marker or `queryguard_ignore`;

For the missing-index check, every run of the statement in the test counts,
although it is explained only at its first run: a statement run inside the
block and again outside it is reported.

A block that left out nothing in the whole run, in any test that entered
it, is listed in the same "stale ignores" summary, with the file and line of
the `with` (or of the decorator):

```text
ignore() blocks that left out no finding in this run:
  app/reports.py:14: ignore('relationship-load')
```

Unlike the patterns, the blocks are also reported under pytest-xdist: the
workers send the blocks they entered and used to the controller. A block is
noted only when it is entered in the context of a measured test (its thread,
its tasks, or a thread given a copy of its context), so a thread left over
from another test does not count. It is not listed when it only ran in tests
with a bare `allow_nplusone` marker or past `queryguard_max_events`, or when
none of its kinds was checked there (a `write-loop` block while
`queryguard_write_loop_threshold` is off, a `missing-index` block without the
index check).

