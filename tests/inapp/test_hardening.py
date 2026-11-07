"""The in-app mode inside a live application: it must never break a request,
keep memory after it, or check its configuration only when a request comes.
"""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import BackgroundTasks, FastAPI, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from flask import Flask
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pytest_queryguard import app as queryguard
from pytest_queryguard.app import (
    QueryGuardASGI,
    QueryGuardWSGI,
    RequestReport,
    Settings,
    _core,
)

from .conftest import Order


def _lazy_loop(engine: Engine) -> list[str]:
    with Session(engine) as session:
        return [o.customer.name for o in session.scalars(select(Order))]


def _selects(engine: Engine, n: int) -> None:
    with engine.connect() as conn:
        for i in range(n):
            conn.execute(text("SELECT :n"), {"n": i})


# --- configuration errors come at startup ----------------------------------


async def _async_reporter(report: RequestReport) -> None:
    raise AssertionError("never awaited")


class _AsyncCallable:
    async def __call__(self, report: RequestReport) -> None:
        raise AssertionError("never awaited")


class _InheritedAsyncCallable(_AsyncCallable):
    pass


@pytest.mark.parametrize(
    "reporter", [_async_reporter, _AsyncCallable(), _InheritedAsyncCallable()]
)
def test_a_coroutine_reporter_is_refused_at_startup(reporter: Any) -> None:
    # Called without await, it would only make a "never awaited" warning per
    # reported request, and the report would be lost.
    with pytest.raises(ValueError, match="coroutine function"):
        Settings(report=reporter)
    with pytest.raises(ValueError, match="coroutine function"):
        QueryGuardASGI(FastAPI(), report=["log", reporter])
    with pytest.raises(ValueError, match="coroutine function"):
        QueryGuardWSGI(Flask(__name__).wsgi_app, report=reporter)


