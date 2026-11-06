"""Settings, per-request state and reporting of the in-application mode.

Nothing here registers a listener or imports pytest. The listeners are in
``_listeners.py``; they only run while an engine is watched.
"""

from __future__ import annotations

import contextvars
import dataclasses
import functools
import inspect
import itertools
import logging
import math
import os
import random
import re
import sys
import threading
import time
import traceback
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from ..api import Query, _to_query
from ..blocks import Sieve
from ..callsite import Sites
from ..collector import QueryCollector
from ..detectors import SCOPE_SESSION, SCOPE_TEST, detect
from ..findings import Finding
from ..messages import display_root, format_budget, format_findings, make_findings
from ..warnings import QueryGuardWarning

logger = logging.getLogger("pytest_queryguard.app")

SCOPE_REQUEST = "request"
REPEAT_SCOPES = (SCOPE_REQUEST, SCOPE_SESSION)

Reporter = Callable[["RequestReport"], object]
ReportTarget = str | Reporter
_TARGETS = ("log", "warn")
PAGE_REPORTS = ("off", "footer", "console")
_METHOD = re.compile(r"[A-Za-z][A-Za-z-]*")  # GET, M-SEARCH, VERSION-CONTROL


def _check_threshold(name: str, value: object, minimum: int = 2) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {value!r}")


