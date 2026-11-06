from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field

from . import callsite
from .blocks import IgnoreBlock, covers
from .blocks import current as current_blocks
from .events import (
    CallSite,
    CartesianLoadEvent,
    ColumnLoadEvent,
    RelationshipLoadEvent,
    ScanEvent,
    StatementEvent,
)
from .findings import KIND_RELATIONSHIP

# Without the GIL (free-threaded CPython) ``count += 1`` from several threads
# loses updates, so the add_* methods take a lock there. With the GIL the
# check below is the whole cost. The GIL can be switched on later (a C
# extension that needs it) but never off, so a build that starts without it
# keeps the lock, which is only ever redundant.
_FREE_THREADED = not getattr(sys, "_is_gil_enabled", lambda: True)()


@dataclass(slots=True, eq=False)
class StrictGroup:
    """The lazy loads of one relationship for the parents of one query line."""

    first: CallSite | None
    parents: set[object] = field(default_factory=set)
    suppressed: bool = False  # its finding is ignored or baselined


@dataclass(slots=True, eq=False)
class QueryCollector:
    statements: list[StatementEvent] = field(default_factory=list)
    relationship_loads: list[RelationshipLoadEvent] = field(default_factory=list)
    column_loads: list[ColumnLoadEvent] = field(default_factory=list)
    # SELECTs that join collections side by side (cartesian.py); few per test
    cartesian_loads: list[CartesianLoadEvent] = field(default_factory=list)
    # Missing-index check (see indexes.py); only used when index_check is set.
    index_check: bool = False
    # Suggest an index for each finding; hypopg is "auto", "on" or "off".
    index_advisor: bool = False
    hypopg: str = "auto"
    # The ignore() blocks entered in the measured test's context, for the
    # stale-block report (see blocks.record_entered).
    blocks_entered: dict[tuple[str, int], IgnoreBlock] = field(default_factory=dict)
    # The blocks that left out a finding in a check run in that context, such
    # as QueryLog.assert_no_nplusone (an ordered set).
    blocks_used: dict[IgnoreBlock, None] = field(default_factory=dict)
    explained: set[object] = field(default_factory=set)
    # A statement is explained at its first run only. For one first explained
    # inside an ignore() block: explain key -> the blocks of every run of it
    # (blocks.current() of each), so a run outside the block is not missed.
    # Kept when DDL clears explained (see catalog.clear).
    index_runs: dict[object, dict[tuple[IgnoreBlock, ...], None]] = field(
        default_factory=dict
    )
    scans: list[ScanEvent] = field(default_factory=list)
    index_notices: dict[str, None] = field(default_factory=dict)  # ordered set
    explain_errors: list[tuple[str, str]] = field(default_factory=list)
    # One entry per statement left out because a partial index may serve it:
    # the tables of that statement's skipped scans.
    partial_skipped: list[tuple[str, ...]] = field(default_factory=list)
    # Most statements (and, separately, relationship loads) whose details are
    # stored; None for no limit. Past it the collector only counts.
    max_events: int | None = None
    # Counts statements only (the queryguard fixture's): it gets no relationship,
    # column or cartesian loads, and while only such collectors are active the
    # listeners skip the work those need.
    statements_only: bool = False
    # Set when a context-local collector is taken off: copies of its context
    # (tasks, threads) that outlive the block send it nothing more.
    closed: bool = False
    statement_count: int = 0
    relationship_load_count: int = 0
    column_load_count: int = 0
    cartesian_load_count: int = 0
    # Seconds the statements took, all of them, stored or only counted.
    db_time: float = 0.0
    # Strict mode (queryguard_strict): called with a lazy load, the call site
    # of the first lazy load of its group and the number of parents, once the
    # relationship has been lazy-loaded for ``strict_threshold`` parents that
    # one query line loaded; returns the exception to raise, or None.
    strict: (
        Callable[[RelationshipLoadEvent, CallSite | None, int], BaseException | None]
        | None
    ) = None
    strict_threshold: int = 2
    # count each Session on its own, as queryguard_repeat_scope = session does
    strict_by_scope: bool = False
    # (relationship, query site of the parents, Session or None) -> group
    strict_groups: dict[tuple[object, ...], StrictGroup] = field(default_factory=dict)
    # Guards the counters and the length checks on a free-threaded build.
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    @property
    def truncated(self) -> bool:
        """True when some statements or loads were counted but not stored."""
        return (
            self.statement_count > len(self.statements)
            or self.relationship_load_count > len(self.relationship_loads)
            or self.column_load_count > len(self.column_loads)
            or self.cartesian_load_count > len(self.cartesian_loads)
        )

    def add_statement(self, event: StatementEvent) -> None:
        # The body is written twice, not shared, so the GIL build pays no
        # extra call for the free-threaded one.
        if _FREE_THREADED:
            with self._lock:
                self.statement_count += 1
                if self.max_events is None or len(self.statements) < self.max_events:
                    self.statements.append(event)
            return
        self.statement_count += 1
        if self.max_events is None or len(self.statements) < self.max_events:
            self.statements.append(event)

    def add_relationship_load(self, event: RelationshipLoadEvent) -> None:
        if _FREE_THREADED:
            with self._lock:
                self.relationship_load_count += 1
                if (
                    self.max_events is None
                    or len(self.relationship_loads) < self.max_events
                ):
                    self.relationship_loads.append(event)
            return
        self.relationship_load_count += 1
        if self.max_events is None or len(self.relationship_loads) < self.max_events:
            self.relationship_loads.append(event)

    def add_column_load(self, event: ColumnLoadEvent) -> None:
        if _FREE_THREADED:
            with self._lock:
                self.column_load_count += 1
                if self.max_events is None or len(self.column_loads) < self.max_events:
                    self.column_loads.append(event)
            return
        self.column_load_count += 1
        if self.max_events is None or len(self.column_loads) < self.max_events:
            self.column_loads.append(event)

    def add_cartesian_load(self, event: CartesianLoadEvent) -> None:
        if _FREE_THREADED:
            with self._lock:
                self.cartesian_load_count += 1
                if (
                    self.max_events is None
                    or len(self.cartesian_loads) < self.max_events
                ):
                    self.cartesian_loads.append(event)
            return
        self.cartesian_load_count += 1
        if self.max_events is None or len(self.cartesian_loads) < self.max_events:
            self.cartesian_loads.append(event)

    def strict_check(self, load: RelationshipLoadEvent) -> BaseException | None:
        """The strict-mode exception for this lazy load, or None.

        Only lazy loads of parents whose query line is known count, so an
        object read on its own, or objects the test made itself, never raise
        here. A group whose finding is ignored or baselined is decided once.
        A load inside an ``ignore("relationship-load")`` block never raises.
        """
        parent = load.parent
        if self.strict is None or parent is None or parent.callsite is None:
            return None
        scope = load.scope if self.strict_by_scope else None
        key = (load.relationship, parent.callsite, scope)
        if _FREE_THREADED:
            with self._lock:
                group = self._strict_group(key, load)
        else:
            group = self._strict_group(key, load)
        if group is None:
            return None
        if load.ignores and covers(load.ignores, KIND_RELATIONSHIP):
            # inside an ignore() block: the load counts for the group (a
            # later load outside the block raises), this one never raises
            return None
        error = self.strict(load, group.first, len(group.parents))
        if error is None:
            group.suppressed = True
        return error

    def _strict_group(
        self, key: tuple[object, ...], load: RelationshipLoadEvent
    ) -> StrictGroup | None:
        """The group of this load once it has reached the threshold, else None."""
        group = self.strict_groups.get(key)
        if group is None:
            group = self.strict_groups[key] = StrictGroup(first=load.callsite)
        if group.suppressed:
            return None
        if len(group.parents) < self.strict_threshold:  # no more is needed
            group.parents.add(load.parent_key)
        return group if len(group.parents) >= self.strict_threshold else None

    def note_index_run(self, key: object, tags: tuple[IgnoreBlock, ...]) -> None:
        """Another run of an explained statement (see ``index_runs``)."""
        runs = self.index_runs.get(key)
        if runs is not None and tags not in runs:
            runs[tags] = None

    def add_db_time(self, seconds: float) -> None:
        if _FREE_THREADED:
            with self._lock:
                self.db_time += seconds
        else:
            self.db_time += seconds


