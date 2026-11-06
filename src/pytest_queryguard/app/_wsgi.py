"""WSGI middleware: one measured unit per request (Flask, Django, any WSGI app).

The request ends when the server closes the response iterable, so statements
run while a streamed body is produced count too. WSGI has no route
templates; budgets match the path unless the application calls
``set_route()``.
"""

from __future__ import annotations

import sys
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from types import TracebackType
from typing import Any
from urllib.parse import quote

from . import _core, _page, _panel
from ._core import RequestReport, Settings, Tracker

Environ = dict[str, Any]
ExcInfo = tuple[type[BaseException], BaseException, TracebackType]
StartResponse = Callable[..., Any]
WSGIApp = Callable[[Environ, StartResponse], Iterable[bytes]]


class QueryGuardWSGI:
    """Run the N+1 rules and budgets on every request of a WSGI app.

    Wrap the application callable, for Flask ``app.wsgi_app``. Takes a
    ``Settings`` or its fields as keyword arguments. With ``panel=True`` it
    also serves the development panel at ``panel_path``; by default only to
    loopback clients. With ``panel_loopback_only=False`` it needs
    ``panel_token``, which ``requests.json`` then requires as a Bearer token.
    Never enable the panel in production.
    """

    def __init__(
        self,
        app: WSGIApp,
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

    def __call__(
        self, environ: Environ, start_response: StartResponse
    ) -> Iterable[bytes]:
        if self.history is not None:
            target = _panel.route(self.panel_path, environ.get("PATH_INFO", ""))
            if target is not None:
                return self._serve_panel(environ, start_response, target)
        tracker = _start(self.settings, environ)
        if tracker is None:
            return self.app(environ, start_response)
        report = tracker.report
        page = (
            _Page(self.settings.page_report, start_response, environ)
            if self.settings.page_report != "off"
            else None
        )

        def start(status: str, headers: Any, exc_info: ExcInfo | None = None) -> Any:
            try:
                report.status = int(status.split(None, 1)[0])
            except (ValueError, IndexError, AttributeError):
                pass
            if page is not None:
                return page.start(status, headers, exc_info, report.status)
            if exc_info is None:
                return start_response(status, headers)
            return start_response(status, headers, exc_info)

        wrapped_files: list[tuple[Iterable[bytes], _File]] = []
        native_wrapper = environ.get("wsgi.file_wrapper")
        file_wrapper: Any = None
        if callable(native_wrapper):

            def file_wrapper(file: Any, *args: Any, **kwargs: Any) -> Iterable[bytes]:
                measured = _File(file, tracker, self)
                result: Iterable[bytes] = native_wrapper(measured, *args, **kwargs)
                wrapped_files.append((result, measured))
                return result

            # The same environ, so outer middleware still sees what the
            # application writes into it, such as REMOTE_USER.
            environ["wsgi.file_wrapper"] = file_wrapper

        try:
            result = self.app(environ, start)
        except BaseException:
            if report.status is None or (page is not None and page.held is not None):
                report.status = 500  # what the server answers for it
            tracker.detach()
            self._finish(tracker)
            raise
        finally:
            # Give the server its own factory back for its native-response check.
            if (
                file_wrapper is not None
                and environ.get("wsgi.file_wrapper") is file_wrapper
            ):
                environ["wsgi.file_wrapper"] = native_wrapper
        tracker.detach()
        for native_result, measured in wrapped_files:
            if result is native_result:
                if page is not None:
                    try:
                        page.flush()  # a file is sent as it is
                    except BaseException:
                        self._finish(tracker)
                        raise
                measured.returned()
                return result
        return _Body(result, tracker, self, page)

    def _finish(self, tracker: Tracker) -> None:
        if tracker.finished:
            return
        report = tracker.finish()
        if self.history is not None:
            self.history.add(report)

    def _serve_panel(
        self, environ: Environ, start_response: StartResponse, target: str
    ) -> Iterable[bytes]:
        method = environ.get("REQUEST_METHOD")
        if method not in ("GET", "HEAD"):
            status, body, kind = (
                "405 Method Not Allowed",
                b"method not allowed",
                "text/plain",
            )
        elif not _panel.allowed(
            environ.get("REMOTE_ADDR"),
            self.panel_loopback_only,
            environ.get("HTTP_HOST"),
        ):
            status, body, kind = (
                "403 Forbidden",
                b"the panel answers loopback clients only",
                "text/plain",
            )
        elif target == "json" and not _panel.authorized(
            self._panel_token, environ.get("HTTP_AUTHORIZATION")
        ):
            status, body, kind = (
                "401 Unauthorized",
                _panel.UNAUTHORIZED,
                "text/plain",
            )
        elif target == "html":
            # PEP 3333: SCRIPT_NAME holds the raw bytes, decoded as latin-1.
            root = (environ.get("SCRIPT_NAME", "") or "").rstrip("/")
            json_url = quote(
                f"{root}{self.panel_path}/requests.json", safe="/", encoding="latin-1"
            )
            status, body, kind = "200 OK", _panel.html_body(json_url), "text/html"
        elif target == "json":
            assert self.history is not None
            status, body, kind = (
                "200 OK",
                _panel.json_body(self.history),
                "application/json",
            )
        else:
            status, body, kind = "404 Not Found", b"not found", "text/plain"
        headers = [
            ("Content-Type", f"{kind}; charset=utf-8"),
            ("Content-Length", str(len(body))),
            *_panel.HEADERS,
        ]
        if status.startswith("401"):
            headers.append(_panel.CHALLENGE)
        start_response(status, headers)
        return [b"" if method == "HEAD" else body]

    @property
    def reports(self) -> tuple[RequestReport, ...]:
        """The reports the panel holds, oldest first; empty without the panel."""
        return self.history.snapshot() if self.history is not None else ()


class _File:
    """Measure file operations without replacing the server's native iterable.

    PEP 3333 requires a native wrapper to call the supplied file's close().
    Only the file whose native iterable is actually returned ends the request;
    applications may construct and discard other wrappers along the way.
    """

    def __init__(self, file: Any, tracker: Tracker, owner: QueryGuardWSGI) -> None:
        self._file = file
        self._tracker = tracker
        self._owner = owner
        self._selected = False
        self._closed = False
        self._failed = False

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._file, name)
        if not callable(value):
            return value

        def call(*args: Any, **kwargs: Any) -> Any:
            token = _core.current.set(self._tracker.active)
            try:
                return value(*args, **kwargs)
            except BaseException:
                self._failed = True
                raise
            finally:
                _core.current.reset(token)

        return call

    def returned(self) -> None:
        self._selected = True
        if self._closed:
            self._finish()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Native transmission can fail without calling any Python file method.
        # Do not replace the server's active error with a budget warning.
        if sys.exc_info()[0] is not None:
            self._failed = True
        try:
            # Access the original close through __getattr__ for its context.
            close = self.__getattr__("close") if hasattr(self._file, "close") else None
            if close is not None:
                close()
        finally:
            if self._selected:
                self._finish()

    def _finish(self) -> None:
        self._owner._finish(self._tracker)
        warning, self._tracker.warning = self._tracker.warning, None
        if warning is not None and not self._failed:
            _log_raised(warning)


