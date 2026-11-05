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
  parents loaded at app/orders.py:9 in list_orders
  kind: relationship-load  label: Order.customer
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

A loop in a shared helper or fixture fails every test that runs it, and each
of them prints the same block. After the run, the summary names such a
finding once:

```
========== queryguard: the same finding in several tests ==========
   30 tests  relationship-load  Order.customer  (900 times in total, first in tests/test_orders.py::test_list[0])
fix it once where it starts; each test above reports it on its own
```

A finding is listed when two tests or more reported it, grouped by kind and
label wherever it was found, the most tests first. Ignored and baselined
findings are not counted. It works the same with pytest-xdist.

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
