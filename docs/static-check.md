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
   assigned exactly once there.
2. In the loop body, `obj.rel` where `rel` is a relationship of `C` with the
   default `lazy="select"` (or `lazy=True`). Relationships with `joined`,
   `selectin`, `subquery`, `immediate`, `raise`, `raise_on_sql`, `noload`,
   `write_only` or `dynamic` are not reported.
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
  entity, `aliased()` classes, and a class name that matches two mapped
  classes unless the file imports it with `from module import Name`.
- A read that runs at most once per loop: in a block that ends with
  `return`, `raise` or (for the loop itself) `break`, or inside a `lambda` or
  a nested function.
- A many-to-one relationship when the same function queried all rows of the
  target class (no `where`, `filter`, `join` or `limit`) before the loop:
  those objects usually come from the identity map, without a query. This
  does not apply when the target is the class of the loop's own query.
- `lazy="dynamic"` and `write_only` relationships, even when each one is
  queried per row; the plugin reports those at runtime.
- Django, SQLAlchemy 1.x `Query` features beyond the ones above, and Core
  statements.

It cannot know how many rows a query returns, whether the objects were
already in the Session, or whether a branch runs: a finding means "likely
N+1", not a measured one. The pytest plugin measures.

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