class _Body:
    """The response iterable: re-enters the request's context for each chunk."""

    def __init__(
        self,
        result: Iterable[bytes],
        tracker: Tracker,
        owner: QueryGuardWSGI,
        page: _Page | None = None,
    ) -> None:
        self._result = result
        self._iter: Iterator[bytes] | None = None
        self._tracker = tracker
        self._owner = owner
        self._page = page
        self._ready: deque[bytes] = deque()
        self._exhausted = False
        self._closed = False
        self._failed = False

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        if self._ready:
            return self._ready.popleft()
        if self._closed:
            raise StopIteration
        chunk = self._pull()
        page = self._page
        if page is not None and page.held is not None:
            self._buffer(page, chunk)
            if self._ready:
                return self._ready.popleft()
            chunk = None
        if chunk is not None:
            return chunk
        # Include response cleanup before finalizing the report; outside the
        # except block, where close() would take it for an error.
        self.close()
        raise StopIteration

    def _pull(self) -> bytes | None:
        """The application's next chunk, in the request's context; None at the end."""
        if self._exhausted:
            return None
        token = _core.current.set(self._tracker.active)
        try:
            if self._iter is None:
                self._iter = iter(self._result)
            return next(self._iter)
        except StopIteration:
            self._exhausted = True
            return None
        except BaseException:
            self._failed = True
            if self._page is not None and self._page.held is not None:
                self._tracker.report.status = 500  # no headers sent: the server's
            raise
        finally:
            _core.current.reset(token)

    def _buffer(self, page: _Page, chunk: bytes | None) -> None:
        """Read the whole body of a held response, then start it, with the report.

        The parts are kept on ``page``: a ``start_response`` call that
        replaces the held response (an error page) drops them, and a
        ``write()`` sends them before its own data.
        """
        while chunk is not None:
            page.add(chunk)
            if page.held is None:
                break  # started by write() or by a response not held
            if page.size > page.limit:
                page.flush()  # longer than its Content-Length: as sent
                break
            chunk = self._pull()
        if page.held is None:
            self._ready.extend(page.take())
            return
        body = page.finish(self._tracker)
        if body:
            self._ready.append(body)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # A server closes the body while handling its own error, such as a
        # client disconnect; do not replace that error with a budget warning.
        if sys.exc_info()[0] is not None:
            self._failed = True
        try:
            close = getattr(self._result, "close", None)
            if close is not None:
                token = _core.current.set(self._tracker.active)
                try:
                    close()
                finally:
                    _core.current.reset(token)
        finally:
            self._owner._finish(self._tracker)
        warning, self._tracker.warning = self._tracker.warning, None
        if warning is not None and not self._failed:
            # "warn" under an "error" filter, and the app itself succeeded.
            _log_raised(warning)


