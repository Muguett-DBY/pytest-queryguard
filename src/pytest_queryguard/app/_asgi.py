"""Pure ASGI middleware: one measured unit per HTTP request.

No Starlette import: it works with any ASGI 3 application. Starlette and
FastAPI put the matched route in ``scope["route"]``; its ``path`` is the
template that budgets match against.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import quote

from . import _core, _page, _panel
from ._core import RequestReport, Settings, Tracker

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class QueryGuardASGI:
    """Run the N+1 rules and budgets on every HTTP request of an ASGI app.

    Takes a ``Settings`` or its fields as keyword arguments, so
    ``app.add_middleware(QueryGuardASGI, budgets={...})`` works in Starlette
    and FastAPI. With ``panel=True`` it also serves the development panel at
    ``panel_path``; by default only to loopback clients. With
    ``panel_loopback_only=False`` it needs ``panel_token``, which
    ``requests.json`` then requires as a Bearer token. Never enable the panel
    in production.
    """

    def __init__(
        self,
        app: ASGIApp,
        settings: Settings | None = None,
        *,
        panel: bool = False,
        panel_path: str = "/_queryguard",
        panel_history: int = 50,
        panel_loopback_only: bool = True,
        panel_token: str | None = None,
        **options: Any,
    ) -> None:
        self.app = app
        self.settings = _core._settings(settings, options)
        self.panel = bool(panel)
        self.panel_path = _panel.normalize_path(panel_path)
        self.panel_loopback_only = panel_loopback_only
        self._panel_token = _panel.check_token(
            panel_token, self.panel, panel_loopback_only
        )
        self.history = _panel.History(panel_history) if self.panel else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if self.history is not None:
            path = scope.get("path", "")
            root = (scope.get("root_path", "") or "").rstrip("/")
            if root and (path == root or path.startswith(root + "/")):
                path = path[len(root) :]
            target = _panel.route(self.panel_path, path)
            if target is not None:
                await self._serve_panel(scope, send, target)
                return
        tracker = _start(self.settings, scope)
        if tracker is None:
            await self.app(scope, receive, send)
            return
        report = tracker.report
        mode = self.settings.page_report
        page = _Page(mode, tracker, scope, send) if mode != "off" else None

        async def send_status(message: Message) -> None:
            if message.get("type") == "http.response.start":
                report.status = message.get("status")
                if page is not None and page.hold(message):
                    return
            elif (
                page is not None and page.held is not None and await page.take(message)
            ):
                return
            await send(message)

        try:
            await self.app(scope, receive, send_status)
            if page is not None:
                # An application that ends without the last part of its body.
                await page.flush()
        except BaseException:
            if report.status is None or (page is not None and page.held is not None):
                report.status = 500  # what the server answers for it
            self._finish(tracker, scope)
            raise
        self._finish(tracker, scope)
        if tracker.warning is not None:
            # "warn" under an "error" filter, and the app itself succeeded.
            raise tracker.warning

    def _finish(self, tracker: Tracker, scope: Scope) -> None:
        route = _route(scope)
        report = tracker.finish(route if tracker.report.route is None else None)
        if self.history is not None:
            self.history.add(report)

    async def _serve_panel(self, scope: Scope, send: Send, target: str) -> None:
        client = scope.get("client")
        host = client[0] if client else None
        if scope.get("method") not in ("GET", "HEAD"):
            status, body, kind = 405, b"method not allowed", "text/plain"
        elif not _panel.allowed(
            host, self.panel_loopback_only, _header(scope, b"host")
        ):
            status, body, kind = (
                403,
                b"the panel answers loopback clients only",
                "text/plain",
            )
        elif target == "json" and not _panel.authorized(
            self._panel_token, _header(scope, b"authorization")
        ):
            status, body, kind = 401, _panel.UNAUTHORIZED, "text/plain"
        elif target == "html":
            root = (scope.get("root_path", "") or "").rstrip("/")
            json_url = quote(f"{root}{self.panel_path}/requests.json", safe="/")
            status, body, kind = 200, _panel.html_body(json_url), "text/html"
        elif target == "json":
            assert self.history is not None
            status, body, kind = 200, _panel.json_body(self.history), "application/json"
        else:
            status, body, kind = 404, b"not found", "text/plain"
        headers = [
            (b"content-type", f"{kind}; charset=utf-8".encode()),
            (b"content-length", str(len(body)).encode()),
        ]
        headers += [(k.encode(), v.encode()) for k, v in _panel.HEADERS]
        if status == 401:
            name, value = _panel.CHALLENGE
            headers.append((name.encode(), value.encode()))
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"" if scope.get("method") == "HEAD" else body,
            }
        )

    @property
    def reports(self) -> tuple[RequestReport, ...]:
        """The reports the panel holds, oldest first; empty without the panel."""
        return self.history.snapshot() if self.history is not None else ()


class _Page:
    """Holds back an HTML response to add the request's findings to it.

    Only with ``page_report`` on and for a response ``_page.eligible()``
    accepts: its start message waits until the body is complete, at most its
    declared ``Content-Length``. Any other response passes through as sent.
    """

    __slots__ = (
        "chunks",
        "held",
        "limit",
        "mode",
        "scope",
        "send",
        "size",
        "tracker",
    )

    def __init__(self, mode: str, tracker: Tracker, scope: Scope, send: Send) -> None:
        self.mode = mode
        self.tracker = tracker
        self.scope = scope
        self.send = send
        self.held: Message | None = None
        self.chunks: list[bytes] = []
        self.size = 0
        self.limit = 0

    def hold(self, message: Message) -> bool:
        """Keep the start message back when the response can get a report."""
        if message.get("trailers"):
            return False
        try:
            limit = _page.eligible(
                message.get("status"), _headers(message), self.scope.get("method")
            )
        except Exception as exc:  # noqa: BLE001  # never break the application
            _core.fail(exc, "the page report")
            return False
        if limit is None:
            return False
        self.held, self.limit = message, limit
        return True

    async def take(self, message: Message) -> bool:
        """Buffer a body part; True when it was taken (not to be sent as is)."""
        if message.get("type") != "http.response.body":
            await self.flush()  # an extension's message: send what we hold first
            return False
        self.chunks.append(bytes(message.get("body", b"")))
        self.size += len(self.chunks[-1])
        if message.get("more_body", False):
            if self.size <= self.limit:
                return True
            await self.flush()  # longer than its Content-Length: as sent
            return True
        held, self.held = self.held, None
        assert held is not None
        body = b"".join(self.chunks)
        self.chunks = []
        headers = held.get("headers") or []
        try:
            report = self.tracker.preview(_route(self.scope))
            text = _headers(held)
            new_body = (
                _page.inject(body, report, self.mode, text)
                if report is not None
                else None
            )
            if new_body is not None:
                headers = [
                    (k.encode("latin-1"), v.encode("latin-1"))
                    for k, v in _page.with_length(text, len(new_body))
                ]
                body = new_body
        except Exception as exc:  # noqa: BLE001  # never break the application
            _core.fail(exc, "the page report")
            headers = held.get("headers") or []
        await self.send({**held, "headers": headers})
        await self.send({**message, "body": body})
        return True

    async def flush(self) -> None:
        """Send the held start message and body parts unchanged."""
        held, self.held = self.held, None
        if held is None:
            return
        chunks, self.chunks = self.chunks, []
        await self.send(held)
        if chunks:
            body = b"".join(chunks)
            await self.send(
                {"type": "http.response.body", "body": body, "more_body": True}
            )


def _headers(message: Message) -> list[tuple[str, str]]:
    return [
        (bytes(k).decode("latin-1"), bytes(v).decode("latin-1"))
        for k, v in message.get("headers") or ()
    ]


def _route(scope: Scope) -> str | None:
    """The route template Starlette and FastAPI put in the scope, or None."""
    try:
        route = getattr(scope.get("route"), "path", None)
    except Exception:  # noqa: BLE001  # never break the application
        return None
    return route if isinstance(route, str) else None


def _start(settings: Settings, scope: Scope) -> Tracker | None:
    """A started tracker for the request; None when the mode is off or not sampled."""
    if _core._error is not None or not _core.sampled(settings):
        return None
    try:
        method = scope.get("method")
        path = scope.get("path")
        tracker = Tracker(settings, f"{method} {path}", method=method, path=path)
        tracker.start()
        return tracker
    except Exception as exc:  # noqa: BLE001  # never break the application
        _core.fail(exc, "the start of a request")
        return None


def _header(scope: Scope, name: bytes) -> str | None:
    """The first value of a request header, or None when it is absent."""
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return bytes(value).decode("latin-1")
    return None
