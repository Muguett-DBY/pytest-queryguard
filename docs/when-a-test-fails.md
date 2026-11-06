# A test failed: now what

A test that passed yesterday now fails with `queryguard: N+1 detected`. This
page is the checklist: read the report, decide whether the finding is real,
then fix it or leave it out on purpose. Nothing here needs a change to the
plugin's configuration unless you decide to silence something.

## 1. Read the report

```
queryguard: N+1 detected

  Order.customer was lazy-loaded 50 times
  first at app/orders.py:10 in list_orders
    called from tests/test_orders.py:7 in test_list_orders
  parents loaded at app/orders.py:9 in list_orders
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
```

- The second line says **what repeated** and how often: here one relationship
  of 50 different orders.
- `first at` is the **line of your code** that sent the first of those
  statements: the first stack frame outside SQLAlchemy, pytest, the standard
  library and `site-packages`. If it points to your test file instead of the
  application, the application is probably installed non-editable into
  `site-packages` (see [Limitations](limitations.md)).
- `called from` lines are the lines of your code that called it, innermost
  first, up to the test: the path through your helpers, with the frames of
  SQLAlchemy, pytest and the plugin left out. They are recorded the first time
  that line runs in the test. `queryguard_stack_depth` sets how many are shown
  (3 by default, `0` for none).
- `parents loaded at` (relationship loads only) is the line that ran the query
  whose rows became the parent objects: the place for the loader option, even
  when the query is built in a helper and the loop is somewhere else. Parents
  reached through other relationships (`order.customer.country`) name the
  query at the start of that chain, and the `fix` line chains the loaders:
  `select(Order).options(joinedload(Order.customer).joinedload(Customer.country))`.
  The line is left out when the plugin cannot tell, for example for objects
  the test created and the query handed back without reading them, or objects
  loaded before the test body started; the hint then names the
  relationship's own class. It is also left out when two queries from
  different places read the same objects, since the plugin cannot tell which
  result the loop uses. An object that a later query returns from the Session
  without reading its row again keeps the query that loaded it.
- `kind` and `label` name the finding the way ignore patterns, `allow_nplusone`
  and the baseline do: `relationship-load Order.customer` is the pattern for
  this one. For a repeated statement the label is the normalized statement,
  which takes the place of a separate line; a statement longer than 200
  characters is cut and ends with `*`, so the label as printed still matches.
- `docs` links to the section of [How detection works](how-detection-works.md)
  that explains this kind of finding: what the rule counts, what it leaves
  out and why.
- `sql` is one of the statements, as SQLAlchemy rendered it.
- `fix` is the usual remedy for this kind of finding. It is a hint, not a
  patch: check it against what the code needs.

The `kind:` line is what you need to silence a finding; the table below shows
how each headline maps to the kind and label, for the cases where you want to
write a pattern with a wildcard.

| The report says | Kind | Label (for ignore patterns and the baseline) | Usual fix |
|---|---|---|---|
| `Order.customer was lazy-loaded 50 times` | `relationship-load` | `Order.customer` | `joinedload` (many-to-one) or `selectinload` (collection) on the query that loaded the parents |
| `the same statement ran 7 times` | `repeated-statement` | the normalized SQL, on the `kind:` line | one query with `WHERE id IN (...)`, or a join, instead of a loop |
| `Customer had its columns reloaded 50 times for 50 instances, after commit(), ...` | `column-load` | `Customer (expired)` | `expire_on_commit=False`, or load the objects again with one query |
| `Customer.name was loaded on its own 50 times for 50 instances (deferred column)` | `column-load` | `Customer.name` | `undefer(Customer.name)`, or drop `defer()` / `load_only()` |
| `the same INSERT ran 6 times, one row at a time` (opt-in) | `write-loop` | the normalized `INSERT`, `UPDATE` or `DELETE` | `session.add_all()` and one flush, or `session.execute(insert(Model), rows)` |
| `queryguard: missing index` (opt-in) | `missing-index` | the table | the `CREATE INDEX` on the `fix:` line; see the [index advisor](index-advisor.md) |
| `queryguard: expected at most 2 queries, got 4` | a budget, not a finding | none | fix the code, or raise the budget; ignore patterns and the baseline do not apply |