def _is_number(value: object) -> bool:
    """An int or a finite float, not a bool."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value)


def _split_budget_key(key: str) -> tuple[str, str]:
    """``"GET /a b"`` -> ``("GET", "/a b")``; ``"/a b"`` -> ``("", "/a b")``.

    The first word is a method only when it looks like one (letters and
    hyphens), so a path with spaces keeps them; any whitespace separates the
    method.
    """
    head, *rest = key.strip().split(None, 1)
    if rest and _METHOD.fullmatch(head):
        return head.upper(), rest[0].strip()
    return "", key.strip()


@dataclass(frozen=True, kw_only=True)
class Settings:
    """What the in-app mode checks for each request and how it reports.

    The thresholds and their defaults are the pytest plugin's (see
    docs/how-detection-works.md). ``repeat_scope`` is ``"request"`` (all
    statements of a request count together) or ``"session"`` (each
    SQLAlchemy Session counts on its own).

    ``budgets`` maps ``"METHOD path"`` or ``"path"`` to the most statements a
    request may run; the path is an ``fnmatch`` pattern matched against the
    route template (``/orders/{order_id}``) and the request path, and the
    first matching key wins. ``default_budget`` applies when no key matches.

    ``report`` is ``"log"`` (a WARNING record on the ``pytest_queryguard.app``
    logger), ``"warn"`` (``QueryGuardWarning``), a callable that receives the
    ``RequestReport``, or a sequence of those. It is used only for requests
    with findings or over their budget.

    ``max_events`` caps the statements stored per request (None: no cap);
    past it they are counted but not stored.

    ``sample_rate`` is the fraction of requests measured, chosen at random;
    the others are not measured at all (statements of a ``track()`` unit left
    out count for the enclosing unit). ``min_db_time`` (seconds) skips the
    N+1 rules for a request whose database time (``RequestReport.db_time``)
    is below it; budgets still apply.

    ``page_report`` (``"off"``, ``"footer"`` or ``"console"``) makes the
    middleware add a request's findings to its HTML response: as a footer
    element, or as a ``console.warn`` script (a footer when the response has
    a Content-Security-Policy). ``track()`` ignores it.
    """

    relationship_threshold: int = 2
    repeat_threshold: int = 5
    column_load_threshold: int = 5
    small_repeat_threshold: int | None = None
    write_loop_threshold: int | None = None
    repeat_scope: str = SCOPE_REQUEST
    budgets: Mapping[str, int] = field(default_factory=dict)
    default_budget: int | None = None
    report: ReportTarget | Sequence[ReportTarget] = "log"
    max_events: int | None = 1000
    sample_rate: float = 1.0
    min_db_time: float = 0.0
    page_report: str = "off"

    def __post_init__(self) -> None:
        _check_threshold("relationship_threshold", self.relationship_threshold)
        _check_threshold("repeat_threshold", self.repeat_threshold)
        _check_threshold("column_load_threshold", self.column_load_threshold)
        small = self.small_repeat_threshold
        if small is not None:
            _check_threshold("small_repeat_threshold", small)
            if small >= self.repeat_threshold:
                raise ValueError(
                    "small_repeat_threshold must be below repeat_threshold "
                    f"({self.repeat_threshold}), got {small!r}"
                )
        if self.write_loop_threshold is not None:
            _check_threshold("write_loop_threshold", self.write_loop_threshold)
        if self.repeat_scope not in REPEAT_SCOPES:
            raise ValueError(
                f"repeat_scope must be one of {', '.join(REPEAT_SCOPES)}, "
                f"got {self.repeat_scope!r}"
            )
        for key, limit in dict(self.budgets).items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(
                    f"a budget key must be a non-empty string, got {key!r}"
                )
            _check_threshold(f"budget {key!r}", limit, minimum=0)
        if self.default_budget is not None:
            _check_threshold("default_budget", self.default_budget, minimum=0)
        if self.max_events is not None:
            _check_threshold("max_events", self.max_events, minimum=1)
        rate = self.sample_rate
        if not _is_number(rate) or not 0 <= rate <= 1:
            raise ValueError(f"sample_rate must be a number from 0 to 1, got {rate!r}")
        if not _is_number(self.min_db_time) or not self.min_db_time >= 0:
            raise ValueError(
                f"min_db_time must be a number of seconds >= 0, got {self.min_db_time!r}"
            )
        if self.page_report not in PAGE_REPORTS:
            raise ValueError(
                f"page_report must be one of {', '.join(PAGE_REPORTS)}, "
                f"got {self.page_report!r}"
            )
        for target in self.targets:
            if not callable(target) and target not in _TARGETS:
                raise ValueError(
                    "report must be 'log', 'warn', a callable or a sequence of "
                    f"them, got {target!r}"
                )
        # A frozen copy, so a dictionary changed later does not change budgets.
        object.__setattr__(self, "budgets", dict(self.budgets))

    @property
    def targets(self) -> tuple[ReportTarget, ...]:
        report = self.report
        if isinstance(report, str) or callable(report):
            return (report,)
        return tuple(report)

    def budget_for(
        self, method: str | None, route: str | None, path: str | None
    ) -> int | None:
        """The statement budget of a request; ``default_budget`` without a match."""
        names = [name for name in (route, path) if name]
        for key, limit in self.budgets.items():
            want_method, pattern = _split_budget_key(key)
            if want_method and (method or "").upper() != want_method:
                continue
            if any(fnmatchcase(name, pattern) for name in names):
                return limit
        return self.default_budget


@dataclass(eq=False, kw_only=True)
class RequestReport:
    """What one request (or one ``track()`` block) ran and what was found.

    Filled when the request ends: ``statements`` holds the stored statements
    (at most ``Settings.max_events``), ``statement_count`` all of them.
    ``duration`` and ``db_time`` are in seconds; ``started`` is a Unix time.
    ``error`` is set when the in-app mode hit an internal error during the
    request; the report is then incomplete and not checked. ``sampled`` is
    False for a ``track()`` unit that ``sample_rate`` left out: nothing was
    measured or reported.
    """

    id: int
    name: str
    method: str | None = None
    path: str | None = None
    route: str | None = None
    status: int | None = None
    started: float = 0.0
    duration: float | None = None
    statements: tuple[Query, ...] = ()
    statement_count: int = 0
    db_time: float = 0.0
    findings: tuple[Finding, ...] = ()
    budget: int | None = None
    error: str | None = None
    sampled: bool = True
    _message: str = field(default="", repr=False)

    @property
    def over_budget(self) -> bool:
        return self.budget is not None and self.statement_count > self.budget

    @property
    def truncated(self) -> bool:
        return self.statement_count > len(self.statements)

    @property
    def problems(self) -> bool:
        """True when the request has findings or ran over its budget."""
        return bool(self.findings) or self.over_budget

    @property
    def message(self) -> str:
        """The text the ``log`` and ``warn`` reports use; empty without problems."""
        return self._message

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready dictionary; the panel's ``requests.json`` uses it."""
        root = display_root()
        return {
            "id": self.id,
            "name": self.name,
            "method": self.method,
            "path": self.path,
            "route": self.route,
            "status": self.status,
            "started": self.started,
            "duration": self.duration,
            "statement_count": self.statement_count,
            "truncated": self.truncated,
            "db_time": self.db_time,
            "budget": self.budget,
            "over_budget": self.over_budget,
            "error": self.error,
            "statements": [
                {
                    "sql": q.sql,
                    "duration": q.duration,
                    "relationship": q.relationship,
                    "file": _relative(q.callsite.filename, root)
                    if q.callsite
                    else None,
                    "line": q.callsite.lineno if q.callsite else None,
                    "function": q.callsite.function if q.callsite else None,
                }
                for q in self.statements
            ],
            "findings": [
                {
                    "kind": f.kind,
                    "label": f.label,
                    "file": f.file,
                    "line": f.line,
                    "function": f.function,
                    "count": f.count,
                    "message": f.message,
                }
                for f in self.findings
            ],
        }