def note_index_reruns(
    targets: Sequence[QueryCollector], fresh: Sequence[QueryCollector], key: object
) -> None:
    """The statement under ``key`` ran again for the targets not in ``fresh``.

    Only a statement first explained inside an ignore() block is followed, so
    the usual cost is a dict lookup per collector.
    """
    if len(fresh) == len(targets):
        return
    tags: tuple[IgnoreBlock, ...] | None = None
    for target in targets:
        if key not in target.index_runs or any(t is target for t in fresh):
            continue
        if tags is None:
            tags = current_blocks()
        target.note_index_run(key, tags)


def note_index_first_run(
    fresh: Sequence[QueryCollector], key: object, tags: tuple[IgnoreBlock, ...]
) -> None:
    """The statement under ``key`` was explained, with scans, inside ``tags``.

    Explained again after a catalog change (see ``catalog.clear``), it adds
    to the runs noted before.
    """
    for target in fresh:
        runs = target.index_runs.get(key)
        if runs is not None:
            runs[tags] = None
        elif tags:
            target.index_runs[key] = {tags: None}


# Process-wide on purpose: the code under test may run in another thread.
_stack: list[QueryCollector] = []
# The tuples active() and full_active() return, rebuilt on push and pop only:
# the listeners call them for every statement.
_active: tuple[QueryCollector, ...] = ()
_active_full: tuple[QueryCollector, ...] = ()
# The same as _active_full, as a list that is never rebound: the per-row
# listeners read it without a call (listeners._collectors).
_full_stack: list[QueryCollector] = []
_generation = 0


