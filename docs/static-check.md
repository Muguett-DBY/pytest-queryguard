# Static check (experimental)

Since 0.8.0.

The pytest plugin finds an N+1 query when a test runs it. The static check
reads your source code instead, without a database and without running your
tests, and reports the most common shape of an N+1: a loop over the objects of
an ORM query that reads a lazy relationship of each one.

```python
def list_orders(session: Session) -> list[dict[str, object]]:
    rows = []
    for order in session.scalars(select(Order)).all():
        rows.append({"id": order.id, "customer": order.customer.name})
    return rows
```

```console
$ python -m pytest_queryguard.lint --models app.models app/
app/orders.py:10:56: Order.customer is lazy-loaded once per row (N+1)
  in the loop at line 9 over the query at line 9 in list_orders
  rule: queryguard/static-relationship-load  label: Order.customer
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))

queryguard lint (experimental): 1 finding in 6 files
```

It is experimental (see [Stability](stability.md#experimental)): the command,
its options and its output may change in a minor release. It is deliberately
conservative: it reports only what it can follow from start to end inside one
function, so it misses many N+1 queries that the pytest plugin finds. Use it
as an early warning, in an editor or a pre-commit hook, next to the plugin, not
instead of it.

## Running it

```text
python -m pytest_queryguard.lint --models MODULE [--models MODULE ...]
    [--format text|json|sarif] [--output FILE] PATHS...
```

- `--models` (required, repeatable): a module that defines your mapped classes,
  or imports them, such as `app.models`. The check **imports** this module, so
  its import-time code runs, as it does when your tests start; it then reads
  every class mapped in the same registry (the `Base` of your models) and
  calls `configure_mappers()`. Run it from the directory where
  `import app.models` works (the working directory is on `sys.path`).
  Nothing else is imported: PATHS are only parsed.
- PATHS: files and directories. Directories are searched for `*.py`; hidden
  directories, `__pycache__`, `venv`, `node_modules`, `site-packages`,
  `build` and `dist` are skipped, and a file named twice is checked once.
  Files are decoded as Python decodes them (UTF-8, a byte order mark or a
  `# -*- coding: ... -*-` line). A file that does not parse is skipped with
  a message on stderr.
- `--format`: `text` (default), `json` or `sarif`.
- `--output FILE`: write the report to this file too.

The exit status is 1 when anything is reported, 0 when nothing is, and 2 when
a model module cannot be imported, the mappers cannot be configured, a path
does not exist, the `--output` file cannot be written or an argument is wrong.

## What it reports

A finding needs all of these:

1. A `for` loop, an `async for` loop or a comprehension over the objects of
   one query of one mapped class:
   - `session.scalars(select(C)...)`, `session.execute(select(C)...).scalars()`,
     `await session.scalars(...)`, `await session.stream_scalars(...)`,
     with or without `.all()`, `.unique()` or `.fetchall()`, or inside
     `list()`, `tuple()` or `sorted()`;
   - `session.query(C)...` and Flask-SQLAlchemy's `C.query...`;
   - `select(C)` followed by `where`, `order_by`, `join`, `options`, `limit`
     and the other methods that keep it a select of `C`. `select` has to be
     SQLAlchemy's (or SQLModel's) by the file's imports: `from sqlalchemy
     import select`, `sa.select` after `import sqlalchemy as sa`, or
     `db.select` where the module has `db = SQLAlchemy(...)` from
     Flask-SQLAlchemy.

   The statement, the result or the list of rows may be assigned to a
   variable first, in the same function, as long as that variable is
   assigned exactly once there. A statement may also be built in steps,
   `stmt = select(C)` and then only `stmt = stmt.where(...)`,
   `stmt = stmt.options(...)` and the like, all above the loop: an option
   added in any of the steps, also in an `if`, counts. `C` may be an
   `aliased(C)` or `with_polymorphic(C, ...)` of the class, written in the
   `select()` or assigned once in the same function (then only the
   relationships of `C` itself are checked).
2. In the loop body, `obj.rel` where `rel` is a relationship of `C` with the
   default `lazy="select"` (or `lazy=True`), or with `lazy="immediate"`,
   which runs the same query per object when the objects load. Relationships
   with `joined`, `selectin`, `subquery`, `raise`, `raise_on_sql`, `noload`,
   `write_only` or `dynamic` are not reported. Reading an
   `association_proxy` reads the relationship it goes through.
3. The query has no loader option for that relationship: `joinedload`,
   `selectinload`, `subqueryload`, `immediateload`, `raiseload`, `noload` or
   `contains_eager`, on that path or with `"*"`. `lazyload()` and
   `defaultload()` keep it lazy, so they do not count, and `lazyload()` of
   the relationship wins over a `"*"` option. The options are functions
   imported from SQLAlchemy (`selectinload(...)`, `orm.selectinload(...)`)
   and the methods chained on them.

It follows chains: in `order.customer.address`, both `Order.customer` and
`Customer.address` are checked, and in a nested loop
`for item in order.items: item.product`, both `Order.items` and
`Item.product`. `obj.awaitable_attrs.rel` counts as reading `rel`. The fix
line is the same as the plugin's: `joinedload` for a many-to-one or
one-to-one step, `selectinload` for a collection, chained along the path.

Each relationship is reported once per loop, at its first read.

## What it does not report

These are left out on purpose, to keep false positives rare:

- Anything that crosses a function: a loop over a function's parameter, a
  query built in a helper, objects passed to a serializer, a Pydantic model,
  a template, `__repr__` or a property that reads a relationship.
- A variable assigned more than once in the function (or a loop variable
  assigned in the loop body): it may no longer hold the query.
- An option it does not understand (a variable, a method of a variable such
  as `opt.joinedload(...)`, `*options`, a helper call,
  `with_loader_criteria`, `selectin_polymorphic`): the query is treated as
  if it loaded everything.
- `session.execute(select(C))` iterated as rows, selects of more than one
  entity, and a class name that matches two mapped classes unless the file
  imports it with `from module import Name`.
- A read that runs at most once per loop: in a block that ends with
  `return`, `raise` or (for the loop itself) `break`, or inside a `lambda` or
  a nested function.
- A many-to-one relationship when the same function queried all rows of the
  target class (no `where`, `filter`, `join` or `limit`) before the loop:
  those objects usually come from the identity map, without a query. This
  does not apply when the target is the class of the loop's own query.
- A read inside pytest-queryguard's `ignore()` for `relationship-load`
  (or a pattern that matches it): a `with ignore(...)` block around the
  read, or `@ignore(...)` on a function around it, as the plugin leaves
  those out.
- A relationship that the function sets or refreshes on each object before
  reading it: `session.refresh(obj, ["rel"])` (or `attribute_names=`) or
  `set_committed_value(obj, "rel", value)` in an earlier loop over the same
  variable, or earlier in the same loop, directly or in a function of the
  same module that the loop calls with the object.
- A relationship that an earlier query of the same function loaded on the
  same rows: a query of the same class with a loader option for it, no
  `limit`, `join` or similar, and only filters that the loop's query has too
  (so it loaded at least those rows, which the identity map hands back). Its
  result is assigned to a variable, outside any `if` or loop, the session
  variable and the names in its filters are bound once, and no `commit()`,
  `rollback()`, `close()`, `expire...()` or `expunge...()` call comes between
  the two queries. Not when the loop's query uses `populate_existing`, which
  loads them again.
- `lazy="dynamic"` and `write_only` relationships, even when each one is
  queried per row; the plugin reports those at runtime.
- Django, SQLAlchemy 1.x `Query` features beyond the ones above, and Core
  statements.

It cannot know how many rows a query returns, whether the objects were
already in the Session, or whether a branch runs: a finding means "likely
N+1", not a measured one. The pytest plugin measures.

## Accuracy

Measured on the [accuracy corpus](accuracy.md), the same scenarios that
measure the plugin, without running them: each scenario module is linted with
its own models (the domain modules it imports and the module itself, as
`--models`). A relationship-load BUG scenario is a positive; every other
scenario, CLEAN or a BUG of another kind, is a negative; a scenario counts as
reported when the check has any finding in it. Findings in a scenario's
`setup` (its seeding code, which the corpus does not measure) are left out.
`uv run python scripts/lint_accuracy_report.py` prints the numbers and every
disagreement, and `tests/corpus/test_lint_accuracy.py` fails when they
change.

On 2026-11-07, 266 scenarios (SQLModel not installed), 51 of them
positives:

| | TP | FP | FN | precision | recall |
|---|---|---|---|---|---|
| before | 30 | 5 | 21 | 86% | 59% |
| after | 35 | 0 | 16 | 100% | 69% |

"Before" is the check before these changes, on the same 266 scenarios. The 14
scenarios added for it are shapes reported to other static checks (a nested loop
covered by the outer query's option chain, orm-lens #146; loops over mapped
classes and over objects read only through columns and properties, orm-lens
#72 and #85) and common ones: a repository with a service, a FastAPI
endpoint that builds its list, Pydantic `model_validate` of a dict per row,
an HTML fragment built like a template loop, a statement built over several
lines, an `aliased()` entity, a search loop with an early `return`, an `if`
guard.

The five false positives were a read inside `ignore()`, a relationship
refreshed or set per object before the loop (two), the same rows loaded with
the option by an earlier query, and a loop in a scenario's `setup` (now left
out of the count; the check is right that it is an N+1 there). The five
misses that were fixed: `aliased()`, `with_polymorphic()`, a statement built
over several lines, `lazy="immediate"` and an `association_proxy`. Of the
16 misses left, 14 cross a function (a serializer, Pydantic or FastAPI
reading the objects, a property, `__str__`, `__repr__`, a hybrid, a
recursive walk, a query in a repository or a fixture), one is a Jinja
template and one a `session.delete()` whose cascade loads the children.
The labels of the 35 true positives are the plugin's but one: for a concrete
inheritance hierarchy the check names the base class (`Payment.store`), the
plugin the subclass (`CardPayment.store`).

As for the plugin, the corpus is small and written by one person, and these
shapes were written with the check in mind: read the numbers as a regression
baseline, not as what the check finds in your code.

## Ignoring a finding

Put `# queryguard: ignore` on the line of the read or on the `for` line (for
a comprehension, its first line). On the `for` line it ignores every
relationship of that loop.