def _relative(filename: str, root: Path) -> str:
    """As findings show paths: relative to the root when inside it."""
    try:
        return Path(filename).relative_to(root).as_posix()
    except ValueError:
        return filename


# --- internal error state -------------------------------------------------
# Separate from listeners.py: an in-app bug must not switch off the pytest
# plugin's measurement, nor the reverse.

_error: BaseException | None = None
_error_lock = threading.Lock()


def internal_error() -> BaseException | None:
    """The error that switched the in-app mode off, or None."""
    return _error


def fail(exc: BaseException, source: str) -> None:
    """Switch the in-app mode off after a bug; log it once, never raise."""
    global _error
    with _error_lock:
        if _error is not None:
            return
        _error = exc
    try:
        frames = traceback.extract_tb(exc.__traceback__)
        where = f" at {os.path.basename(frames[-1].filename)}:{frames[-1].lineno}"
    except Exception:  # noqa: BLE001  # the report of a bug must not raise
        where = ""
    try:
        logger.error(
            "queryguard: in-app mode switched off after an internal error in %s%s: "
            "%s: %s. The application is not affected; please report this.",
            source,
            where,
            type(exc).__name__,
            exc,
        )
    except Exception:  # noqa: BLE001,S110  # a broken logging setup must not break the app
        pass


def _reset_for_tests() -> None:
    global _error
    _error = None


# --- the current request --------------------------------------------------


def sampled(settings: Settings) -> bool:
    """Whether to measure the next unit: ``sample_rate`` of them, at random."""
    rate = settings.sample_rate
    return rate >= 1 or random.random() < rate  # not for security


@dataclass(eq=False, slots=True)
class Active:
    """What the listeners write to: the collector of the current request.

    ``owner`` is the unit whose block set it, when that is not ``tracker``:
    a unit left out by sampling, which writes to the enclosing unit.
    """

    collector: QueryCollector
    tracker: Tracker
    owner: Tracker | None = None


_P = ParamSpec("_P")
_R = TypeVar("_R")

current: contextvars.ContextVar[Active | None] = contextvars.ContextVar(
    "queryguard_app_current", default=None
)
_ids = itertools.count(1)
# The collector of a unit left out by sampling; nothing is written to it.
_NONE = QueryCollector(max_events=1)


def live() -> Active | None:
    """The current unit's ``Active``, or None outside one or once it finished.

    A thread can keep a finished unit's context: a pool worker on builds where
    threads inherit the creator's context. Its statements are dropped then,
    not stored in the finished unit's collector.
    """
    active = current.get()
    if active is None or active.tracker.finished:
        return None
    return active


class Tracker:
    """One measured unit (a request): starts, stops and reports it."""

    __slots__ = (
        "_done",
        "_started",
        "_token",
        "active",
        "report",
        "settings",
        "sites",
        "warning",
    )

    def __init__(
        self,
        settings: Settings,
        name: str,
        *,
        method: str | None = None,
        path: str | None = None,
        sampled: bool = True,
    ) -> None:
        self.settings = settings
        self.report = RequestReport(
            id=next(_ids),
            name=name,
            method=method,
            path=path,
            started=time.time(),
            sampled=sampled,
        )
        # A unit left out by sampling is finished from the start: finish()
        # reports nothing, and on its own live() drops its statements.
        self._done = not sampled
        collector = QueryCollector(max_events=settings.max_events) if sampled else _NONE
        self.active = Active(collector, self)
        self._token: contextvars.Token[Active | None] | None = None
        # The unit's own call sites: each line's callers are those of its
        # first run in this unit, not in another unit that ran it earlier or
        # runs it at the same time.
        self.sites: Sites = {}
        self._started = time.perf_counter()
        # The QueryGuardWarning a warning filter turned into an error, for the
        # caller to raise once the unit itself has ended without an exception.
        self.warning: Warning | None = None

    def start(self) -> None:
        self._started = time.perf_counter()
        outer = current.get()
        if self._done and outer is not None:
            # Not sampled: the enclosing unit keeps collecting.
            self.active = Active(outer.collector, outer.tracker, self)
        self._token = current.set(self.active)

    @property
    def finished(self) -> bool:
        return self._done

    def detach(self) -> None:
        """Leave the context (the request's own code has finished)."""
        token, self._token = self._token, None
        if token is None:
            return
        try:
            current.reset(token)
        except ValueError:
            # Reset from another context (a WSGI server closing the body in
            # another one): clear only this context's value.
            if current.get() is self.active:
                current.set(None)

    def finish(self, route: str | None = None) -> RequestReport:
        """Detach, run the detectors and report; idempotent, never raises."""
        self.detach()
        report = self.report
        if self._done:
            return report
        self._done = True
        try:
            report.duration = time.perf_counter() - self._started
            if route is not None:
                report.route = route
            self._evaluate()
        except Exception as exc:  # noqa: BLE001  # never break the application
            fail(exc, "the end of a request")
            report.error = report.error or f"{type(exc).__name__}: {exc}"
            return report
        self.warning = _emit(report, self.settings)
        return report

    def preview(self, route: str | None = None) -> RequestReport | None:
        """The report as it stands now, for the page report; None without problems.

        The unit goes on: statements that run later (a background task) count
        for the report ``finish()`` makes. Nothing is reported from here.
        """
        if self._done:
            return None
        report = dataclasses.replace(self.report, findings=(), budget=None)
        if route is not None and report.route is None:
            report.route = route
        _check(report, self.settings, self.active.collector, statements=False)
        return report if report.problems and report.error is None else None

    def _evaluate(self) -> None:
        _check(self.report, self.settings, self.active.collector, statements=True)


