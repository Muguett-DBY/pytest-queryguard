# Unused eager loads (experimental)

An eager load that nothing reads costs a join or an extra query for no
benefit:

```python
rows = session.scalars(
    select(Customer).options(selectinload(Customer.orders))
).all()
names = [c.name for c in rows]  # Customer.orders is never read
```

Set `queryguard_unused_eager_load` to turn the check on:

| Value | Effect |
|---|---|
| `off` (default) | Nothing is installed; there is no cost. |
| `warn` | A `QueryGuardWarning`; the test passes. |
| `fail` | The test fails, in `fail` mode, unless `queryguard_fail_on` leaves the kind out. |

`queryguard_mode = off` turns this check off too. The finding has the kind
`unused-eager-load`, the label `Customer.orders`, and the file and function of
the query that eager-loaded it.

## Opting out

The kind works with the usual mechanisms:

```python
@pytest.mark.queryguard_ignore(kind="unused-eager-load")
def test_export(session): ...

@pytest.mark.queryguard_ignore("Customer.orders")  # one relationship
def test_report(session): ...
```

or `unused-eager-load Customer.orders function=load_customers` in
`queryguard_ignore`.

## How it decides

While the option is on, the plugin does two things inside the body of each
test (fixtures and teardown are not measured):

1. A `do_orm_execute` listener reads the loader options of every top-level ORM
   `SELECT` and notes each relationship loaded with `selectinload`,
   `joinedload`, `subqueryload` or `immediateload`, and the query's call site.
2. `InstrumentedAttribute.__get__` is wrapped, and every attribute read on a
   mapped instance is noted as (class, name). Reads by the unit of work
   (flush, cascades, backrefs) do not go through it.

A relationship is reported when all of these hold:

- it was eager-loaded in the test body;
- no instance of that class (or a related class in the hierarchy) had that
  relationship read, from anywhere in the process, during the test body;
- some attribute of some instance of that class was read, so the code does
  use these instances through attribute access;
- it is not a relationship with delete cascade on a class deleted in the test
  (SQLAlchemy loads those to delete them);
- in a chain (`selectinload(A.b).selectinload(B.c)`), only the highest unused
  link is reported, and `B.c` is reported only if `A.b` was read.

### Why this approach

| Approach | Verdict |
|---|---|
| Attribute events (`set`, `init_scalar`, `modified`) | A read of a loaded attribute fires no event. Not usable. |
| Inspect `InstanceState` (`unloaded`, `dict`) at the end | Shows what is loaded, not what was read. Needs every instance tracked, with weak references. |
| Wrap `InstrumentedAttribute.__get__` while enabled (chosen) | One set insertion per read, only while a test body runs; works the same for sync and async sessions (the read happens in plain Python). It is a patch of a SQLAlchemy class, applied only when the option is on and removed at the end of the session. |
| Per-class descriptor replacement | The same cost with more moving parts (mappers configured later, inheritance). |

Measured with `scripts/bench_eager_overhead.py` (CPython 3.13, SQLAlchemy
2.1, SQLite in memory, median of 7 runs, one machine):

| Mode | ns per attribute read | us per ORM SELECT with `selectinload` |
|---|---|---|
| option off (default) | 171 | 653 |
| on, outside a test body | 213 | 679 |
| on, inside a test body | 322 | 711 |

With the option off, `scripts/bench_overhead.py` shows the same overhead as
before the change (Core about 30 %, ORM about 40 %, within run-to-run noise).

## Limitations

The check is conservative on purpose (when unsure it reports nothing) but it
can still be wrong.

- Code that reads `instance.__dict__` or `vars(instance)` is invisible. If the
  code under test never reads any attribute of the instances through normal
  access, no finding is made (this is what "some attribute was read" is for);
  if it reads some attributes normally and the relationship through
  `__dict__`, the relationship is reported wrongly. Ignore it.
- Reads outside the test body are not counted: a relationship that is only
  used by a fixture's teardown, or by code run after the test returns, is
  reported.
- Granularity is the class, not the instance and not the query. A
  relationship read on any instance, or eager-loaded in one query and read
  from the results of another, counts as used.
- Only loader options on the query are tracked. `relationship(lazy="selectin")`
  or `lazy="joined"` set on the mapping are not, and neither is
  `contains_eager()`. `lazy="raise"` works as expected.
- Reads made by SQLAlchemy through the descriptor (some internals, merge, ORM
  events you write) count as reads, which hides findings, never adds them.
- A query that returns no rows, or rows nothing looks at, gives no finding.
- Deletes are noticed at flush (`before_flush`); a delete that is never
  flushed inside the test body is not seen.
- `immediateload` loads each parent with its own statement, so the same query
  is also reported as an N+1.
- The check reads private parts of SQLAlchemy's loader options
  (`Load.context`, each element's `strategy` and `path`). If they change, the
  check turns itself off with a `QueryGuardInternalWarning` instead of failing
  tests; the rest of the plugin keeps working.
- While the option is on, every attribute read on a mapped instance pays for
  the wrapper (see the table), also for code outside the test body.
