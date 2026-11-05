# In-application mode: N+1 detection per request

| | |
|---|---|
| Status | Implemented, experimental (0.5.0) |
| Author | Maciej Zieliński |
| Date | 2026-10-13 |
| Target release | 0.5.0 (released as experimental) |
| Extends | [design.md](design.md), [design-0.3-api.md](design-0.3-api.md) |

## 1. Problem

pytest-queryguard only measures inside pytest: a collector is pushed around
each test, and class-level listeners on `Engine` and `Session` feed every
active collector (a process-wide stack). That is right for a test suite, where
one test runs at a time, but it cannot run inside a development server:

- concurrent requests (asyncio tasks, worker threads) would all write into the
  same stack, so one request's statements would be reported for another;
- the listeners are registered on every `Engine` in the process, not on the
  ones the application chooses;
- nothing reports per request, and there is nothing to look at while clicking
  through the application.

## 2. Goals

1. Run the same detectors (relationship loads, repeated statements, column
   loads, optional small repeats and write loops) on every HTTP request of a
   running ASGI or WSGI application, and report findings through `logging`,
   `warnings` or a callback, with optional per-route query budgets.
2. Statements of concurrent requests, tasks and threads never mix. The one
   exception is a thread pool on builds where threads inherit the context,
   used without `copy_context().run` (see the context-local collector below).
3. Opt in per engine: `watch(engine)` and `unwatch(engine)`, for `Engine`
   and `AsyncEngine`.
4. No cost when not enabled: importing the module registers nothing.
5. Never break the application: every internal error degrades to one log
   record and switches the in-app mode off.
6. An optional development panel (JSON and a self-contained HTML page).
7. The pytest plugin keeps its behaviour, its listeners and its tests.

Non-goals: production monitoring, persistence, user accounts or
roles for the panel, the missing-index check (it runs `EXPLAIN` on the request's
connection; that belongs in tests), and framework-specific integrations
beyond pure ASGI and WSGI.

## 3. API

A new subpackage, `pytest_queryguard.app`, importable without pytest:

```python
from pytest_queryguard import app as queryguard

queryguard.watch(engine)                      # Engine or AsyncEngine
asgi_app = queryguard.QueryGuardASGI(asgi_app, budgets={"GET /orders": 3})
wsgi_app = queryguard.QueryGuardWSGI(flask_app.wsgi_app, report="warn")

with queryguard.track("nightly-job") as report:  # any unit of work
    run_job()
```

| Name | Meaning |
|---|---|
| `watch(engine)`, `unwatch(engine)` | Attach and detach the in-app listeners to one engine. Idempotent. |
| `track(name, *, settings)` | Everything watched that runs in this context is one unit, reported when it ends: a `with`/`async with` block (yields a live `RequestReport`), a decorator (one unit per call; coroutine and generator functions included), or `start()`/`stop()` for hooks that begin and end a unit separately. |
| `QueryGuardASGI(app, ...)` | Pure ASGI middleware: one `track()` per `http` request. |
| `QueryGuardWSGI(app, ...)` | WSGI middleware: the same for WSGI. |
| `Settings` | Thresholds, `repeat_scope`, `budgets`, `default_budget`, `report`, `max_events`, `sample_rate`, `min_db_time`. |
| `opentelemetry_reporter()`, `sentry_reporter()` | Ready-made `report` callables: events on the current OpenTelemetry span, Sentry messages fingerprinted by the finding's identity. |
| `RequestReport` | Method, path, route, status, statements (`Query`), times, findings (`Finding`), budget. |
| `set_route(template)` | Names the route of the current request for budgets (Flask). |
| `internal_error()` | The error that switched the in-app mode off, or None. |

## 4. Decisions

- **Context-local collector.** One `ContextVar` holds the current
  `RequestReport`'s collector. asyncio copies the context into each task,
  Starlette copies it into its thread pool, and SQLAlchemy's async greenlets
  share the awaiting task's context (`gr_context`), so a request's tasks and
  `run_in_threadpool` calls see its collector while concurrent requests do
  not. A plain `threading.Thread` or `ThreadPoolExecutor` without
  `contextvars.copy_context()` starts empty, so its statements are not
  attributed to any request: missing, never mixed. Where threads inherit the
  creator's context (Python 3.14 free-threaded builds, or
  `-X thread_inherit_context=1`), a pool worker keeps the context of the
  request that created it, so other requests' tasks can be attributed to that
  request while it runs. Once that request has finished, the listeners see
  its tracker as finished and drop the worker's statements (`_core.live()`).
  Submitting each task with `copy_context().run` avoids both on every build;
  `docs/in-app.md` says so.