def _check(
    report: RequestReport,
    settings: Settings,
    collector: QueryCollector,
    *,
    statements: bool,
) -> None:
    """Fill ``report`` from ``collector``: counts, budget, findings, message."""
    if statements:
        report.statements = tuple(_to_query(e) for e in list(collector.statements))
    report.statement_count = collector.statement_count
    report.db_time = collector.db_time
    error = internal_error()
    if error is not None:
        # The counts may be incomplete: no findings, no budget verdict.
        report.error = f"{type(error).__name__}: {error}"
        return
    report.budget = settings.budget_for(report.method, report.route, report.path)
    root = display_root()
    scope = SCOPE_SESSION if settings.repeat_scope == SCOPE_SESSION else SCOPE_TEST
    if report.db_time >= settings.min_db_time:
        report.findings = tuple(
            make_findings(
                detect(
                    collector,
                    relationship_threshold=settings.relationship_threshold,
                    repeat_threshold=settings.repeat_threshold,
                    column_load_threshold=settings.column_load_threshold,
                    small_repeat_threshold=settings.small_repeat_threshold,
                    write_loop_threshold=settings.write_loop_threshold,
                    scope=scope,
                    sieve=Sieve(),
                ),
                cwd=root,
            )
        )
    if not report.problems:
        return
    parts = [f"queryguard: {report.name}"]
    if report.route and report.route != report.path:
        parts[0] += f" (route {report.route})"
    parts[0] += f": {report.statement_count} statements"
    if report.findings:
        parts.append(format_findings(report.findings, cwd=root))
    if report.over_budget:
        assert report.budget is not None
        parts.append(
            format_budget(
                report.budget,
                list(collector.statements),
                cwd=root,
                total=report.statement_count,
                db_time=report.db_time,
            )
        )
    report._message = "\n\n".join(parts)


def _emit(report: RequestReport, settings: Settings) -> Warning | None:
    """Report to every target; return the warning a filter made an error.

    The caller raises it only when the unit itself ended without an exception,
    so the application's own exception is never replaced.
    """
    if not report.problems:
        return None
    raised: Warning | None = None
    for target in settings.targets:
        if target == "warn":
            # Follows the warning filters: "error" turns it into an exception.
            # A throwaway registry: the "default" action would otherwise show
            # each message once per process and keep every one of them in the
            # caller's module registry, which grows for a long-running server.
            caller = sys._getframe(1)
            try:
                warnings.warn_explicit(
                    QueryGuardWarning(report.message),
                    QueryGuardWarning,
                    caller.f_code.co_filename,
                    caller.f_lineno,
                    module=caller.f_globals.get("__name__"),
                    registry={},
                )
            except Warning as exc:
                raised = exc
            continue
        try:
            if target == "log":
                logger.warning("%s", report.message)
            elif callable(target):
                target(report)
        except Exception:  # a reporter must not break the app
            logger.exception("queryguard: the report callback %r raised", target)
    return raised


def set_route(template: str) -> None:
    """Name the route of the current request, for budgets and reports.

    WSGI has no route templates, so a Flask application calls this from a
    ``before_request`` hook with ``request.url_rule.rule``. Outside a
    request it does nothing.
    """
    active = current.get()
    if active is not None:
        active.tracker.report.route = template


