from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import overload

from .blocks import Sieve
from .collector import QueryCollector, pop, pop_local, push, push_local
from .detectors import SCOPES, detect
from .events import CallSite, StatementEvent
from .listeners import install, internal_error, record_error
from .messages import (
    display_root,
    format_budget,
    format_findings,
    format_time_budget,
    make_findings,
)
from .warnings import QueryGuardInternalWarning, default, renamed, warn_internal


class QueryBudgetExceeded(AssertionError):
    """Raised when a block runs more SQL statements than its budget allows."""


class NPlusOneDetected(AssertionError):
    """Raised by ``QueryLog.assert_no_nplusone()`` when it finds N+1 queries."""


@contextmanager
def _measure(
    limit: int | None,
    time_limit: float | None,
    *,
    local: bool = False,
    exact: bool = False,
) -> Iterator[QueryLog]:
    """The budget block; ``local`` counts only this thread's or task's statements.

    With ``exact``, any other number of statements than ``limit`` fails.
    """
    # The budget failure is raised here; pytest should show the user's with block.
    __tracebackhide__ = True
    install()
    collector = QueryCollector(statements_only=local)
    log = QueryLog(collector)
    earlier = internal_error()  # recording is already off if this is set
    (push_local if local else push)(collector)
    stopped: BaseException | None = None
    try:
        yield log
    except NPlusOneDetected as exc:
        if getattr(exc, "_queryguard_strict", False):
            stopped = exc
        raise
    finally:
        (pop_local if local else pop)(collector)
        error = log._finish(earlier)  # however the block ends, the log is fixed
        if stopped is not None:
            # queryguard_strict stopped the block at a lazy load: a budget it
            # already went over is gone over whatever the rest would have run
            # (too few statements is no verdict then), so it is shown with
            # the N+1 instead of being lost with the block's end
            try:
                text = _budget_failure(
                    log, collector, limit, time_limit, exact=exact, too_few=False
                )
                if text is not None:
                    add_to_strict_error(stopped, text)
            except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
                record_error(exc, "budget of a block stopped by strict mode")
    if local and error is not None:
        warn_internal(
            QueryGuardInternalWarning(
                "the QueryLog of this queryguard block may be incomplete: "
                f"pytest-queryguard stopped recording after an internal error: {error!r}"
            ),
            stacklevel=3,
        )
    # After an internal error statements may be missing: too few is no verdict.
    text = _budget_failure(
        log, collector, limit, time_limit, exact=exact, too_few=error is None
    )
    if text is not None:
        raise QueryBudgetExceeded(text)


def _budget_failure(
    log: QueryLog,
    collector: QueryCollector,
    limit: int | None,
    time_limit: float | None,
    *,
    exact: bool,
    too_few: bool,
) -> str | None:
    """The failure text of a finished budget block, or None when it kept to it.

    With ``exact`` and ``too_few``, fewer statements than ``limit`` fail it too.
    """
    # The verdict uses the log's fixed total: a statement still running in
    # another thread may finish after the block and grow the collector's.
    db_time = log.db_time
    count = log.count
    if limit is not None and (count > limit or (exact and too_few and count < limit)):
        return format_budget(
            limit,
            collector.statements[:count],
            cwd=display_root(),
            db_time=db_time,
            exact=exact,
        )
    if time_limit is not None and db_time > time_limit:
        return format_time_budget(
            time_limit,
            collector.statements,
            cwd=display_root(),
            db_time=db_time,
        )
    return None


def add_to_strict_error(error: BaseException, text: str) -> None:
    """Add a budget failure under the N+1 of the error queryguard_strict raised."""
    message = str(error.args[0]) if error.args else ""
    error.args = (f"{message}\n\n{text}",)


def max_queries(limit: int) -> AbstractContextManager[QueryLog]:
    """Fail when the block runs more than ``limit`` SQL statements.

    Yields the same ``QueryLog`` as ``record_queries()``: ``with max_queries(3)
    as log:`` gives ``log.count``, ``log.queries`` and the other members.
    """
    _check_count("limit", limit)
    return _measure(limit, None)


def _check_count(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value!r}")


def max_db_time(seconds: float) -> AbstractContextManager[QueryLog]:
    """Fail when the statements of the block take more than ``seconds`` in total.

    Yields the same ``QueryLog`` as ``record_queries()``.

    The time is what the database and the driver took for each statement,
    measured around the cursor call; it does not include the time the code in
    the block spends outside the database. The failure lists the slowest
    statements.
    """
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not seconds >= 0  # also rejects NaN
        or seconds == float("inf")
    ):
        raise ValueError(f"seconds must be a non-negative number, got {seconds!r}")
    return _measure(None, float(seconds))