def test_a_reporter_that_returns_a_coroutine_is_logged(
    watched: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    async def send(report: RequestReport) -> None:
        raise AssertionError("never awaited")

    seen: list[RequestReport] = []
    made: list[Any] = []

    def wrapper(report: RequestReport) -> Any:
        made.append(send(report))
        return made[-1]

    with queryguard.track(report=[wrapper, seen.append]):
        _lazy_loop(watched)
    assert len(seen) == 1  # the other targets still run
    # Closed: no "coroutine ... was never awaited" when it is collected.
    assert [inspect.getcoroutinestate(c) for c in made] == ["CORO_CLOSED"]
    assert "returned a coroutine" in caplog.text


def test_the_report_targets_are_fixed_when_the_settings_are_made(
    watched: Engine,
) -> None:
    seen: list[RequestReport] = []
    targets: list[Any] = [seen.append]
    settings = Settings(report=targets)
    targets.append("print")  # changed later: neither used nor checked
    generated = Settings(report=(t for t in [seen.append]))  # type: ignore[arg-type]
    assert settings.targets == generated.targets == (seen.append,)
    for one in (settings, generated):
        with queryguard.track(settings=one):
            _lazy_loop(watched)
    assert len(seen) == 2


@pytest.mark.parametrize("bad", [None, 5, {"log": True}, {"log"}])
def test_a_report_that_is_not_a_target_is_a_value_error(bad: object) -> None:
    with pytest.raises(ValueError, match="report must be"):
        Settings(report=bad)  # type: ignore[arg-type]


# --- reporters that fail never reach the request --------------------------


class _BrokenSpan:
    def is_recording(self) -> bool:
        return True

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        raise TypeError("misconfigured exporter")


def _broken_reporters(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    trace = ModuleType("opentelemetry.trace")
    trace.get_current_span = lambda: _BrokenSpan()  # type: ignore[attr-defined]
    package = ModuleType("opentelemetry")
    package.trace = trace  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry", package)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", trace)
    sentry_sdk = ModuleType("sentry_sdk")

    def capture_message(message: str, **kwargs: Any) -> None:
        raise ConnectionError("sentry is down")

    sentry_sdk.capture_message = capture_message  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", sentry_sdk)

    def raising(report: RequestReport) -> None:
        raise KeyError("reporter bug")

    return [
        queryguard.opentelemetry_reporter(),
        queryguard.sentry_reporter(),
        raising,
    ]


def test_failing_reporters_never_break_an_asgi_request(
    watched: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[RequestReport] = []
    api = FastAPI()

    @api.get("/orders")
    def orders() -> list[str]:
        return _lazy_loop(watched)

    app = QueryGuardASGI(
        api, default_budget=1, report=[*_broken_reporters(monkeypatch), seen.append]
    )
    with TestClient(app) as client:
        for _ in range(2):
            response = client.get("/orders")
            assert response.status_code == 200
            assert response.json()[0] == "customer 1"
    assert len(seen) == 2
    assert queryguard.internal_error() is None
    for text_ in ("misconfigured exporter", "sentry is down", "reporter bug"):
        assert text_ in caplog.text


def test_failing_reporters_never_break_a_wsgi_request(
    watched: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[RequestReport] = []
    app = Flask(__name__)
    app.wsgi_app = QueryGuardWSGI(  # type: ignore[method-assign]
        app.wsgi_app,
        default_budget=1,
        report=[*_broken_reporters(monkeypatch), seen.append],
    )

    @app.get("/orders")
    def orders() -> list[str]:
        return _lazy_loop(watched)

    client = app.test_client()
    for _ in range(2):
        response = client.get("/orders")
        assert response.status_code == 200
        assert response.get_json()[0] == "customer 1"
    assert len(seen) == 2
    assert queryguard.internal_error() is None
    assert "sentry is down" in caplog.text


# --- ASGI: streaming, background tasks, websockets, lifespan ----------------


def test_asgi_streaming_background_websocket_and_lifespan(watched: Engine) -> None:
    seen: list[RequestReport] = []
    started: list[int] = []

    async def lifespan(app: FastAPI) -> Any:
        _selects(watched, 3)  # startup: no request, nothing counted
        started.append(1)
        yield

    api = FastAPI(lifespan=lifespan)

    @api.get("/stream")
    def stream() -> StreamingResponse:
        def body() -> Iterator[bytes]:
            for i in range(3):
                _selects(watched, 1)
                yield str(i).encode()

        return StreamingResponse(body())

    @api.get("/later")
    def later(tasks: BackgroundTasks) -> str:
        tasks.add_task(_selects, watched, 4)
        return "queued"

    @api.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        _selects(watched, 2)
        await socket.send_text("hello")
        await socket.close()

    app = QueryGuardASGI(api, default_budget=0, report=seen.append)
    with TestClient(app) as client:
        assert started == [1]
        assert client.get("/stream").content == b"012"
        assert client.get("/later").json() == "queued"
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_text() == "hello"
    assert [(r.route, r.statement_count) for r in seen] == [
        ("/stream", 3),
        ("/later", 4),  # the task ran after the response, for this request
    ]
    assert queryguard.internal_error() is None


# --- requests that raise ---------------------------------------------------


def test_a_wsgi_request_that_raises_is_reported_and_propagates(
    watched: Engine,
) -> None:
    seen: list[RequestReport] = []

    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        _selects(watched, 2)
        raise RuntimeError("endpoint bug")

    wsgi = QueryGuardWSGI(app, default_budget=1, report=seen.append)
    with pytest.raises(RuntimeError, match="endpoint bug"):
        wsgi({"REQUEST_METHOD": "GET", "PATH_INFO": "/x"}, lambda *a: None)
    assert [(r.status, r.statement_count) for r in seen] == [(500, 2)]
    assert _core.current.get() is None  # the thread's context is clean again


# --- several engines, one watched after the middleware ---------------------


def test_several_engines_and_one_watched_after_the_middleware(
    watched: Engine,
) -> None:
    seen: list[RequestReport] = []
    late: list[Engine] = []

    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        _selects(watched, 1)
        for engine in late:
            _selects(engine, 2)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    wsgi = QueryGuardWSGI(app, default_budget=0, report=seen.append)
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/x"}
    other = create_engine("sqlite://")
    try:
        late.append(other)  # not watched yet: its statements do not count
        list(wsgi(dict(environ), lambda *a: None))
        queryguard.watch(other)  # after the middleware was installed
        list(wsgi(dict(environ), lambda *a: None))
    finally:
        other.dispose()
    assert [r.statement_count for r in seen] == [1, 3]


# --- per-request state of threads and tasks at once -------------------------


def test_threads_and_asyncio_requests_at_once_keep_their_own_counts(
    watched: Engine,
) -> None:
    seen: list[RequestReport] = []
    lock = threading.Lock()

    def keep(report: RequestReport) -> None:
        with lock:
            seen.append(report)

    def wsgi_app(environ: dict[str, Any], start_response: Any) -> Iterator[bytes]:
        n = int(environ["PATH_INFO"].rsplit("/", 1)[1])
        start_response("200 OK", [("Content-Type", "text/plain")])
        for _ in range(n):
            _selects(watched, 1)
            yield b"."

    async def asgi_app(scope: Any, receive: Any, send: Any) -> None:
        n = int(scope["path"].rsplit("/", 1)[1])
        for _ in range(n):
            await asyncio.to_thread(_selects, watched, 1)
            await asyncio.sleep(0)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wsgi = QueryGuardWSGI(wsgi_app, default_budget=0, report=keep)
    asgi = QueryGuardASGI(asgi_app, default_budget=0, report=keep)

    def thread(n: int) -> None:
        for _ in range(5):
            body = wsgi(
                {"REQUEST_METHOD": "GET", "PATH_INFO": f"/w/{n}"}, lambda *a: None
            )
            for _ in body:
                pass
            body.close()  # type: ignore[attr-defined]

    async def recv() -> dict[str, Any]:
        return {"type": "http.request"}

    async def drop(message: Any) -> None:
        return None

    async def tasks() -> None:
        await asyncio.gather(
            *(
                asgi({"type": "http", "method": "GET", "path": f"/a/{n}"}, recv, drop)
                for n in range(1, 9)
                for _ in range(3)
            )
        )

    threads = [threading.Thread(target=thread, args=(n,)) for n in range(1, 9)]
    for t in threads:
        t.start()
    asyncio.run(tasks())
    for t in threads:
        t.join()
    assert len(seen) == 8 * 5 + 8 * 3
    for report in seen:
        assert report.path is not None
        assert report.statement_count == int(report.path.rsplit("/", 1)[1])


# --- memory ------------------------------------------------------------------


# The memory tests run in a process of their own: the outer pytest plugin
# measures this test's statements too, and its per-test collector keeps them.
def _run(tmp_path: Path, script: str, *args: str) -> list[int]:
    path = tmp_path / "memory_run.py"
    path.write_text(script)
    done = subprocess.run(
        [sys.executable, str(path), *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return [int(word) for word in done.stdout.split()]


KEPT_CONTEXT_RUN = """
import contextvars, gc, tracemalloc
from sqlalchemy import create_engine, text
from pytest_queryguard import app as queryguard

engine = create_engine("sqlite://")
queryguard.watch(engine)
kept = []
blob = "x" * 200_000
gc.collect()
tracemalloc.start()
before = tracemalloc.get_traced_memory()[0]
with engine.connect() as conn:
    for unit in range(10):
        with queryguard.track("job", report=[]) as report:
            for i in range(10):
                conn.execute(text("SELECT length(:p)"), {"p": f"{blob}{unit}{i}"})
            kept.append(contextvars.copy_context())
gc.collect()
print(report.statement_count, len(report.statements),
      tracemalloc.get_traced_memory()[0] - before)
"""


def test_a_context_kept_after_the_unit_does_not_keep_its_statements(
    tmp_path: Path,
) -> None:
    # A pool worker that inherited a request's context, or a task the request
    # started and left running, keeps that context after the request ends.
    count, stored, retained = _run(tmp_path, KEPT_CONTEXT_RUN)
    assert count == stored == 10  # the report itself keeps its statements
    # Each unit ran 2 MB of parameters; without the release all 20 MB stay.
    assert retained < 2_000_000, retained


MEMORY_RUN = """
import asyncio, gc, logging, sys, tracemalloc
from sqlalchemy import create_engine, literal_column, select
from pytest_queryguard import app as queryguard

logging.getLogger("pytest_queryguard.app").setLevel(logging.CRITICAL)
engine = create_engine("sqlite://")
queryguard.watch(engine)
conn = engine.connect()
reported = 0

def count(report):
    global reported
    reported += 1

def broken(report):
    raise RuntimeError("reporter bug")

def work():
    conn.execute(select(literal_column("1")))
    conn.execute(select(literal_column("2")))

async def asgi_app(scope, receive, send):
    work()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})

def wsgi_app(environ, start_response):
    work()
    start_response("200 OK", [("Content-Type", "text/plain")])
    yield b"ok"

async def recv():
    return {"type": "http.request"}

def start(status, headers, exc_info=None):
    return None

async def drop(message):
    return None

options = dict(
    max_events=1, default_budget=1, report=[count, broken, "log"],
    panel=True, panel_history=20,
)
if sys.argv[1] == "asgi":
    app = queryguard.QueryGuardASGI(asgi_app, **options)

    def stream(n):
        async def run():
            for i in range(n):
                scope = {"type": "http", "method": "GET", "path": f"/x/{i}"}
                await app(scope, recv, drop)
        asyncio.run(run())
else:
    app = queryguard.QueryGuardWSGI(wsgi_app, **options)

    def stream(n):
        for i in range(n):
            body = app({"REQUEST_METHOD": "GET", "PATH_INFO": f"/x/{i}"}, start)
            for _ in body:
                pass
            body.close()

stream(300)  # warm caches: compiled statements, call sites
gc.collect()
tracemalloc.start()
before = tracemalloc.get_traced_memory()[0]
stream(10_000)
gc.collect()
grown = tracemalloc.get_traced_memory()[0] - before
assert queryguard.internal_error() is None
print(reported, len(app.reports), grown)
"""


@pytest.mark.parametrize("kind", ["asgi", "wsgi"])
def test_memory_stays_flat_over_10k_requests(kind: str, tmp_path: Path) -> None:
    reported, kept, grown = _run(tmp_path, MEMORY_RUN, kind)
    assert reported == 10_300
    assert kept == 20
    # A leak of a few dozen bytes per request would show as >= 300 kB.
    assert grown < 150_000, grown


# --- overhead when not measuring -------------------------------------------


def test_an_unsampled_request_creates_no_per_request_state(
    watched: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_tracker(*args: object, **kwargs: object) -> None:
        raise AssertionError("an unsampled request made a tracker")

    monkeypatch.setattr(_core, "Tracker", no_tracker)
    monkeypatch.setattr("pytest_queryguard.app._asgi.Tracker", no_tracker)
    monkeypatch.setattr("pytest_queryguard.app._wsgi.Tracker", no_tracker)

    def wsgi_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        _selects(watched, 2)
        start_response("200 OK", [])
        return [b"ok"]

    body = QueryGuardWSGI(wsgi_app, sample_rate=0)(
        {"REQUEST_METHOD": "GET", "PATH_INFO": "/"}, lambda *a: None
    )
    assert body == [b"ok"]  # the application's own iterable, not wrapped

    async def asgi_app(scope: Any, receive: Any, send: Any) -> None:
        _selects(watched, 2)

    async def never(*args: Any) -> Any:
        raise AssertionError("not called")

    scope = {"type": "http", "method": "GET", "path": "/"}
    asyncio.run(QueryGuardASGI(asgi_app, sample_rate=0)(scope, never, never))
