"""Pure ASGI middleware: one measured unit per HTTP request.

No Starlette import: it works with any ASGI 3 application. Starlette and
FastAPI put the matched route in ``scope["route"]``; its ``path`` is the
template that budgets match against.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import quote

from . import _core, _panel
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

        async def send_status(message: Message) -> None:
            if message.get("type") == "http.response.start":
                report.status = message.get("status")
            await send(message)

        try:
            await self.app(scope, receive, send_status)
        except BaseException:
            if report.status is None:
                report.status = 500  # what the server answers for it
            self._finish(tracker, scope)
            raise
        self._finish(tracker, scope)
        if tracker.warning is not None:
            # "warn" under an "error" filter, and the app itself succeeded.
            raise tracker.warning

    def _finish(self, tracker: Tracker, scope: Scope) -> None:
        route = None
        try:
            route = getattr(scope.get("route"), "path", None)
            if not isinstance(route, str):
                route = None
        except Exception:  # noqa: BLE001  # never break the application
            route = None
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
