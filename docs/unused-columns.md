# Unused columns (experimental)

A query that loads every column of a wide table, when the code reads two of
them, moves data it never uses: large text and JSON columns travel to the
client, get decoded and are kept in the Session.

```python
rows = session.scalars(select(Account)).all()  # 12 columns
names = [a.name for a in rows]                  # 1 of them is read
```

Set `queryguard_unused_columns` to turn the check on:

| Value | Effect |
|---|---|
| `off` (default) | Nothing is installed; there is no cost. |
| `warn` | A `QueryGuardWarning`; the test passes. |
| `fail` | The test fails, in `fail` mode, unless `queryguard_fail_on` leaves the kind out. |

`queryguard_mode = off` turns this check off too. The finding has the kind
`unused-columns`, the label of the class (`Account`), and the file and
function of the query that loaded the rows:

```text
queryguard: unused columns detected (experimental)

  Account: 12 columns loaded, 9 never read
  first at app/accounts.py:41 in list_names
  kind: unused-columns  label: Account
  docs: https://maciejziel.github.io/pytest-queryguard/unused-columns.html
  never read: email, phone, street, city, country, notes, bio, avatar, settings
  fix: load only what is used, .options(load_only(Account.name, Account.owner_id)),
  or defer() the large columns that are never read
```

The suggested `load_only()` lists the columns that were read and the columns
the ORM needs that `load_only()` does not add by itself (foreign keys and the
other columns of relationships, the discriminator, the version counter); the
primary key is always loaded. For a query that is used by other code too,
`defer()` of the large columns is the smaller change.

## Opting out

The kind works with the usual mechanisms:

```python
@pytest.mark.allow_nplusone(kind="unused-columns")
def test_export(session): ...

@pytest.mark.allow_nplusone("Account")  # one class
def test_report(session): ...
```

`unused-columns Account function=list_names` in `queryguard_ignore`,
`with ignore("unused-columns"):` around the query, and the baseline work the
same.

## How it decides

While the option is on, the plugin does two things inside the body of each
test (fixtures and teardown are not measured):

1. The `load` and `refresh` instance events of each query's rows note which
   mapped columns each instance has loaded (those in its `__dict__`: columns
   left out by `load_only()`, `defer()` or `deferred()` are not loaded),
   grouped by class and by the file and function of the query. Only the
   query's own entities count: instances loaded through a relationship
   (lazy loads, `selectinload`, `joinedload`) are not grouped.
2. `InstrumentedAttribute.__get__` is wrapped, and every attribute read of a
   grouped instance is noted for its group. Reads by the unit of work
   (flush, cascades, backrefs) do not go through it.

A group is reported when all of these hold:

- its instances had at least **8** columns loaded (`MIN_LOADED` in
  `pytest_queryguard/unused_columns.py`);
- at most **half** of them were read (`MAX_READ_SHARE`), where the primary
  key, foreign keys and other columns of relationships, the discriminator,
  the version counter and the columns of a `composite()` count as read;
- some attribute of some of its instances was read, so the code does look at
  these instances through attribute access.

The thresholds are constants, not options, while the check is experimental.

Objects the test creates itself are never grouped: they are not loaded. An
object the Session already has, expired (after `commit()` or `expire()`),
that a query loads again is grouped like a new one, with only the columns
that query populated (those it had before do not count as loaded). `session.refresh(obj)`
and the reload of one expired attribute are not queries of rows and are not
grouped.

### Why this approach

| Approach | Verdict |
|---|---|
| Attribute events (`set`, `init_scalar`, `modified`) | A read of a loaded attribute fires no event. Not usable. |
| Inspect `InstanceState` (`unloaded`, `dict`) at the end | Shows what is loaded, not what was read. |
| Wrap `InstrumentedAttribute.__get__` while enabled (chosen) | One dictionary lookup per read, only while a test body runs; the same for sync and async sessions. The wrapper is shared with the [unused eager-load check](unused-eager-load.md), applied only when one of the two options is on and removed at the end of the session. |

Instances are noted by `id()`, with their `InstanceState`, in a dictionary
that lives for one test body, not on the instance: a read then costs one
lookup and one comparison of the state, so an `id()` that Python reuses for
another object after an instance is gone is not taken for it. A test body
that loads more than 100,000 instances (`MAX_INSTANCES`) gives no verdict for
the groups past that point. An error inside the wrapper turns the check off,
with a warning, and the read goes on.

Measured with `scripts/bench_columns_overhead.py` (CPython 3.13, SQLAlchemy
2.1, SQLite in memory, 12-column class, the plugin's listeners on in all three
rows, median of 7 runs, one machine pinned to two cores):

| Mode | ns per attribute read | us per ORM SELECT of 50 rows |
|---|---|---|
| option off (default) | 169 | 475 |
| on, outside a test body | 210 | 492 |
| on, inside a test body | 342 | 538 |

That is about 0.3 us per loaded row while idle and 1.3 us while tracked, and
about 170 ns per attribute read inside a test body. With the option off,
`scripts/bench_overhead.py` shows the same overhead as before the change
(Core 31 %, ORM 42 to 46 %, within run-to-run noise).

## Limitations

The check is conservative on purpose (when unsure it reports nothing) but it
can still be wrong.

- Code that reads `instance.__dict__`, `vars(instance)` or the instance state
  directly is invisible; if it reads some columns normally and others that
  way, the others are reported wrongly. Serializers that use `getattr()`
  (Pydantic `from_attributes`, Marshmallow, `dataclasses.asdict`) are seen.
- Reads outside the test body are not counted: a column only read by a
  fixture's teardown, or by code run after the test returns, counts as never
  read.
- Granularity is the class and the function that ran the query. Two queries
  of one class in one function are one group: the columns either loads, and
  the reads of either's rows, are added up. A row that a second query returns
  again, without loading it, stays in the group of the first.
- Rows loaded through relationships are not checked; for those, use
  `selectinload(Parent.children).load_only(...)` by hand.
- Columns written but never read count as unused; `load_only()` still lets
  the code write them.
- A test that reads a column on only some rows counts it as read: the check
  is about which columns a code path uses, not about each row.
- `column_property()` expressions count as columns; a `composite()`'s columns
  count as read, since a composite reads them without the descriptor.
- The check reads the call site and load path that the plugin's own listeners
  note on each instance. If they are unknown (a row two queries of different
  places loaded), the instance is not grouped.
- While the option is on, every attribute read on a mapped instance pays for
  the wrapper and every loaded row for the listener (see the table), also for
  code outside the test body.
