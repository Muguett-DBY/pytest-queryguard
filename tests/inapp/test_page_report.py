"""page_report: findings added to the request's own HTML response (ASGI and WSGI)."""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from flask import Flask, Response, stream_with_context
from sqlalchemy import literal_column, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pytest_queryguard.app import QueryGuardASGI, QueryGuardWSGI, Settings, _page

from .conftest import Order

PAGE = "<!doctype html><html><body><h1>Orders</h1>{}</body></html>"
EVIL = "</script><img src=x onerror=alert(document.domain)>"


def _names(engine: Engine) -> str:
    """Runs an N+1: one lazy load of Order.customer per order."""
    with Session(engine) as s:
        return ",".join(o.customer.name for o in s.scalars(select(Order)))


def _evil_repeats(engine: Engine) -> None:
    """A repeated statement whose SQL text holds markup."""
    with Session(engine) as s:
        for _ in range(5):
            s.execute(select(literal_column("1").label(EVIL))).all()


def _footer(text: str) -> str:
    match = re.search(
        r'<aside id="queryguard-page-report"[^>]*>.*?</aside>', text, re.DOTALL
    )
    assert match, text
    return match.group(0)


def _console(text: str) -> tuple[str, dict[str, Any]]:
    match = re.search(r"<script>console\.warn\((.*)\);</script>", text, re.DOTALL)
    assert match, text
    message, data = json.loads(f"[{match.group(1)}]")
    return message, data


# --- ASGI (FastAPI) ---------------------------------------------------------


def _fastapi(engine: Engine, **options: Any) -> FastAPI:
    app = FastAPI()

    @app.get("/html", response_class=HTMLResponse)
    def html_page() -> str:
        return PAGE.format(_names(engine))

    @app.get("/clean", response_class=HTMLResponse)
    def clean() -> str:
        return PAGE.format("nothing")

    @app.get("/fragment", response_class=HTMLResponse)
    def fragment() -> str:
        return f"<p>{_names(engine)}</p>"

    @app.get("/json")
    def as_json() -> JSONResponse:
        return JSONResponse({"names": _names(engine)})

    @app.get("/gzip")
    def zipped() -> HTMLResponse:
        body = gzip.compress(PAGE.format(_names(engine)).encode())
        return HTMLResponse(body, headers={"content-encoding": "gzip"})

    @app.get("/csp", response_class=HTMLResponse)
    def csp() -> HTMLResponse:
        return HTMLResponse(
            PAGE.format(_names(engine)),
            headers={"content-security-policy": "script-src 'self'", "etag": '"1"'},
        )

    @app.get("/evil/{name}", response_class=HTMLResponse)
    def evil(name: str) -> str:
        _evil_repeats(engine)
        return PAGE.format("evil")

    @app.get("/stream")
    def stream() -> StreamingResponse:
        def body() -> Iterator[str]:
            yield "<html><body>"
            yield _names(engine)
            yield "</body></html>"

        return StreamingResponse(body(), media_type="text/html")

    app.add_middleware(QueryGuardASGI, report=[], **options)
    return app


@pytest.fixture
def asgi_footer(watched: Engine) -> Iterator[TestClient]:
    with TestClient(_fastapi(watched, page_report="footer")) as client:
        yield client


def test_asgi_footer_before_body_end_with_content_length(
    asgi_footer: TestClient,
) -> None:
    response = asgi_footer.get("/html")
    text = response.text
    assert response.headers["content-length"] == str(len(response.content))
    footer = _footer(text)
    assert text.endswith(footer + "\n</body></html>")
    assert "queryguard: 1 problem in GET /html" in footer
    assert "Order.customer" in footer
    assert 'style="' in footer  # no CSP: a styled footer


def test_asgi_clean_html_untouched(asgi_footer: TestClient) -> None:
    response = asgi_footer.get("/clean")
    assert response.text == PAGE.format("nothing")
    assert response.headers["content-length"] == str(len(response.content))


def test_asgi_non_html_untouched(asgi_footer: TestClient) -> None:
    response = asgi_footer.get("/json")
    assert "queryguard" not in response.text
    assert response.json()["names"].startswith("customer 1")


