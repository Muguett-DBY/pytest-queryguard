# In-application mode

pytest-queryguard normally runs in your tests. The in-application mode runs the
same N+1 rules and query budgets on every HTTP request of a running ASGI or
WSGI application, so you see findings while you click through a development
server or a staging environment.

> **Development and staging only.** The mode adds work to every statement of
> a watched engine, and the optional panel shows SQL text to anyone who can
> reach it. Never enable either in production. The examples below switch it
> on only when `APP_ENV` is `development`.

It ships from 0.5.0 on. It is experimental (see
[Stability and versioning](stability.md#experimental)):
names in `pytest_queryguard.app`, their settings and the panel's JSON may still
change in a minor release, without a deprecation period.

## FastAPI and Starlette

Watch the engine, add the middleware. A shortened version of `app/main.py` in
[examples/in-app](https://github.com/MaciejZiel/pytest-queryguard/tree/main/examples/in-app),
which also has the endpoints and a `demo.py` that sends requests to them:

```python
import os

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pytest_queryguard import app as queryguard

engine = create_async_engine("sqlite+aiosqlite:///app.db")
sessions = async_sessionmaker(engine, expire_on_commit=False)
app = FastAPI()

# Development only: never in production.
if os.environ.get("APP_ENV") == "development":
    queryguard.watch(engine)
    app.add_middleware(
        queryguard.QueryGuardASGI,
        budgets={"GET /orders/{order_id}": 1},
        panel=True,
    )
```

`watch()` takes an `Engine` or an `AsyncEngine`. Call it at startup, before the
engine hands out connections. `QueryGuardASGI` is plain ASGI middleware and
does not import Starlette, so it also wraps any other ASGI 3 application:
`app = queryguard.QueryGuardASGI(app)`.

A request to the endpoint with a lazy-load loop logs a WARNING on the
`pytest_queryguard.app` logger. This is the output of `python demo.py` in the
example (FastAPI 0.141, Starlette 1.7, SQLAlchemy 2.1):

```text
WARNING pytest_queryguard.app: queryguard: GET /orders: 6 statements

queryguard: N+1 detected

  Order.customer was lazy-loaded 5 times
  first at app/main.py:48 in list_orders
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
```

`GET /orders/3` runs two statements against a budget of one:

```text
WARNING pytest_queryguard.app: queryguard: GET /orders/3 (route /orders/{order_id}): 2 statements

queryguard: expected at most 1 query, got 2

  1. SELECT orders.id, orders.customer_id FROM orders WHERE orders.id = ?  (app/main.py:64 in get_order)
  2. SELECT customer.id, customer.name FROM customer WHERE customer.id = ?  (app/main.py:65 in get_order)

  database time: 226 us
  slowest:
       127 us  SELECT orders.id, orders.customer_id FROM orders WHERE orders.id = ?  (app/main.py:64 in get_order)
        99 us  SELECT customer.id, customer.name FROM customer WHERE customer.id = ?  (app/main.py:65 in get_order)
```

A request without findings and within its budget logs nothing.

A request's `status` is the one in the response it sends. An endpoint that
raises before sending one is reported with `500`. In Starlette and FastAPI,
a handler for `500` or `Exception`
(`@app.exception_handler(Exception)`) runs in Starlette's outermost
`ServerErrorMiddleware`, outside every `add_middleware()` layer. The report
then shows `500` even when that handler answers with another status, and
the handler's statements are not counted. To include both, wrap the whole
application instead of calling `add_middleware()`:
`app = queryguard.QueryGuardASGI(app, ...)`. Handlers for other exceptions and
statuses run inside the middleware and are reported as sent.

## Flask and other WSGI applications

Wrap `app.wsgi_app`. WSGI has no route templates, so a `before_request` hook
tells queryguard the Flask rule; without it, budgets match the request path.

```python
import os

from flask import Flask, request
from sqlalchemy import create_engine

from pytest_queryguard import app as queryguard

engine = create_engine("sqlite:///app.db")
app = Flask(__name__)

# Development only: never in production.
if os.environ.get("APP_ENV") == "development":
    queryguard.watch(engine)
    app.wsgi_app = queryguard.QueryGuardWSGI(
        app.wsgi_app, budgets={"GET /orders/<int:order_id>": 1}
    )

    @app.before_request
    def name_the_route() -> None:
        # WSGI has no route templates; tell queryguard the Flask rule.
        if request.url_rule is not None:
            queryguard.set_route(request.url_rule.rule)
```

With Flask-SQLAlchemy, the engines exist only inside an application context:

```python
with app.app_context():
    queryguard.watch(db.engine)  # the default bind
    queryguard.watch(db.engines["audit"])  # a bind from SQLALCHEMY_BINDS
```

Only watched engines count. A bind you do not watch adds no statements and
no findings to the request, even when one Session uses several binds.

The same lazy-load loop in a Flask view:

```text
WARNING pytest_queryguard.app: queryguard: GET /orders: 6 statements

queryguard: N+1 detected

  Order.customer was lazy-loaded 5 times
  first at app/flask_app.py:41 in list_orders
  kind: relationship-load  label: Order.customer
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = ?
  fix: load it with the parent query, e.g.
       select(Order).options(joinedload(Order.customer))
```

A WSGI request ends when its response body has been read to the end or closed,
as every WSGI server does. Iterator initialization and response cleanup run in
that request's context. When the body is exhausted, the middleware closes it
before finalizing the report, so queries in cleanup callbacks (such as Flask's
`Response.call_on_close`) count too. A later `close()` does not run those
callbacks again. Because of that early close, an exception raised by the
application's `close()` reaches the server from the last step of iteration
instead of from the server's own `close()` call; the report is finalized either
way. The Flask test client does neither on its own: read the body
(`response.data`, `response.get_json()`), use
`with client.get(...) as response:`, or pass `buffered=True`.

When the server provides `wsgi.file_wrapper` and the application returns an
object created by it (for example, a Flask `send_file()` response), queryguard
returns that native object unchanged. The server can still recognize it for
its optimized file transfer. Operations on the supplied file run in the
request's context; the report ends when the server closes the native wrapper,
including on a partial transfer or a response whose body is not read. Queries
in the file's cleanup count too. File methods such as `fileno()`, `seek()` and
`tell()` remain available. This does not enable a native transfer on servers
that do not already support one.

## Outside HTTP

`track()` measures any unit of work that is not a request: a job, a CLI
command, a message handler, a task of Celery or RQ. Each unit is reported
like a request, with its own budget (`default_budget`):

```python
from pytest_queryguard import app as queryguard

with queryguard.track("nightly export", default_budget=50) as report:
    ...  # run the job
print(report.statement_count, [f.label for f in report.findings])
```

As a decorator, each call is a unit, named after the function unless a name
is given. A coroutine function is measured across its awaits, a generator
function over its whole iteration, and `async with` works too:

```python
@queryguard.track(default_budget=20)
def export_invoices(month): ...

@queryguard.track("orders consumer")
async def handle(message): ...

async with queryguard.track("sync accounts") as report: ...
```

Units in concurrent tasks or threads never mix: each counts the statements
of its own context, also when they enter one shared `track()` object (a
module-level `UNIT = queryguard.track("handler")`).

### Celery

Decorate the task function under `@app.task`, or measure every task from
the worker's signals, which run in the task's context:

```python
from celery.signals import task_postrun, task_prerun
from pytest_queryguard import app as queryguard

queryguard.watch(engine)
_units: dict[str, queryguard.track] = {}


@task_prerun.connect
def _start_unit(task_id, task, **_):
    unit = queryguard.track(task.name, default_budget=100)
    unit.start()
    _units[task_id] = unit


@task_postrun.connect
def _stop_unit(task_id, state=None, **_):
    unit = _units.pop(task_id, None)
    if unit is not None:
        unit.stop(failed=state != "SUCCESS")
```

`start()` and `stop()` are the two halves of the `with` block for code that
cannot wrap the work in one: `stop()` returns the `RequestReport`, and
`failed=True` keeps a warning turned into an error from being raised over
the task's own failure. One object runs one such unit at a time; starting it
again, or stopping it twice, raises `RuntimeError`.

### RQ

RQ runs a job function by its import path, so decorating the function is
enough: `@queryguard.track()` on the function the job names. Call
`queryguard.watch(engine)` where the worker imports the application.

## Settings

`QueryGuardASGI`, `QueryGuardWSGI` and `track()` take these as keyword
arguments, or one `queryguard.Settings(...)` object as `settings=`.

| Setting | Default | Meaning |
|---|---|---|
| `relationship_threshold` | 2 | Lazy loads of one relationship for different parents that count as N+1 |
| `repeat_threshold` | 5 | Runs of one `SELECT` shape that count as N+1 |
| `column_load_threshold` | 5 | Instances loading deferred or expired columns on their own |
| `small_repeat_threshold` | None | Optional; as in the plugin |
| `write_loop_threshold` | None | Optional; as in the plugin |
| `scope` | `"request"` | `"request"`: everything in a request counts together; `"session"`: each Session on its own. Before 0.8.0 `repeat_scope`, which still works with a `QueryGuardDeprecationWarning` until 2.0 |
| `budgets` | `{}` | `"METHOD path"` or `"path"` to the most statements a request may run |
| `default_budget` | None | The budget of a request no key matches |
| `report` | `"log"` | `"log"`, `"warn"`, a callable, or a list of them |
| `max_events` | 1000 | Statements stored per request; more are counted, not stored |
| `sample_rate` | 1.0 | Fraction of requests measured, from 0 to 1 |
| `min_db_time` | 0.0 | Seconds of database time below which a request's N+1 rules do not run |
| `page_report` | `"off"` | Since 0.8.0. `"footer"` or `"console"`: add a request's findings to its own HTML page (see [Findings in the page](#findings-in-the-page)) |

The rules are the plugin's; see [How detection works](how-detection-works.md).
The missing-index check does not run in the application.

**Budgets.** A key's path is an `fnmatch` pattern matched against the route
template (`/orders/{order_id}` in FastAPI and Starlette, the rule given to
`set_route()` in Flask) and against the request path; the first matching key
wins, so put specific keys first: `{"GET /orders/{order_id}": 2, "/admin/*": 50}`.
The method is the key's first word when it is letters and hyphens only; the rest of the
key, spaces included, is the path (`"GET /files/my docs/*"`).
In an application mounted inside another (`app.mount("/api", sub_app)`), the
template is the one inside the mounted application, without the mount prefix;
the request path keeps it.

**Reports.** `report` is used only for requests with findings or over their
budget. `"log"` writes one WARNING record. `"warn"` issues
`QueryGuardWarning`, which follows Python's warning filters: with
`-W error::pytest_queryguard.QueryGuardWarning` it becomes an exception,
raised when the request ends, and only when the application did not raise
its own: the application's exception always wins, and the other targets
still run. In an ASGI server the exception comes after the response has been
sent, so the client gets the whole response and the server logs the error
(uvicorn: `Exception in ASGI application`). A WSGI response has started
before its report is ready, and an exception raised from its body would only
cut it short (a chunked response loses its final chunk under gunicorn and
waitress), so the WSGI middleware logs it instead: one ERROR record on
`pytest_queryguard.app`, starting `QueryGuardWarning:`. `track()` raises it
as usual. Every reported request warns: the `default` and `module` actions
do not hide a repeated message, while `once` keeps every distinct message in
Python's process-wide registry. A callable receives a `RequestReport` with `method`, `path`, `route`, `status`,
`statements` (`Query` objects with `sql`, `callsite`, `relationship` and
`duration`), `statement_count`, `db_time`, `duration`, `findings`, `budget`,
`over_budget` and `message`:

```python
from pytest_queryguard import app as queryguard


def to_my_tracker(report: queryguard.RequestReport) -> None:
    for finding in report.findings:
        print(report.method, report.route or report.path, finding.kind, finding.label)


middleware_options = {"report": ["log", to_my_tracker]}
```

An exception in a callable is logged and does not reach the request.

**Sampling and fast requests.** `sample_rate=0.1` measures one request in
ten, chosen at random; the others run as if the middleware were not there
(no collector, no report, not in the panel), and their statements cost the
watched engine's listeners one `ContextVar.get()` each. A `track()` unit
left out the same way yields a `RequestReport` with `sampled=False` and
nothing in it; statements inside it still count for an enclosing request or
unit that was measured.

`min_db_time=0.005` skips the N+1 rules for a request whose database time
was below 5 ms. The value compares `RequestReport.db_time`: the summed
duration of every statement the request ran on watched engines, stored or
only counted, from the cursor execute to its end (a statement that raises
adds nothing). It is not the time of the
finding's own statements, which queryguard does not total: an N+1 of five
fast lazy loads in a request with one slow query is still reported. Such a
request has no findings, and it is reported only when it is over its budget;
budgets do not depend on `min_db_time`.

## Reporting to OpenTelemetry and Sentry

Two ready-made `report` callables send findings to the tools you may already
watch. Each imports its library when you create it, so a missing package
raises `ImportError` at startup; neither library is a dependency of
pytest-queryguard. Like any callable, an exception inside one (a Sentry
outage) is logged and does not reach the request.

```python
from pytest_queryguard import app as queryguard

app.add_middleware(
    queryguard.QueryGuardASGI,
    report=[
        "log",
        queryguard.opentelemetry_reporter(),
        queryguard.sentry_reporter(),
    ],
)
```

`opentelemetry_reporter()` (needs `opentelemetry-api`) writes to the span
that is current when the request ends (for a `track()` unit, the span
around the block), so the OpenTelemetry instrumentation must wrap the
queryguard middleware: wrap the `OpenTelemetryMiddleware` of the ASGI or
WSGI instrumentation around it, or instrument FastAPI with
`FastAPIInstrumentor` after adding queryguard. Nothing is written when no
span is recording, for example when the request's span has already ended.
A WSGI request read to the end is reported while its body is iterated; one
closed before its end is reported from `close()`, which the WSGI
instrumentation may call outside its span, so such a report can miss it. The span gets the attributes
`queryguard.statement_count`, `queryguard.db_time` (seconds),
`queryguard.findings` (how many), `queryguard.budget` (when there is one)
and `queryguard.over_budget`; each finding adds a `queryguard.finding` event
with `queryguard.kind`, `queryguard.label`, `queryguard.file`,
`queryguard.line`, `queryguard.function`, `queryguard.count` and
`queryguard.fingerprint`, and a request over budget a `queryguard.budget`
event with `queryguard.budget` and `queryguard.statement_count`. Attributes
without a value (a finding with an unknown call site) are left out.

`sentry_reporter(level="warning")` (needs `sentry-sdk`, initialized with
`sentry_sdk.init()` as usual) sends one `capture_message` per finding, titled
`queryguard: <kind> <label>`. Its fingerprint is
`["queryguard", <fingerprint>]`, where the fingerprint is the SHA-256 of the
finding's identity (kind, label, file and function), as in the
[SARIF file](sarif.md), so one N+1 stays one Sentry issue across requests,
counts and line numbers. Each message has the tag `queryguard.kind` and a
`queryguard` context with the request (`name`, `method`, `path`, `route`,
`status`, `statement_count`, `db_time`, `budget`) and the finding (`file`,
`line`, `function`, `count` and the full `message`). A request over budget
sends one more message, `queryguard: <route> ran <n> statements, budget <b>`,
grouped by method and route; without a route (WSGI without `set_route()`,
a `track()` unit) by method alone, with the path in the title and context.

Both report only what the `report` targets get: requests with findings or
over their budget.

## Findings in the page

Since 0.8.0.

Like Bullet in Rails, the middleware can put a request's findings into the
HTML page it answers with, so they show up where you are already looking:

```python
app.add_middleware(queryguard.QueryGuardASGI, page_report="footer")
app.wsgi_app = queryguard.QueryGuardWSGI(app.wsgi_app, page_report="console")
```

- `"footer"` adds an `<aside id="queryguard-page-report">` with a one-line
  summary (`queryguard: 1 problem in GET /orders: 6 statements`) and, in a
  `<details>` element, the same text the `log` report writes.
- `"console"` adds a `<script>` that calls `console.warn()` with that text
  and an object with the request, the statement count, the budget and each
  finding's kind, label, file, line, function and count.

The report goes before the page's last `</body>` (in any letter case), or at
the end of a page without one, and `Content-Length` is set to the new
length. Only requests that were measured (see `sample_rate`) and have
findings or are over their budget get one; it reflects the statements run
until the body was complete, and statements of a later background task
count only for the usual report.

Only a response the middleware can change safely gets a report. It must be
`text/html` (any charset in which `</body>` is ASCII, so not UTF-16, nor a
body that starts with a UTF-16 or UTF-32 byte order mark), a 2xx
status other than 204, 205 and 206, an answer to a method other than `HEAD`,
with a `Content-Length` of at most 4 MiB and without `Content-Encoding`
(`gzip`, `br`: wrap a compressing middleware around queryguard's, in
Starlette by adding `GZipMiddleware` after it, so queryguard sees the page
before it is compressed) or `Content-Range`. A streamed response
without `Content-Length` (`StreamingResponse`, a Flask generator) and a file
sent with `wsgi.file_wrapper` pass through as they are. Every other response
passes through untouched, and an HTML response the rules allow is held back
only until its body is complete: ASGI holds its start message and body
parts, WSGI calls `start_response` once the body iterable has been read (an
application that uses the legacy `write()` callable is passed through; a
later `start_response` call with `exc_info` replaces the held page, whose
body is then not sent). A changed page loses its `ETag` and digest headers
(`Content-MD5`, `Digest`, `Content-Digest`, `Repr-Digest`), which described
the original bytes. For such a page the detectors run twice: once when its
body is complete and once when the request ends.

Every value is escaped: the footer's text with `html.escape()` and character
references for non-ASCII characters, the script's data as JSON in which `<`,
`>` and `&` are `\u` escapes, so SQL text, labels and paths cannot end the
element or add markup. An inline script is blocked by a
Content-Security-Policy without `'unsafe-inline'`, and queryguard does not
edit the application's policy. When the response has a
`Content-Security-Policy` or `Content-Security-Policy-Report-Only` header,
or the page a `<meta http-equiv="Content-Security-Policy">` element,
`"console"` falls back to the footer, and the footer has no `style`
attributes, so nothing triggers a violation report.

Like the panel, the page report is for development only: it shows SQL text,
file names and line numbers to whoever loads the page, with no loopback
check or token. Never enable it in production or on a server others reach.

## The panel

`panel=True` serves two pages from the middleware itself, for the last
`panel_history` requests (default 50):

- `/_queryguard/`: a self-contained HTML page (inline CSS and script, no
  third-party code) with each request's statements, timings and findings;
- `/_queryguard/requests.json`: the same data as JSON, newest first.

`panel_path` moves both. The history is kept in memory by each server
process. With several workers (`uvicorn --workers 2`, `gunicorn -w 2`) each
worker has its own history and holds only the requests it served. A page
load or refresh shows whichever worker answered it, so the list changes from one
refresh to the next. Run a single worker while you use the panel, or use a
`report` callable to collect reports from every worker in one place.

By default the panel answers only loopback clients
(`127.0.0.0/8`, `::1`); a peer without an address, such as a connection on a
Unix socket, is refused too. So is a request whose `Host` header names another
site than this machine (`localhost`, `*.localhost` or a loopback address; `testserver` only from
Starlette's test client): a web page whose name was rebound
to `127.0.0.1` cannot read the panel. This guards against exposing it by
accident and is not access control: behind a reverse proxy on the same machine
every client is the proxy (unless the server takes the client address from
the proxy's `X-Forwarded-For`, as uvicorn does for `127.0.0.1` by default),
and a server that trusts proxy headers from anyone (uvicorn's `forwarded_allow_ips="*"`, a `ProxyFix` in front of an open port)
or a tunnel that rewrites the Host header lets a remote client look local.

### A token for staging

`panel_loopback_only=False` lifts both checks, and then the middleware needs a
`panel_token`: without one it raises `ValueError` when it is created. The
token must have at least 16 characters, from letters, digits and `-._~+/`;
`secrets.token_urlsafe(32)` makes one. Keep it out of the code, for example in
an environment variable:

```python
import os

from pytest_queryguard import app as queryguard

# Staging only: never in production.
if os.environ.get("APP_ENV") == "staging":
    queryguard.watch(engine)
    app.add_middleware(
        queryguard.QueryGuardASGI,
        panel=True,
        panel_loopback_only=False,
        panel_token=os.environ["QUERYGUARD_PANEL_TOKEN"],
    )
```

```bash
export QUERYGUARD_PANEL_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

`/_queryguard/requests.json` then answers only a request with
`Authorization: Bearer <token>`; any other gets `401` with
`WWW-Authenticate: Bearer`. The HTML page holds no data and needs no token.
Open it with the token in the URL fragment,
`https://staging.example.com/_queryguard/#token=<token>`: the page reads it
there and sends it in that header. A browser never sends the fragment to the
server, but it stays in the browser's history. A token may also be set with
`panel_loopback_only` on; then both checks apply.

```bash
curl -H "Authorization: Bearer $QUERYGUARD_PANEL_TOKEN" \
    https://staging.example.com/_queryguard/requests.json
```

Serve the panel over HTTPS: the token is a password sent with every refresh.

A WSGI server must pass the `Authorization` header on to the application.
Most do; Apache's mod_wsgi drops it unless `WSGIPassAuthorization On` is set,
and the panel then answers `401` to the right token.

For ASGI applications, `panel_path` is relative to the application's
`root_path`. An application mounted at `/api` serves the default panel at
`/api/_queryguard/` and its history at `/api/_queryguard/requests.json`.
The page uses that same prefix to fetch its history, including when the
mount path contains URL-escaped characters. Already application-relative
ASGI paths are accepted too.
Starlette 0.33 leaves `root_path` empty inside a `Mount`, so there the panel
is not reachable under the mount.

The responses carry a strict
`Content-Security-Policy` (no `'unsafe-inline'`: it allows only the hashes
of the page's own script and style, and requests to the same origin) and
`Cache-Control: no-store`, and the page writes
every value as text, never as HTML. Bound parameter values are never stored
in the panel, but the SQL text is, including any literal the application puts
into it. The panel is off by default; never enable it in production.

The demo prints the panel's JSON for its three FastAPI requests:

```text
--- the panel's requests.json, newest first ---
GET /orders/3 200 2 statements budget 1 []
GET /orders-fixed 200 2 statements budget None []
GET /orders 200 6 statements budget None ['Order.customer']
```

## Concurrent requests

Each request's statements go to a collector held in a `contextvars.ContextVar`,
so concurrent requests never mix:

- asyncio tasks get a copy of the context, so tasks a request starts count for
  it, and other requests' tasks do not;
- SQLAlchemy runs an `AsyncSession`'s work in a greenlet that shares the
  awaiting task's context;
- Starlette runs sync endpoints with `run_in_threadpool`, and
  `asyncio.to_thread()` copies the context too, so their statements count.

A thread you start yourself (`threading.Thread`, a `ThreadPoolExecutor`)
starts with an empty context: its statements are not counted for any request,
unless you run it with `contextvars.copy_context().run(...)`.

Starlette and FastAPI `BackgroundTasks` run after the response has been sent
but before the ASGI call returns, also under `BaseHTTPMiddleware`. Their
statements count for the request that scheduled them. The report is made only
after they finish, so the log record and the panel entry arrive after the
client has its response, and the request's `duration` includes them. The same
holds for a `StreamingResponse`: statements made while its body is produced
count. A request ends when its body is complete or the client disconnects, so
a long-lived response, such as a server-sent events stream, is reported only
when it closes.

Python 3.14 can start threads with a copy of the creating thread's context
instead. That is the default on free-threaded builds (3.14t) and can be turned
on with `-X thread_inherit_context=1` or `PYTHON_THREAD_INHERIT_CONTEXT=1`;
`sys.flags.thread_inherit_context` tells which applies. Then a thread you start
inside a request counts for that request. A pool's worker keeps the context of
the request whose `submit()` created it (workers start lazily, so a pool
created at import time is affected too), for every later task: while that
request is still running, statements from other requests' tasks are counted for
it, and once it has ended they are not counted at all. To attribute pool work
correctly on every build, submit each task in a copy of the current context:

```python
ctx = contextvars.copy_context()
pool.submit(ctx.run, work, arg)
```

`tests/inapp/` checks this with 50 requests in `asyncio.gather`, through
`httpx.AsyncClient`, in a thread pool, and with tasks and threads mixed in one
request.

## Errors and cost

An internal error never reaches the application. The first one logs an ERROR
record on `pytest_queryguard.app` and switches the in-app mode off for the
rest of the process; `queryguard.internal_error()` returns it. It is separate
from the pytest plugin's own error state, so neither switches the other off.

Importing `pytest_queryguard.app` or creating the middleware registers
nothing; only `watch()` adds listeners: to that engine, and, while any engine
is watched, `after_begin` and `after_flush` to `Session`. Outside a request
those shared listeners return after one `ContextVar.get()`. `unwatch()` of
the last watched engine removes them. As the pytest plugin does, the
engine's `before_cursor_execute` listener reads which relationship or column
load an ORM `SELECT` runs for from that statement's own options; there is no
`do_orm_execute` listener, so SQLAlchemy prepares each ORM statement once,
and the `yield_per` issue of SQLAlchemy 2.0.20 to 2.0.49 (see
[Limitations](limitations.md)) does not apply.
`python scripts/bench_overhead.py --app` times a statement with the engine not
watched, watched but outside a request, and inside one. On an in-memory SQLite
database, on a 16-core laptop with the benchmark pinned to two cores
(`taskset -c 6-7`, Python 3.13, SQLAlchemy 2.1; median of 9 runs,
microseconds per statement):

| Statement | Not watched | Watched, outside a request | Inside a request |
|---|---|---|---|
| Core `SELECT` | 40.9 | 46.8 (+5.9) | 54.2 (+13.3) |
| ORM `select()` | 77.2 | 83.5 (+6.3) | 98.4 (+21.2) |

Run to run, these numbers move by a few microseconds; the shared `Session`
listeners cost well under one microsecond of that when timed on their own.
Before the `do_orm_execute` listener was dropped, the same run measured
+11.0 (outside a request) and +31.6 (inside one) for the ORM `select()`.

Treat these as an order of magnitude and measure your own application; see
[Performance](performance.md) for the method.