class _Page:
    """Holds back ``start_response`` of an HTML response to add the report.

    Only for a response ``_page.eligible()`` accepts: ``start_response`` is
    called once the body has been read, at most its declared
    ``Content-Length``. Any other response starts as the application asks.
    """

    __slots__ = (
        "_parts",
        "_write",
        "environ",
        "held",
        "limit",
        "mode",
        "size",
        "start_response",
    )

    def __init__(
        self, mode: str, start_response: StartResponse, environ: Environ
    ) -> None:
        self.mode = mode
        self.start_response = start_response
        self.environ = environ
        self.held: tuple[str, list[tuple[str, str]], ExcInfo | None] | None = None
        self.limit = 0
        self._parts: list[bytes] = []
        self.size = 0
        self._write: Callable[[bytes], Any] | None = None

    def add(self, chunk: bytes) -> None:
        self._parts.append(chunk)
        self.size += len(chunk)

    def take(self) -> list[bytes]:
        """The buffered parts, which the caller now sends."""
        parts, self._parts, self.size = self._parts, [], 0
        return parts

    def start(
        self, status: str, headers: Any, exc_info: ExcInfo | None, code: int | None
    ) -> Any:
        if self._write is None:  # not started yet: an error page may replace it
            if self.held is not None:
                self.take()  # the replaced response's body is never sent
            self.held = None
            try:
                limit = _page.eligible(
                    code, headers, self.environ.get("REQUEST_METHOD")
                )
            except Exception as exc:  # noqa: BLE001  # never break the application
                _core.fail(exc, "the page report")
                limit = None
            if limit is not None:
                self.held, self.limit = (status, list(headers), exc_info), limit
                return self.write
        self._write = self._start(status, headers, exc_info)
        return self._write

    def _start(self, status: str, headers: Any, exc_info: ExcInfo | None) -> Any:
        if exc_info is None:
            return self.start_response(status, headers)
        return self.start_response(status, headers, exc_info)

    def write(self, data: bytes) -> None:
        """The legacy ``write()``: the body is not ours to hold; start as sent."""
        self.flush()
        if self._write is not None:
            for part in self.take():  # what the iterable yielded before
                self._write(part)
            self._write(data)

    def flush(self) -> None:
        held, self.held = self.held, None
        if held is not None:
            self._write = self._start(*held)

    def finish(self, tracker: Tracker) -> bytes:
        """Start the held response, with the report in its body when it has problems."""
        held, self.held = self.held, None
        assert held is not None
        status, headers, exc_info = held
        body = b"".join(self.take())
        try:
            report = tracker.preview()
            new_body = None
            if report is not None:
                new_body = _page.inject(body, report, self.mode, headers)
            if new_body is not None:
                headers, body = _page.with_length(headers, len(new_body)), new_body
        except Exception as exc:  # noqa: BLE001  # never break the application
            _core.fail(exc, "the page report")
        self._write = self._start(status, headers, exc_info)
        return body


def _log_raised(warning: Warning) -> None:
    """Log a budget warning that a filter turned into an error.

    A WSGI response has started by the time its report is ready. Raised from
    the body or its close(), the error cannot fail it; it only stops the
    server early, and a chunked response loses its final chunk. So it is
    logged at ERROR, as loud as the server's own error log would be.
    """
    _core.logger.error("%s: %s", type(warning).__name__, warning)


def _start(settings: Settings, environ: Environ) -> Tracker | None:
    if _core._error is not None or not _core.sampled(settings):
        return None
    try:
        method = environ.get("REQUEST_METHOD")
        path = environ.get("PATH_INFO") or "/"
        tracker = Tracker(settings, f"{method} {path}", method=method, path=path)
        tracker.start()
        return tracker
    except Exception as exc:  # noqa: BLE001  # never break the application
        _core.fail(exc, "the start of a request")
        return None