def test_asgi_gzip_untouched(asgi_footer: TestClient) -> None:
    # The test client decodes gzip; the decoded page must be the app's own.
    response = asgi_footer.get("/gzip")
    assert response.headers["content-encoding"] == "gzip"
    assert "queryguard" not in response.text
    assert response.text.endswith("</body></html>")


def test_asgi_without_body_end_appends(asgi_footer: TestClient) -> None:
    response = asgi_footer.get("/fragment")
    assert response.text.startswith("<p>customer 1")
    assert response.text.rstrip().endswith("</aside>")
    assert response.headers["content-length"] == str(len(response.content))


def test_asgi_streaming_response_untouched(asgi_footer: TestClient) -> None:
    response = asgi_footer.get("/stream")
    assert "queryguard" not in response.text
    assert response.text.endswith("</body></html>")


def test_asgi_escapes_sql_and_path(asgi_footer: TestClient) -> None:
    response = asgi_footer.get("/evil/<b>bold")
    footer = _footer(response.text)
    assert "<img" not in footer
    assert "<b>" not in footer
    assert "&lt;img src=x onerror=alert(document.domain)&gt;" in footer
    assert "&lt;b&gt;bold" in footer
    assert response.headers["content-length"] == str(len(response.content))


def test_asgi_console_script(watched: Engine) -> None:
    with TestClient(_fastapi(watched, page_report="console")) as client:
        response = client.get("/evil/x")
    script = response.text[response.text.index("<script>") :]
    body = script[len("<script>") : script.index("</script>")]
    assert "<" not in body  # nothing can end or confuse the script element
    message, data = _console(response.text)
    assert EVIL in message
    assert data["findings"][0]["kind"] == "repeated-statement"
    assert EVIL in data["findings"][0]["label"]
    assert data["request"] == "GET /evil/x"
    assert response.headers["content-length"] == str(len(response.content))


def test_asgi_csp_skips_console_for_unstyled_footer(watched: Engine) -> None:
    with TestClient(_fastapi(watched, page_report="console")) as client:
        response = client.get("/csp")
    assert "<script>" not in response.text
    footer = _footer(response.text)
    assert "style=" not in footer
    assert response.headers["content-security-policy"] == "script-src 'self'"
    assert "etag" not in response.headers  # it described the original bytes
    assert response.headers["content-length"] == str(len(response.content))


def test_asgi_off_by_default_and_not_sampled(watched: Engine) -> None:
    with TestClient(_fastapi(watched)) as client:
        assert "queryguard" not in client.get("/html").text
    app = _fastapi(watched, page_report="footer", sample_rate=0.0)
    with TestClient(app) as client:
        assert "queryguard" not in client.get("/html").text


def test_asgi_over_budget_without_findings(watched: Engine) -> None:
    app = _fastapi(watched, page_report="footer", default_budget=1, repeat_threshold=10)
    with TestClient(app) as client:
        footer = _footer(client.get("/evil/x").text)
    assert "budget 1" in footer
    assert "repeated-statement" not in footer


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _scope(method: str = "GET") -> dict[str, Any]:
    return {"type": "http", "method": method, "path": "/raw", "headers": []}


def _raw_asgi(
    engine: Engine, parts: list[bytes], headers: list[tuple[bytes, bytes]]
) -> Any:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        _names(engine)
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        for n, part in enumerate(parts):
            more = n < len(parts) - 1
            await send({"type": "http.response.body", "body": part, "more_body": more})

    return app


def _run_asgi(app: Any, method: str = "GET") -> list[dict[str, Any]]:
    import asyncio

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    middleware = QueryGuardASGI(app, report=[], page_report="footer")
    asyncio.run(middleware(_scope(method), _receive, send))
    return sent


def _html_headers(length: int) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(length).encode()),
    ]


def test_asgi_body_in_parts_with_content_length(watched: Engine) -> None:
    parts = [b"<html><BODY>", b"page</Body >", b"</html>"]
    total = sum(map(len, parts))
    sent = _run_asgi(_raw_asgi(watched, parts, _html_headers(total)))
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    body = sent[1]["body"]
    assert body.startswith(b"<html><BODY>page\n<aside")
    assert body.endswith(b"</aside>\n</Body ></html>")
    assert dict(sent[0]["headers"])[b"content-length"] == str(len(body)).encode()


