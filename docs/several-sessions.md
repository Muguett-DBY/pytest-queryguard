# Tests with several sessions or requests

The repeat rules count how often something happens. By default they count over
the whole test, and a test that makes several independent requests then looks
like a loop although nothing loops:

```python
def test_five_requests(client, org):
    for _ in range(5):
        client.get(f"/orgs/{org.id}")   # each request: its own Session, the
                                        # same authentication lookup
```

Every request opens its own `Session`, runs the same lookups, and closes it.
The statement runs five times in the test and once in each request.

## The option

```toml
[tool.pytest.ini_options]
queryguard_repeat_scope = "session"
```

| Value | What is counted together |
|---|---|
| `test` (default) | Everything the test runs, as in 0.3. |
| `session` | The statements, lazy loads, column loads and writes of one scope. A finding needs one scope to reach the threshold. |

It applies to four kinds: `repeated-statement` (also the small-repeat rule),
`relationship-load`, `column-load` and `write-loop`. `missing-index` and
`unused-eager-load` do not count occurrences and are not affected. The default
stays `test`, so an upgrade changes nothing until you set the option.

With `test`, a finding whose occurrences come from more than one scope says so:

```
  the same statement ran 9 times across 8 different Sessions or connection checkouts
  first at app/auth.py:31 in current_user
  kind: repeated-statement  label: SELECT users.id FROM users WHERE users.token = ?
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#repeated-statement
  note: if each belongs to a separate request or unit of work, this is not a
  loop; set queryguard_repeat_scope = session to count each Session on its
  own (QueryLog.assert_no_nplusone: repeat_scope='session')
```

With `session`, the message says `ran 6 times in one Session or connection
checkout`, and the count is the largest one scope has. The identity of a finding
(kind, label, file and function) and the baseline format do not change.

`QueryLog.assert_no_nplusone(repeat_scope="session")` does the same for a
recorded block; like the thresholds it does not read the ini option.

## How the scope is decided

- A statement run through a `Session` (ORM queries, `session.execute(text())`,
  `session.connection()`) belongs to that `Session`: the plugin listens to
  `after_begin`, which hands it the `Session` and the connection it opened for
  the transaction. All transactions of one `Session` are one scope, so a loop
  with a `commit()` in every round is still a loop.
- A statement on a connection no `Session` opened (`engine.connect()`, Core) is
  one scope per connection checkout. A checkout that runs `commit()` and goes on
  is still one scope.
- Lazy loads and column loads belong to the `Session` that loaded them.
- Async works the same: `AsyncSession` and `AsyncConnection` wrap a sync
  `Session` and `Connection`, and those are what the plugin sees. Concurrent
  tasks each have their own `AsyncSession`, so they are separate scopes.
- A pooled or `StaticPool` connection that several checkouts share does not
  merge their scopes: every checkout gets a new `Connection` object, which is
  what the scope is attached to. A `Session` bound to one `Connection` that
  several `Session`s share (the "test transaction" pattern) is separated by
  `Session`, because each `Session` transaction tags the connection again.

## Limits

- One `Session` is one scope. If a test passes the same `Session` to every
  request (a dependency override that returns the fixture's session), the
  requests are one scope and the repeats add up again. Give each request its
  own `Session` in the override, or keep `test`.
- Two `Session`s that use one shared `Connection` at the same time are not told
  apart; the later transaction's `Session` wins until the next one starts.
- A loop that opens a new `Session` per round (`for x in xs: with Session()
  as s: s.get(...)`) is a real N+1 that `session` cannot see, because every
  round is its own scope. With `test` it is reported, with the note above.
  This is the cost of the option; check that a report with the note is really
  independent requests before you set it.
- A statement the plugin cannot attach to a `Session` or `Connection` object (a
  connection type that refuses attributes) counts as one unknown scope.
- The scope is attached to the Python objects, not to the database
  transaction, so it is unrelated to isolation levels or the pool.