@dataclass(frozen=True, slots=True, kw_only=True)
class Query:
    """One SQL statement recorded by ``record_queries()``.

    ``sql`` is the statement text as SQLAlchemy sent it to the driver.
    ``callsite`` is the first stack frame outside SQLAlchemy, pytest, the
    standard library, site-packages and this plugin, or None. When the
    statement loaded a relationship, lazily or as the extra query of
    ``selectinload``, ``relationship`` names it, as in ``"Order.customer"``;
    otherwise it is None. ``duration`` is the time in seconds the database and
    the driver took for the statement, measured around the cursor call; None
    while the statement is still running, and when it raised.

    Not constructible by users: only ``record_queries()`` creates one. The
    fields are public; the constructor is not (it is keyword-only, and fields
    may be added in a minor release).
    """

    sql: str
    callsite: CallSite | None
    relationship: str | None
    duration: float | None = None


def _to_query(event: StatementEvent) -> Query:
    return Query(
        sql=event.sql,
        callsite=event.callsite,
        relationship=event.relationship,
        duration=event.duration,
    )


class QueryLog:
    """The statements recorded by one ``record_queries()`` block.

    Inside the block it grows as statements run; after the block it no
    longer changes. It is sized, iterable and indexable (``len()``, ``for``,
    ``log[i]`` and ``log[a:b]``) but not a ``collections.abc.Sequence``: its
    ``count`` is a number, not the ``count(value)`` method of a sequence. An
    empty log is falsy, like any sized container.

    Not constructible by users: ``record_queries()`` is the only way to get
    one. Its constructor takes an internal type and may change in any release.
    """

    __slots__ = ("_collector", "_done", "_error", "_frozen", "_frozen_db_time")

    def __init__(self, collector: QueryCollector) -> None:
        # Not for direct use: only record_queries() creates a QueryLog.
        self._collector = collector
        # Set by _finish() when the block ends: an internal error during the
        # block, or the one that had already switched recording off.
        self._done = False
        self._error: BaseException | None = None
        self._frozen: tuple[Query, ...] = ()
        self._frozen_db_time = 0.0

    def _finish(self, earlier: BaseException | None) -> BaseException | None:
        self._error = internal_error() or earlier
        self._frozen = self._snapshot()  # built once; the log no longer changes
        self._frozen_db_time = self._collector.db_time
        self._done = True
        return self._error

    def _incomplete(self) -> BaseException | None:
        """The internal error that may have left this log incomplete, or None."""
        return self._error if self._done else internal_error()

    @property
    def queries(self) -> tuple[Query, ...]:
        """Every recorded statement, in the order it ran."""
        if self._done:
            return self._frozen
        return self._snapshot()

    def _snapshot(self) -> tuple[Query, ...]:
        return tuple(_to_query(event) for event in list(self._collector.statements))

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[Query]:
        return iter(self.queries)

    @overload
    def __getitem__(self, index: int) -> Query: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[Query, ...]: ...

    def __getitem__(self, index: int | slice) -> Query | tuple[Query, ...]:
        """One recorded statement, or a tuple of them for a slice."""
        if self._done:
            return self._frozen[index]
        if isinstance(index, slice):  # live: convert only what is asked for
            return tuple(_to_query(e) for e in self._collector.statements[index])
        return _to_query(self._collector.statements[index])

    @property
    def count(self) -> int:
        """The number of recorded statements."""
        if self._done:  # the same tuple that queries and iteration use
            return len(self._frozen)
        return len(self._collector.statements)

    @property
    def db_time(self) -> float:
        """Seconds the recorded statements took in total, as ``Query.duration``.

        Counts every statement, including those beyond ``queryguard_max_events``
        that are not stored. Fixed when the block ends, like ``queries``.
        """
        if self._done:
            return self._frozen_db_time
        return self._collector.db_time

    def slowest(self, n: int = 5) -> tuple[Query, ...]:
        """The ``n`` slowest finished statements, slowest first."""
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError(f"n must be a non-negative integer, got {n!r}")
        timed = [q for q in self.queries if q.duration is not None]
        return tuple(sorted(timed, key=lambda q: q.duration or 0.0, reverse=True)[:n])

    def assert_no_nplusone(
        self,
        *,
        relationship_threshold: int = 2,
        repeat_threshold: int = 5,
        column_load_threshold: int = 5,
        small_repeat_threshold: int | None = None,
        write_loop_threshold: int | None = None,
        scope: str = default("test"),
        repeat_scope: str | None = None,
    ) -> None:
        """Raise ``NPlusOneDetected`` when the recorded block has N+1 queries.

        The rules, and the defaults of the thresholds, are the plugin's: one
        relationship lazy-loaded for ``relationship_threshold`` different
        parent objects that one query line loaded, or one ``SELECT``, without its literals and
        parameters, run ``repeat_threshold`` times (pagination and chunked
        ``IN`` loads, recognised by their parameters, do not count), or
        ``column_load_threshold``
        instances of one class each loading deferred or expired columns on
        their own. With ``small_repeat_threshold`` (an integer, at least 2 and
        below ``repeat_threshold``), a statement that ran that many times or
        more from one line, each time with other parameters, counts too,
        unless it paginates. With ``write_loop_threshold`` (an integer, at least
        2), that many single-row INSERT, UPDATE or DELETE statements of one
        shape from one line count too.

        ``scope`` is ``"test"`` (the default: everything in the block counts
        together) or ``"session"``: each SQLAlchemy ``Session`` (or, for a
        connection no Session opened, each connection checkout) counts on its
        own, which is what a block that makes several independent requests needs.
        ``repeat_scope`` is its deprecated name (removed in 2.0).

        The thresholds and ``scope`` are these arguments only. The ``queryguard_*`` ini
        options, ``queryguard_mode`` and the ``queryguard_ignore`` marker are not
        read, so a project that changes ``queryguard_repeat_threshold``,
        ``queryguard_relationship_threshold`` or
        ``queryguard_column_load_threshold``,
        ``queryguard_small_repeat_threshold`` or
        ``queryguard_write_loop_threshold`` must pass the same values here.

        Raises ``RuntimeError`` instead of checking when pytest-queryguard hit
        an internal error and stopped recording, because the log may then be
        incomplete (``record_queries()`` also warns with
        ``QueryGuardInternalWarning`` when the block ends).
        """
        __tracebackhide__ = True
        if self._collector.statements_only:
            raise RuntimeError(
                "a queryguard fixture block counts statements only, without the "
                "relationship and column loads the N+1 rules need: use "
                "record_queries() for assert_no_nplusone()"
            )
        error = self._incomplete()
        if error is not None:
            raise RuntimeError(
                "the query log may be incomplete, so it cannot be checked: "
                f"pytest-queryguard stopped recording after an internal error: {error!r}"
            )
        for name, value in (
            ("relationship_threshold", relationship_threshold),
            ("repeat_threshold", repeat_threshold),
            ("column_load_threshold", column_load_threshold),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 2:
                raise ValueError(f"{name} must be an integer >= 2, got {value!r}")
        small = small_repeat_threshold
        if small is not None and (
            isinstance(small, bool)
            or not isinstance(small, int)
            or not 2 <= small < repeat_threshold
        ):
            raise ValueError(
                "small_repeat_threshold must be None or an integer >= 2 and "
                f"below repeat_threshold ({repeat_threshold}), got {small!r}"
            )
        scope = renamed("scope", scope, "repeat_scope", repeat_scope)
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}, got {scope!r}")
        loop = write_loop_threshold
        if loop is not None and (
            isinstance(loop, bool) or not isinstance(loop, int) or loop < 2
        ):
            raise ValueError(
                f"write_loop_threshold must be None or an integer >= 2, got {loop!r}"
            )
        root = display_root()
        findings = make_findings(
            detect(
                self._collector,
                relationship_threshold=relationship_threshold,
                repeat_threshold=repeat_threshold,
                column_load_threshold=column_load_threshold,
                small_repeat_threshold=small_repeat_threshold,
                write_loop_threshold=write_loop_threshold,
                scope=scope,
                sieve=Sieve(),
            ),
            cwd=root,
        )
        if findings:
            raise NPlusOneDetected(format_findings(findings, cwd=root))

    def __repr__(self) -> str:
        return f"<QueryLog {self.count} {'query' if self.count == 1 else 'queries'}>"