def generation() -> int:
    """Changes on every push/pop; lets per-measurement state detect staleness."""
    return _generation


def _rebuild() -> None:
    global _active, _active_full
    _active = tuple(_stack)
    _active_full = tuple(c for c in _active if not c.statements_only)
    _full_stack[:] = _active_full


def push(collector: QueryCollector) -> None:
    global _generation
    _generation += 1
    callsite.new_measurement()
    _stack.append(collector)
    _rebuild()


def pop(collector: QueryCollector) -> None:
    global _generation
    _generation += 1
    for i in range(len(_stack) - 1, -1, -1):
        if _stack[i] is collector:
            del _stack[i]
            break
    _rebuild()


def active() -> tuple[QueryCollector, ...]:
    return _active


def full_active() -> tuple[QueryCollector, ...]:
    """The active collectors that want loads too, not only statements."""
    return _active_full


# Statements-only collectors that count only the statements run in the
# context that pushed them (the queryguard fixture's blocks): the test's
# thread or asyncio task, and the tasks and threads that run in a copy of its
# context (a task created inside the block, asyncio.to_thread, any new thread
# on a free-threaded build). They do not change the generation: the
# attributions of other tasks and of the process-wide collectors stay valid.
_local: ContextVar[tuple[QueryCollector, ...]] = ContextVar(
    "queryguard_local", default=()
)
_local_lock = threading.Lock()
# Open context-local collectors in the process; while 0, targets() is active().
_live_local = 0
# Bumped when one closes, so the cached combination below is rebuilt.
_local_version = 0
# (active, local tuple, version, combined): the last combination built.
_combined: tuple[object, object, int, tuple[QueryCollector, ...]] | None = None


def push_local(collector: QueryCollector) -> None:
    global _live_local
    with _local_lock:
        _live_local += 1
    _local.set((*_local.get(), collector))


def pop_local(collector: QueryCollector) -> None:
    """Close ``collector`` and take it off this context's stack.

    Other contexts that still hold it (copies made inside the block) skip it
    from now on.
    """
    global _live_local, _local_version, _combined
    with _local_lock:
        if not collector.closed:
            collector.closed = True
            _live_local -= 1
            _local_version += 1
            _combined = None
    stack = _local.get()
    for i in range(len(stack) - 1, -1, -1):
        if stack[i] is collector:
            _local.set(stack[:i] + stack[i + 1 :])
            return


def targets() -> tuple[QueryCollector, ...]:
    """The collectors a statement of this thread or task goes to."""
    global _combined
    if not _live_local:
        return _active
    local = _local.get()
    if not local:
        return _active
    active, version, cached = _active, _local_version, _combined
    if (
        cached is not None
        and cached[0] is active
        and cached[1] is local
        and cached[2] == version
    ):
        return cached[3]
    live = tuple(c for c in local if not c.closed)
    combined = (*active, *live) if live else active
    _combined = (active, local, version, combined)
    return combined


def _reset_for_tests() -> None:
    global _generation, _live_local, _local_version, _combined
    _generation += 1
    _stack.clear()
    _rebuild()
    _local.set(())
    with _local_lock:
        _live_local = 0
        _local_version += 1
        _combined = None