def test_asgi_head_and_longer_body_pass_through(watched: Engine) -> None:
    parts = [b"<html><body>", b"x" * 10, b"</body></html>"]
    # Declares fewer bytes than it sends: sent as it is, in its parts.
    sent = _run_asgi(_raw_asgi(watched, parts, _html_headers(5)))
    assert b"".join(m.get("body", b"") for m in sent) == b"".join(parts)
    assert dict(sent[0]["headers"])[b"content-length"] == b"5"
    sent = _run_asgi(_raw_asgi(watched, [b""], _html_headers(30)), method="HEAD")
    assert sent[1]["body"] == b""


def test_asgi_utf16_untouched(watched: Engine) -> None:
    body = "<html><body>x</body></html>".encode("utf-16")
    headers = [
        (b"content-type", b"text/html; charset=utf-16"),
        (b"content-length", str(len(body)).encode()),
    ]
    sent = _run_asgi(_raw_asgi(watched, [body], headers))
    assert sent[1]["body"] == body


def test_page_report_setting_is_checked() -> None:
    with pytest.raises(ValueError, match="page_report must be one of"):
        Settings(page_report="banner")
    assert Settings().page_report == "off"


def test_eligible_rules() -> None:
    html = [("Content-Type", "text/html"), ("Content-Length", "10")]
    assert _page.eligible(200, html, "GET") == 10
    assert _page.eligible(404, html, "GET") is None
    assert _page.eligible(206, html, "GET") is None
    assert _page.eligible(200, html, "HEAD") is None
    assert _page.eligible(200, [*html, ("Content-Encoding", "br")], "GET") is None
    assert _page.eligible(200, [*html, ("Content-Encoding", "identity")], "GET") == 10
    assert _page.eligible(200, html[:1], "GET") is None
    assert (
        _page.eligible(200, [("Content-Type", "application/xhtml+xml")], "GET") is None
    )
    big = [("content-type", "TEXT/HTML; Charset=UTF-8"), ("content-length", "9" * 9)]
    assert _page.eligible(200, big, "GET") is None
    assert _page.eligible(200, [*html, ("content-length", "11")], "GET") is None


# --- WSGI (Flask) -----------------------------------------------------------


def _flask(engine: Engine, **options: Any) -> Flask:
    app = Flask(__name__)
    app.wsgi_app = QueryGuardWSGI(app.wsgi_app, report=[], **options)  # type: ignore[method-assign]

    @app.get("/html")
    def html_page() -> str:
        return PAGE.format(_names(engine))

    @app.get("/clean")
    def clean() -> str:
        return PAGE.format("nothing")

    @app.get("/fragment")
    def fragment() -> str:
        return f"<p>{_names(engine)}</p>"

    @app.get("/json")
    def as_json() -> dict[str, str]:
        return {"names": _names(engine)}

    @app.get("/gzip")
    def zipped() -> Response:
        body = gzip.compress(PAGE.format(_names(engine)).encode())
        return Response(
            body, mimetype="text/html", headers={"Content-Encoding": "gzip"}
        )

    @app.get("/csp")
    def csp() -> Response:
        return Response(
            PAGE.format(_names(engine)),
            mimetype="text/html",
            headers={"Content-Security-Policy": "default-src 'self'"},
        )

    @app.get("/evil/<name>")
    def evil(name: str) -> str:
        _evil_repeats(engine)
        return PAGE.format("evil")

    @app.get("/stream")
    def stream() -> Response:
        def body() -> Iterator[str]:
            yield "<html><body>"
            yield _names(engine)
            yield "</body></html>"

        return Response(stream_with_context(body()), mimetype="text/html")

    return app


def test_wsgi_footer_and_content_length(watched: Engine) -> None:
    client = _flask(watched, page_report="footer").test_client()
    response = client.get("/html")
    text = response.get_data(as_text=True)
    footer = _footer(text)
    assert text.endswith(footer + "\n</body></html>")
    assert "Order.customer" in footer
    assert response.headers["Content-Length"] == str(len(response.get_data()))