```python
for order in session.scalars(select(Order)):  # queryguard: ignore
    print(order.customer.name)
```

## JSON output

`--format json` prints one object. The keys are stable for `format` 1:

| Key | Value |
|---|---|
| `format` | `1` |
| `tool` | `{"name": "pytest-queryguard", "version": ...}` |
| `files` | number of files read |
| `errors` | files that were skipped, as `"path: reason"` |
| `findings` | list of findings, sorted by file, line and column |

Each finding:

| Key | Value |
|---|---|
| `rule` | `"queryguard/static-relationship-load"` |
| `file` | path relative to the working directory (as given when outside it) |
| `line`, `column` | the read of the relationship (1-based) |
| `loop_line` | the loop that reads it |
| `query_line` | the call that runs the query |
| `function` | the qualified name of the function (`Repo.list_orders`), or `<module>` |
| `relationship` | the label, as in the plugin: `"Order.customer"` |
| `path` | the relationships from the query's class to this one, `["Order.customer", "Customer.address"]` |
| `query_class` | the class the query loads |
| `fix` | the suggested query, `"select(Order).options(joinedload(Order.customer))"` |

## SARIF output

`--format sarif` writes SARIF 2.1.0 with one rule,
`queryguard/static-relationship-load`, at level `warning`, so it can be
uploaded to GitHub code scanning like the plugin's file (see
[SARIF output](sarif.md)). In GitHub Actions the paths are made relative to
the repository root with `GITHUB_WORKSPACE`, as in the plugin. Its findings
are separate from the plugin's `relationship-load` results: the rule id is
different, and a baseline does not apply to it. A result's fingerprint is
made of the file, the function and the relationship path (and its position,
when one function has the same path in two loops), not the line, so it
survives edits elsewhere in the file.

```yaml
      - run: python -m pytest_queryguard.lint --models app.models --format sarif --output lint.sarif app
        continue-on-error: true
      - uses: github/codeql-action/upload-sarif@v4
        if: always()
        with:
          sarif_file: lint.sarif
          category: pytest-queryguard-static
```