@contextmanager
def _record() -> Iterator[QueryLog]:
    install()
    collector = QueryCollector()
    log = QueryLog(collector)
    earlier = internal_error()  # recording is already off if this is set
    push(collector)
    try:
        yield log
    finally:
        pop(collector)
        # However the block ends, the log is fixed. The warning below is for a
        # normal exit only: it must not replace the exception that ended it.
        error = log._finish(earlier)
    if error is not None:
        warn_internal(
            QueryGuardInternalWarning(
                "the QueryLog of this block may be incomplete: pytest-queryguard "
                f"stopped recording after an internal error: {error!r}"
            ),
            stacklevel=3,
        )


def record_queries() -> AbstractContextManager[QueryLog]:
    """Record every SQL statement SQLAlchemy sends inside the block.

    Statements from any thread count. The yielded ``QueryLog`` gives the
    statements, their number and an N+1 check.

    When the block ends normally after an internal error, it issues
    ``QueryGuardInternalWarning``. A block left by an exception does not warn,
    so the warning never replaces that exception; the log is fixed either way.
    """
    return _record()


class QueryGuardFixture:
    """What the ``queryguard`` fixture gives a test.

    ``max_queries()`` and ``num_queries()`` blocks count the statements run in
    the test's own thread or asyncio task, and in the tasks and threads that
    run in a copy of its context: a task created inside the block,
    ``asyncio.to_thread()``, ``contextvars.copy_context().run()``, or any new
    thread on a free-threaded build of Python 3.14. A thread or task that
    was running before the block does not count.

    ``queries`` and ``count`` are the statements of the whole test function,
    from every thread, like ``record_queries()``.

    Not constructible by users: only the fixture creates one. Use the class
    for type hints.
    """

    __slots__ = ("_earlier", "_log", "_state")

    def __init__(self) -> None:
        # Not for direct use: only the queryguard fixture creates one.
        self._log = QueryLog(QueryCollector(statements_only=True))
        self._earlier: BaseException | None = None
        self._state = "new"  # then "running" and "done", or "unused"

    def _start(self) -> None:
        """Record the statements of the test function (the plugin calls this)."""
        if self._state != "new":
            return
        install()
        self._earlier = internal_error()
        push(self._log._collector)
        self._state = "running"

    def _stop(self) -> None:
        """Stop recording; ``queries`` no longer changes. Safe to call again."""
        if self._state == "new":
            self._state = "unused"
            return
        if self._state != "running":
            return
        self._state = "done"
        pop(self._log._collector)
        error = self._log._finish(self._earlier)
        if error is not None:
            warn_internal(
                QueryGuardInternalWarning(
                    "queryguard.queries of this test may be incomplete: "
                    "pytest-queryguard stopped recording after an internal "
                    f"error: {error!r}"
                )
            )

    def max_queries(self, limit: int) -> AbstractContextManager[QueryLog]:
        """Fail when the block runs more than ``limit`` statements in this thread.

        Raises ``QueryBudgetExceeded`` with every statement of the block, as
        ``pytest_queryguard.max_queries()`` does, and yields a ``QueryLog`` of
        the block (without ``assert_no_nplusone()``, which needs
        ``record_queries()``). Blocks can be nested; each counts its own
        statements.
        """
        _check_count("limit", limit)
        return _measure(limit, None, local=True)

    def num_queries(self, expected: int) -> AbstractContextManager[QueryLog]:
        """Fail unless the block runs exactly ``expected`` statements in this thread.

        Like ``max_queries()``, but fewer statements fail too, with the same
        ``QueryBudgetExceeded``: pytest-django's ``django_assert_num_queries``.
        """
        _check_count("expected", expected)
        return _measure(expected, None, local=True, exact=True)

    def _started_log(self) -> QueryLog:
        if self._state in ("new", "unused"):
            raise RuntimeError(
                "queryguard.queries counts the statements of the test function, "
                "which has not run: read it in the test or in a fixture's teardown"
            )
        return self._log

    @property
    def queries(self) -> tuple[Query, ...]:
        """The statements the test function has run so far, from every thread.

        Counted from the start of the test function to its end: statements
        that fixtures run in their setup or teardown are not here. Raises
        ``RuntimeError`` before the test function starts.
        """
        return self._started_log().queries

    @property
    def count(self) -> int:
        """The number of statements in ``queries``."""
        return self._started_log().count

    def __repr__(self) -> str:
        count = "-" if self._state in ("new", "unused") else self._log.count
        return f"<QueryGuardFixture {count} {'query' if count == 1 else 'queries'}>"