To list the kind and label of every finding of a run in one file, write a
scratch baseline:

```console
pytest --queryguard-baseline=/tmp/findings.json --queryguard-update-baseline
```

The SARIF file (`--queryguard-sarif=PATH`) carries the `kind:` line in each
message and the kind as the `queryguard/<kind>` rule id.

### The same finding in many tests

A loop in a shared helper or fixture is reported by every test that runs it,
and each of them prints the same block. After the run, the summary names such
a finding once:

```
========== queryguard: the same finding in several tests ==========
   30 tests  relationship-load  Order.customer  (900 times in total, from one place in the code; e.g. tests/test_orders.py::test_list[0])
a finding from one place in the code is fixed once for all its tests
```

A finding is listed when two tests or more reported it, failing or as a
warning, grouped by kind and label. "From one place in the code" means every
test reached it through the same function; with several places, the same
relationship or statement is loaded in a loop in more than one function. The
example is the first of the tests by name. Ignored and baselined findings are
not counted, a test that runs again (a rerun plugin) counts once, and it works
the same with pytest-xdist.

### Strict mode: fail at the access

The report comes when the test ends. With `queryguard_strict = true` (or
`--queryguard-strict`) the N+1 fails the test where it happens instead: the
lazy load of a relationship for the second parent loaded at the same line
(`queryguard_relationship_threshold`, 2 by default) raises
`NPlusOneDetected`, and the traceback ends at the line that read the
attribute, the frames of SQLAlchemy and the plugin left out:

```
    for order in _helper_query(session):
>       name = order.customer.name
E       pytest_queryguard.NPlusOneDetected: queryguard strict: Order.customer was lazy-loaded for 2 parents loaded at one line; this access is the N+1
E
E         Order.customer was lazy-loaded 2 times
E         first at app/orders.py:13 in list_orders
E         parents loaded at app/orders.py:7 in _helper_query
E         kind: relationship-load  label: Order.customer
E         fix: load it with the parent query, e.g.
E              select(Order).options(joinedload(Order.customer))
```

It is Rails' `strict_loading!(mode: :n_plus_one_only)` and Laravel's
`preventLazyLoading()` for SQLAlchemy:

- Reading the relationship of one object never raises, and neither do
  objects whose query is unknown (made by the test, loaded before it). Objects
  loaded one by one at the same line (`for id in ids: get_order(id).customer`)
  count together, as in the report. The report at the end still covers the
  rest, and with `queryguard_repeat_scope = session` each Session counts on
  its own here too.
- The finding is the one the report makes, at the first lazy load, so
  ignore patterns with `file=`/`function=` and baseline entries match it.
  The analysis at the end still runs: SARIF, `--queryguard-json` and the
  summary include the N+1, and the exception replaces the report's text.
- It raises only what would fail the test anyway: in `fail` mode, for an N+1
  that no `allow_nplusone`, `queryguard_ignore` pattern or baseline entry
  covers. It does not raise while `--queryguard-update-baseline` records the
  findings.
- Code that catches `AssertionError` around the loop does not hide the N+1:
  the report at the end still fails the test.

## 2. Is it real?

Most findings are real: the code ran one statement per row, and the test
database only hides the cost because it holds few rows. Look closer when:

- **The test makes several requests**, for example five calls through a test
  client, and the repeated statement is an authentication or settings lookup.
  The report says `across N different Sessions or connection checkouts` in
  that case. Nothing loops; set `queryguard_repeat_scope = session`, see
  [Several sessions or requests](several-sessions.md).
- **The loop is pagination or a batched `IN (...)` load** that runs five or
  more pages or chunks. The repeat rule counts each page; see
  [Limitations](limitations.md).
- **Two parents each load a relationship once.** That is reported by default,
  as the smallest N+1; the [FAQ](faq.md) explains the threshold and how to raise
  it.

`--queryguard-report` prints how many statements each test ran, and
[`record_queries()`](python-api.md) lists every statement of a block with its
call site, if you want to look at them in the test itself.

## 3. Fix it

