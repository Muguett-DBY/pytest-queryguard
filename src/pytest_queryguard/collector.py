from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field

from .events import (
    CartesianLoadEvent,
    ColumnLoadEvent,
    RelationshipLoadEvent,
    ScanEvent,
    StatementEvent,
)

# Without the GIL (free-threaded CPython) ``count += 1`` from several threads
# loses updates, so the add_* methods take a lock there. With the GIL the
# check below is the whole cost. The GIL can be switched on later (a C
# extension that needs it) but never off, so a build that starts without it
# keeps the lock, which is only ever redundant.
_FREE_THREADED = not getattr(sys, "_is_gil_enabled", lambda: True)()


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
    explained: set[object] = field(default_factory=set)
    scans: list[ScanEvent] = field(default_factory=list)
    index_notices: dict[str, None] = field(default_factory=dict)  # ordered set
    explain_errors: list[tuple[str, str]] = field(default_factory=list)
    # One entry per statement left out because a partial index may serve it:
    # the tables of that statement's skipped scans.
    partial_skipped: list[tuple[str, ...]] = field(default_factory=list)
    # Most statements (and, separately, relationship loads) whose details are
    # stored; None for no limit. Past it the collector only counts.
    max_events: int | None = None
    statement_count: int = 0
    relationship_load_count: int = 0
    column_load_count: int = 0
    # Seconds the statements took, all of them, stored or only counted.
    db_time: float = 0.0
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
        if self.max_events is not None and len(self.cartesian_loads) >= self.max_events:
            return
        if _FREE_THREADED:
            with self._lock:
                self.cartesian_loads.append(event)
            return
        self.cartesian_loads.append(event)

    def add_db_time(self, seconds: float) -> None:
        if _FREE_THREADED:
            with self._lock:
                self.db_time += seconds
        else:
            self.db_time += seconds


# Process-wide on purpose: the code under test may run in another thread.
_stack: list[QueryCollector] = []
# The tuple active() returns, rebuilt on push and pop only: the listeners
# call active() for every statement.
_active: tuple[QueryCollector, ...] = ()
_generation = 0


def generation() -> int:
    """Changes on every push/pop; lets per-measurement state detect staleness."""
    return _generation


def push(collector: QueryCollector) -> None:
    global _generation, _active
    _generation += 1
    _stack.append(collector)
    _active = tuple(_stack)


def pop(collector: QueryCollector) -> None:
    global _generation, _active
    _generation += 1
    for i in range(len(_stack) - 1, -1, -1):
        if _stack[i] is collector:
            del _stack[i]
            break
    _active = tuple(_stack)


def active() -> tuple[QueryCollector, ...]:
    return _active


def _reset_for_tests() -> None:
    global _generation, _active
    _generation += 1
    _stack.clear()
    _active = ()