def test_wsgi_untouched_responses(watched: Engine) -> None:
    client = _flask(watched, page_report="footer").test_client()
    assert client.get("/clean").get_data(as_text=True) == PAGE.format("nothing")
    assert "queryguard" not in client.get("/json").get_data(as_text=True)
    zipped = client.get("/gzip")
    assert gzip.decompress(zipped.get_data()).endswith(b"</body></html>")
    assert b"queryguard" not in gzip.decompress(zipped.get_data())
    streamed = client.get("/stream").get_data(as_text=True)
    assert "queryguard" not in streamed
    assert streamed.endswith("</body></html>")


def test_wsgi_without_body_end_and_escaping(watched: Engine) -> None:
    client = _flask(watched, page_report="footer").test_client()
    fragment = client.get("/fragment")
    assert fragment.get_data(as_text=True).rstrip().endswith("</aside>")
    assert fragment.headers["Content-Length"] == str(len(fragment.get_data()))
    footer = _footer(client.get("/evil/%3Cb%3Ebold").get_data(as_text=True))
    assert "<img" not in footer
    assert "<b>" not in footer
    assert "&lt;img src=x onerror=alert(document.domain)&gt;" in footer
    assert "&lt;b&gt;bold" in footer


def test_wsgi_console_and_csp(watched: Engine) -> None:
    client = _flask(watched, page_report="console").test_client()
    message, data = _console(client.get("/evil/x").get_data(as_text=True))
    assert EVIL in message
    assert data["findings"][0]["kind"] == "repeated-statement"
    response = client.get("/csp")
    text = response.get_data(as_text=True)
    assert "<script>" not in text
    assert "style=" not in _footer(text)
    assert response.headers["Content-Length"] == str(len(response.get_data()))


class _Closing:
    def __init__(self, parts: list[bytes]) -> None:
        self.parts: Any = parts
        self.closed = False

    def wrap(self, parts: Any) -> _Closing:
        self.parts = parts
        return self

    def __iter__(self) -> Iterator[bytes]:
        return iter(self.parts)

    def close(self) -> None:
        self.closed = True


def _call_wsgi(app: Any) -> tuple[list[Any], bytes, Any]:
    started: list[Any] = []
    out: list[bytes] = []  # write() data and chunks, in the order sent

    def start_response(status: str, headers: list[Any], exc_info: Any = None) -> Any:
        started.append((status, headers))
        return out.append

    middleware = QueryGuardWSGI(app, report=[], page_report="footer")
    result = middleware({"REQUEST_METHOD": "GET", "PATH_INFO": "/raw"}, start_response)
    for chunk in result:
        assert started, "start_response must come before the first chunk"
        out.append(chunk)
    result.close()  # type: ignore[attr-defined]
    return started, b"".join(out), result


def test_wsgi_lazy_start_response_and_close(watched: Engine) -> None:
    page = b"<html><body>x</body></html>"

    def app(environ: Any, start_response: Any) -> Any:
        def body() -> Iterator[bytes]:
            # start_response from the first next(), as PEP 3333 allows.
            _names(watched)
            start_response(
                "200 OK",
                [("Content-Type", "text/html"), ("Content-Length", str(len(page)))],
            )
            yield page[:12]
            yield page[12:]

        return inner.wrap(body())

    inner = _Closing([])
    started, body, _ = _call_wsgi(app)
    assert len(started) == 1
    assert dict(started[0][1])["Content-Length"] == str(len(body))
    assert body.startswith(b"<html><body>x\n<aside")
    assert body.endswith(b"</aside>\n</body></html>")
    assert inner.closed


def test_wsgi_write_callable_is_passed_through(watched: Engine) -> None:
    def app(environ: Any, start_response: Any) -> Any:
        _names(watched)
        write = start_response(
            "200 OK", [("Content-Type", "text/html"), ("Content-Length", "27")]
        )
        write(b"<html><body>")
        return [b"x</body></html>"]

    started, body, _ = _call_wsgi(app)
    assert body == b"<html><body>x</body></html>"
    assert dict(started[0][1])["Content-Length"] == "27"