Apply the hint at the line the report names and run the test again. Two
patterns cover most cases:

```python
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

# a many-to-one read for every row: join it into the parent query
orders = select(Order).options(joinedload(Order.customer))

# a collection read for every row: one extra query for all of them
customers = select(Customer).options(selectinload(Customer.orders))

# a session.get() or a SELECT in a loop: one query with IN
rows = session.scalars(select(Customer).where(Customer.id.in_(ids))).all()
```

### A patch for the parent query

For a `relationship-load` finding, pytest-queryguard can write that change for
you. `--queryguard-patch` (or `queryguard_patch = true`) prints a unified diff
under the finding, and `--queryguard-write-patch=PATH` writes the diffs of the
whole run to one file:

```
pytest --queryguard-write-patch=queryguard.patch
git apply queryguard.patch      # from the rootdir
```

```diff
--- a/app/orders.py
+++ b/app/orders.py
@@ -1,9 +1,9 @@
 from sqlalchemy import select
-from sqlalchemy.orm import Session
+from sqlalchemy.orm import Session, joinedload
 
 from app.models import Order
 
 
 def list_orders(session: Session) -> list[Order]:
-    orders = session.scalars(select(Order)).all()
+    orders = session.scalars(select(Order).options(joinedload(Order.customer))).all()
     return [order.customer.name for order in orders]
```

The option goes on the `select(Order)` or `session.query(Order)` call of the
line in `parents loaded at`: as `.options(...)` right after the call, or as
one more argument of the `.options(...)` already in its chain. The loader is
`joinedload` or `selectinload`, the same as in the `fix:` hint, chained
through the relationships that reached the parents. A loader the module does
not import yet is added to its `from sqlalchemy.orm import` line, or on a new
line after the imports at the top of the file.

A wrong patch is worse than none, so a finding gets one only when the line is
certain, and otherwise only the `fix:` hint:

- every parent was loaded by one query line, and the plugin knows the class it
  selected;
- the statement on that line has exactly one `select(...)` or `.query(...)`
  call, with that class as its only argument. A query built on another line
  (`statement = select(Order)` and then `session.scalars(statement)`), a
  `session.get()` or a statement with a subquery gets no patch;
- `select` is imported from SQLAlchemy or SQLModel, the classes are imported
  (or defined) in the module under their own names, the function does not
  rebind them, and no `.options(...)` in the chain names the relationship
  already;
- the file is inside the rootdir, UTF-8 with `\n` line ends, and was not
  modified after the session started, so it is the code that ran.

The diff paths are relative to the rootdir (`a/app/orders.py`). When the
rootdir is a subdirectory of the git repository, apply it with
`git apply --directory=<subdirectory>`, or with `patch -p1` from the rootdir.
Fixes of the same file are merged into one diff; two relationships of one
query become two `.options(...)` calls, which SQLAlchemy accepts. Findings
left out by an ignore or the baseline get no patch. The patch is made when the
test's findings are reported, from its source file: no cost per statement.
Read it before you apply it, as any generated change: `joinedload` of a
collection or a large row can cost more than the lazy loads it replaces.

To keep the fix from regressing, put a [budget](budgets.md) on the test:
`@pytest.mark.max_queries(2)` fails it the day the count goes up again.

## 4. Or leave it out on purpose

If the code is right as it is, or you cannot fix it now, leave the finding
out, from the narrowest scope to the widest:

- one finding in one test:
  `@pytest.mark.allow_nplusone("Order.customer", function="list_orders")`;
- one finding in the whole suite: a line in `queryguard_ignore`, such as
  `relationship-load Order.customer function=list_orders`;
- everything that exists today: a [baseline](baseline.md), so that only new
  findings fail;
- a whole kind as a warning only: `queryguard_fail_on`;
- every finding as a warning for one run: `pytest --queryguard-mode=warn`.

[Ignoring findings](ignoring.md) has the details. Ignore patterns that no
longer match anything are listed as stale at the end of the run, so they do
not outlive the code they were written for.

## Next

Once the suite is green, run it in CI ([CI and GitHub Action](ci.md)); on a
project with many existing findings, start from a [baseline](baseline.md).