- **Separate listeners, per engine.** The in-app handlers live in
  `app/_listeners.py` and are registered with `event.listen(engine, ...)` on
  the watched engine (the `sync_engine` of an `AsyncEngine`). The ORM events
  (`do_orm_execute`, `after_begin`) exist only on `Session`; they are
  registered on the `Session` class while at least one engine is watched, and
  return after one `ContextVar.get()` outside a request. A relationship or
  column load is only kept when its statement reaches a watched engine: a
  `before_cursor_execute` listener on the `Engine` class, registered with
  them, drops a pending load whose SELECT runs on an unwatched engine, so
  sessions on other engines add nothing. The engine is never guessed with
  `Session.get_bind()`, which may be application code (`ShardedSession`'s
  `shard_chooser`, Flask-SQLAlchemy's binds, replica routers). The pytest listeners in
  `listeners.py` are not changed; the in-app handlers reuse its pure helpers
  (`_is_write`, the scope numbers) and the shared `QueryCollector`,
  `detect()` and `make_findings()`.

- **Own error state.** The pytest listeners stop on their first error through
  a module global. The in-app mode has its own, so an in-app bug never turns
  off the measurement of a test run, and the reverse.

- **Reporting after the response.** Detection runs once, when the request
  ends (after the last byte for ASGI, after `close()` of the response
  iterable for WSGI), never on the statement path.

- **Route templates for budgets.** Budgets are keyed by `"METHOD path"` or
  `"path"`, with `fnmatch` patterns. Starlette and FastAPI put the matched
  route in `scope["route"]`, whose `path` is the template (`/orders/{id}`).
  WSGI has no route, so the path is used unless the application calls
  `set_route()` (a two-line Flask `before_request` hook in the docs).

- **Panel.** Off by default; `panel=True` serves `<panel_path>/` (HTML) and
  `<panel_path>/requests.json` from the middleware itself, the last
  `panel_history` reports. The HTML has inline CSS and JS only and renders
  text with `textContent`. By default it answers loopback clients only, and
  only for a loopback `Host` header (against DNS rebinding); that
  is a guard against accidental exposure, not access control. Access
  control is `panel_token`, required with `panel_loopback_only=False` (a
  `ValueError` when the middleware is created without it): at least 16
  characters of the RFC 6750 token alphabet. `requests.json` then answers
  only `Authorization: Bearer <token>`, compared with `hmac.compare_digest`,
  and anything else gets `401` with `WWW-Authenticate: Bearer`. The checks
  run in this order: method, loopback client and `Host` (when on), token.
  The HTML page holds no data and is served without the token; it reads the
  token from its URL fragment (`#token=...`), which browsers do not send, and
  puts it in the `Authorization` header of its fetch, so the token never
  appears in a request URL or a server log. A header, not a cookie, so
  another site cannot make the browser send it (no CSRF). Bound parameters
  are never stored in the panel.

- **Memory.** `max_events` (default 1000) caps the statements kept per
  request; past it they are only counted, as in the plugin.

- **Sampling.** `sample_rate` decides per unit, with `random.random()`, when
  the unit starts. The middleware then creates no tracker at all; `track()`
  creates one that is finished from the start, so `finish()` reports
  nothing. Its `Active` points at the enclosing unit's collector and tracker
  (or at itself, so `live()` drops the statements, when there is none), with
  `owner` set so `__exit__` still ends its own unit. `min_db_time` compares the unit's total `db_time`, since a
  finding has no time of its own; below it the detectors do not run.

- **Reporters.** `opentelemetry_reporter()` and `sentry_reporter()` are plain
  `report` callables (`app/_reporters.py`); they import their library when
  created and go through the same error handling as any callable.

## 5. Risks

- Statements a request starts in a thread without a copied context are not
  counted (documented).
- Starlette background tasks run inside the request's ASGI call, so their
  statements count for the request (documented).
- The `Session`-class listeners cost one `ContextVar.get()` per ORM execution,
  and the `Engine`-class one a `ContextVar.get()` per statement, in the whole
  process while any engine is watched.