def test_wsgi_error_page_replaces_held_start(watched: Engine) -> None:
    def app(environ: Any, start_response: Any) -> Any:
        _names(watched)
        start_response(
            "200 OK", [("Content-Type", "text/html"), ("Content-Length", "9")]
        )
        try:
            raise RuntimeError("late")
        except RuntimeError:
            import sys

            start_response(
                "500 Internal Server Error",
                [("Content-Type", "text/plain"), ("Content-Length", "4")],
                sys.exc_info(),
            )
        return [b"oops"]

    started, body, _ = _call_wsgi(app)
    assert [s for s, _ in started] == ["500 Internal Server Error"]
    assert body == b"oops"


def test_wsgi_error_page_during_iteration_drops_held_body(watched: Engine) -> None:
    import sys

    def app(environ: Any, start_response: Any) -> Any:
        _names(watched)
        start_response(
            "200 OK", [("Content-Type", "text/html"), ("Content-Length", "99")]
        )
        yield b"<html><body>half"
        try:
            raise RuntimeError("late")
        except RuntimeError:
            start_response(
                "500 Internal Server Error",
                [("Content-Type", "text/plain"), ("Content-Length", "4")],
                sys.exc_info(),
            )
        yield b"oops"

    started, body, _ = _call_wsgi(app)
    assert [s for s, _ in started] == ["500 Internal Server Error"]
    assert body == b"oops"


def test_wsgi_write_during_iteration_keeps_order(watched: Engine) -> None:
    def app(environ: Any, start_response: Any) -> Any:
        _names(watched)
        write = start_response(
            "200 OK", [("Content-Type", "text/html"), ("Content-Length", "99")]
        )
        yield b"<html><body>"
        write(b"payload")
        yield b"</body></html>"

    _, body, _ = _call_wsgi(app)
    assert body == b"<html><body>payload</body></html>"


def test_wsgi_body_error_while_held_reports_500(watched: Engine) -> None:
    reports: list[Any] = []

    def app(environ: Any, start_response: Any) -> Any:
        _names(watched)
        start_response(
            "200 OK", [("Content-Type", "text/html"), ("Content-Length", "99")]
        )
        yield b"<html>"
        raise RuntimeError("body bug")

    middleware = QueryGuardWSGI(app, report=reports.append, page_report="footer")
    started: list[Any] = []
    result = middleware(
        {"REQUEST_METHOD": "GET", "PATH_INFO": "/raw"},
        lambda status, headers, exc_info=None: started.append(status),
    )
    with pytest.raises(RuntimeError, match="body bug"):
        list(result)
    result.close()  # type: ignore[attr-defined]
    assert started == []  # nothing reached the server: it answers 500
    assert reports[0].status == 500


def test_bom_and_digest_headers() -> None:
    from pytest_queryguard.app import RequestReport

    report = RequestReport(id=1, name="GET /", statement_count=3, budget=1)
    assert _page.inject("<body>x</body>".encode("utf-16"), report, "footer", []) is None
    page = _page.inject(b"<body>x</body>", report, "footer", [])
    assert page is not None
    assert b"queryguard: 1 problem in GET /: 3 statements, budget 1" in page
    headers = [
        ("Content-Length", "5"),
        ("ETag", '"x"'),
        ("Repr-Digest", "sha-256=:x:"),
        ("Content-MD5", "x"),
        ("X-Kept", "1"),
    ]
    assert _page.with_length(headers, 9) == [("Content-Length", "9"), ("X-Kept", "1")]


def test_asgi_failed_flush_still_finishes(watched: Engine) -> None:
    import asyncio

    reports: list[Any] = []
    parts = [b"<html><body>", b"never ends"]

    async def app(scope: Any, receive: Any, send: Any) -> None:
        _names(watched)
        await send(
            {"type": "http.response.start", "status": 200, "headers": _html_headers(99)}
        )
        for part in parts:  # returns without the last part (more_body stays True)
            await send({"type": "http.response.body", "body": part, "more_body": True})

    async def send(message: dict[str, Any]) -> None:
        raise OSError("client went away")

    middleware = QueryGuardASGI(app, report=reports.append, page_report="footer")
    with pytest.raises(OSError, match="went away"):
        asyncio.run(middleware(_scope(), _receive, send))
    assert len(reports) == 1  # reported, though the held start never went out