class track:  # a function to its callers, as in 0.5.0
    """Measure everything watched engines run in one unit of work.

    For work outside HTTP: a job, a CLI command, a message handler, a task of
    Celery or RQ. Takes a ``Settings`` or its fields as keyword arguments,
    and is used in any of these ways::

        with track("nightly export") as report: ...
        async with track("sync accounts") as report: ...

        @track()                      # named after the function
        def export(): ...

        @track(default_budget=20)     # coroutine and generator functions too
        async def handle(message): ...

        unit = track(task.name)       # from signals that start and end a task
        unit.start()
        ...
        report = unit.stop()

    The ``RequestReport`` is filled and reported when the unit ends. A
    ``QueryGuardWarning`` that a warning filter turned into an error is
    raised when the unit ends without an exception of its own.

    One object can be entered by several threads or tasks at once (a
    module-level ``UNIT = track("handler")``): each ``with`` is a unit of its
    own. ``start()`` and ``stop()`` are for one unit at a time.
    """

    __slots__ = ("_name", "_settings", "_tracker")

    def __init__(
        self, name: str = "", *, settings: Settings | None = None, **options: Any
    ) -> None:
        self._name = name
        self._settings = _settings(settings, options)
        self._tracker: Tracker | None = None

    def _begin(self) -> Tracker:
        settings = self._settings
        tracker = Tracker(settings, self._name or "track()", sampled=sampled(settings))
        tracker.start()
        return tracker

    def start(self) -> RequestReport:
        """Begin the unit in the current context; pair with ``stop()``."""
        if self._tracker is not None:
            raise RuntimeError("this track() unit has already started")
        self._tracker = self._begin()
        return self._tracker.report

    def stop(self, *, failed: bool = False) -> RequestReport:
        """End the unit, report it and return its report.

        ``failed=True`` when the work itself failed: a warning turned into an
        error is then not raised, as a ``with`` block does not raise it over
        the block's own exception. Called from another context than
        ``start()``, it still ends the unit; statements that later run in the
        starting context are dropped.
        """
        tracker, self._tracker = self._tracker, None
        if tracker is None:
            raise RuntimeError("this track() unit has not started")
        tracker.finish()
        if not failed and tracker.warning is not None:
            raise tracker.warning
        return tracker.report

    def __enter__(self) -> RequestReport:
        return self._begin().report

    def __exit__(self, kind: object, value: object, tb: object) -> None:
        # Units nest like the blocks, so the current one is this block's own.
        active = current.get()
        if active is None:
            return
        tracker = active.owner or active.tracker
        tracker.finish()
        if kind is None and tracker.warning is not None:
            raise tracker.warning

    async def __aenter__(self) -> RequestReport:
        return self.__enter__()

    async def __aexit__(self, kind: object, value: object, tb: object) -> None:
        self.__exit__(kind, value, tb)

    def __call__(self, function: Callable[_P, _R]) -> Callable[_P, _R]:
        """Measure each call of ``function`` as a unit of its own.

        For a generator function the unit spans the iteration, from the first
        item to the last; a coroutine function is measured across its awaits.
        """
        if self._tracker is not None:
            raise RuntimeError("a started track() unit cannot decorate a function")
        name = self._name or getattr(function, "__qualname__", repr(function))
        unit = track(name, settings=self._settings)
        call: Any = function
        wrapper: Callable[..., Any]

        if inspect.isasyncgenfunction(function):

            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                async with unit:
                    async for item in call(*args, **kwargs):
                        yield item

        elif inspect.isgeneratorfunction(function):

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                with unit:
                    return (yield from call(*args, **kwargs))

        elif _is_async(function):

            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                async with unit:
                    return await call(*args, **kwargs)

        else:

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                with unit:
                    return call(*args, **kwargs)

        result: Callable[_P, _R] = functools.wraps(function)(wrapper)
        return result


# What inspect.signature() shows: calling track(...) gives a unit.
track.__signature__ = inspect.signature(track).replace(  # type: ignore[attr-defined]
    return_annotation="track"
)


def _is_async(function: Callable[..., Any]) -> bool:
    """A coroutine function, or an object whose ``__call__`` is one."""
    if inspect.iscoroutinefunction(function):
        return True
    method = type(function).__dict__.get("__call__")  # an instance with async __call__
    return method is not None and inspect.iscoroutinefunction(method)


def _settings(settings: Settings | None, options: Mapping[str, Any]) -> Settings:
    if settings is not None and options:
        raise TypeError("pass either settings= or its fields, not both")
    return settings if settings is not None else Settings(**options)
