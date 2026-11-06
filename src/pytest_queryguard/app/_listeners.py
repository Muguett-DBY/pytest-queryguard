"""The in-app listeners: per engine, writing to the current request only.

They mirror the pytest listeners in ``listeners.py`` but read the collector
from a ``ContextVar`` instead of the process-wide stack, and keep their own
error state. Registered only by ``watch()``; nothing runs before that.
"""

from __future__ import annotations

import threading
import time
import weakref
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .. import blocks
from .._compat import option_engine_parent
from ..callsite import find_callsite, find_flush
from ..events import StatementEvent
from ..listeners import (
    _NO_LABELS,
    _is_write,
    _orm_load,
    _Written,
    flush_record,
    orm_select,
    scope_of_connection,
    scope_of_session,
)
from . import _core

_TIMING_ATTR = "_queryguard_app_timing"
_SCOPE_ATTR = "_queryguard_scope"  # the attribute listeners.py tags with
# The objects the latest flush of a Session wrote, with the id of the unit
# (request or track() block) it ran in. Apart from the plugin's record: each
# lets one refresh per class and flush through, and must not take the other's.
_WRITTEN_ATTR = "_queryguard_app_written"


def _written(session: Any) -> _Written | None:
    """The record of the Session's latest flush, if it ran in the current unit.

    Called by ``_orm_load`` for a refresh only, from the cursor listener of
    the unit's own statement.
    """
    entry = getattr(session, "__dict__", {}).get(_WRITTEN_ATTR)
    active = _core.live()
    if entry is None or active is None or entry[0] != active.tracker.report.id:
        return None
    return entry[1]  # type: ignore[no-any-return]


def _on_after_flush(session: Session, flush_context: Any) -> None:
    """Remember which objects this flush wrote, for the refresh that follows."""
    if _core._error is not None or not _watched:
        return
    try:
        active = _core.live()
        if active is None:
            session.__dict__.pop(_WRITTEN_ATTR, None)
            return
        session.__dict__[_WRITTEN_ATTR] = (
            active.tracker.report.id,
            flush_record(session),
        )
    except Exception as exc:  # noqa: BLE001  # never break the application
        _core.fail(exc, "the after_flush listener")


def _on_after_begin(session: Session, transaction: Any, connection: Any) -> None:
    """Tag the connection with its Session, for repeat_scope="session"."""
    if _core.live() is None or _core._error is not None or not _watched:
        return
    try:
        scope = scope_of_session(session)
        if scope is not None:
            setattr(connection, _SCOPE_ATTR, scope)
    except Exception as exc:  # noqa: BLE001  # never break the application
        _core.fail(exc, "the after_begin listener")


def _on_before_cursor_execute(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    active = _core.live()
    if active is None or _core._error is not None:
        return
    try:
        collector = active.collector
        sites = active.tracker.sites
        write = _is_write(statement)
        ignores = blocks.current()
        # The relationship or column load this SELECT runs for, from the ORM's
        # options of this execution, as in listeners.py (no do_orm_execute
        # listener): the same labels as the plugin's, with the unit's own
        # call sites and record of the latest flush.
        orm = None if write else orm_select(context)
        labels = (
            None
            if orm is None
            else _orm_load(
                conn, orm, (collector,), ignores, sites, _written, parents=False
            )
        )
        if labels is None:
            labels = _NO_LABELS
        column_load = labels.column_load
        if column_load is not None:
            column_load.sql = statement
        site = labels.site
        stmt = StatementEvent(
            statement,
            site if site is not None else find_callsite(sites),
            labels.relationship,
            column_load=column_load is not None,
            lazy_load=labels.lazy,
            parameters=None if executemany else parameters,
            executemany=bool(executemany),
            flush=find_flush() if write else None,
            scope=scope_of_connection(conn),
            ignores=ignores,
        )
        collector.add_statement(stmt)
        if context is not None:
            setattr(context, _TIMING_ATTR, (stmt, collector, time.perf_counter()))
    except Exception as exc:  # noqa: BLE001  # never break the application
        _core.fail(exc, "the before_cursor_execute listener")


def _on_after_cursor_execute(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    now = time.perf_counter()
    if _core._error is not None:
        return
    try:
        timing = getattr(context, _TIMING_ATTR, None)
        if timing is None:
            return
        setattr(context, _TIMING_ATTR, None)
        stmt, collector, started = timing
        stmt.duration = now - started
        collector.add_db_time(stmt.duration)
    except Exception as exc:  # noqa: BLE001  # never break the application
        _core.fail(exc, "the after_cursor_execute listener")


# --- watch / unwatch -------------------------------------------------------

_lock = threading.Lock()
_watched: weakref.WeakSet[Engine] = weakref.WeakSet()
_session_listeners = False


def _in_chain(engine: Any, ancestor: Engine) -> bool:
    """Whether ``engine`` is ``ancestor`` or was derived from it.

    ``engine.execution_options(...)`` returns an ``OptionEngine`` that shares
    its parent's listeners; ``_proxied`` leads back to that parent.
    """
    while engine is not None:
        if engine is ancestor:
            return True
        engine = option_engine_parent(engine)
    return False


def _in_watched(engine: Any) -> bool:
    """Whether ``engine`` or an engine it was derived from is watched."""
    while engine is not None:
        if engine in _watched:
            return True
        engine = option_engine_parent(engine)
    return False


def _sync_engine(engine: Any) -> Engine:
    if isinstance(engine, Engine):
        return engine
    sync = getattr(engine, "sync_engine", None)  # AsyncEngine
    if isinstance(sync, Engine):
        return sync
    raise TypeError(
        f"watch() takes a SQLAlchemy Engine or AsyncEngine, got {type(engine).__name__}"
    )


def watch(engine: Any) -> None:
    """Measure the statements of ``engine`` in requests and ``track()`` blocks.

    Takes an ``Engine`` or an ``AsyncEngine``; calling it again for the same
    engine does nothing. Call it at startup, before the engine hands out
    connections: a ``Connection`` opened earlier does not see the listeners.
    An engine made with ``engine.execution_options()`` shares its parent's
    listeners: watching the parent covers it, and watching the parent after
    it moves its listeners to the parent.
    """
    sync = _sync_engine(engine)
    with _lock:
        _drop_session_listeners_if_unused()
        if _in_watched(sync):
            return
        # Engines derived from this one would run both sets of listeners.
        for derived in [e for e in _watched if _in_chain(e, sync)]:
            _remove_engine_listeners(derived)
        event.listen(sync, "before_cursor_execute", _on_before_cursor_execute)
        event.listen(sync, "after_cursor_execute", _on_after_cursor_execute)
        _watched.add(sync)
        _add_session_listeners()


def unwatch(engine: Any) -> None:
    """Stop measuring ``engine``; does nothing when it was not passed to watch().

    An engine watched through its parent (see ``watch()``) shares the parent's
    listeners and stops with ``unwatch(parent)``.
    """
    sync = _sync_engine(engine)
    with _lock:
        if sync in _watched:
            _remove_engine_listeners(sync)
        _drop_session_listeners_if_unused()


def _remove_engine_listeners(engine: Engine) -> None:
    event.remove(engine, "before_cursor_execute", _on_before_cursor_execute)
    event.remove(engine, "after_cursor_execute", _on_after_cursor_execute)
    _watched.discard(engine)


def _add_session_listeners() -> None:
    global _session_listeners
    if not _session_listeners:
        event.listen(Session, "after_begin", _on_after_begin)
        event.listen(Session, "after_flush", _on_after_flush)
        _session_listeners = True


def _drop_session_listeners_if_unused() -> None:
    """Remove the shared Session listeners once no engine is watched.

    Also after the last watched engine was garbage collected without
    ``unwatch()``; not from a finalizer, which could run in the middle of a
    SQLAlchemy event dispatch. Until then the listeners return at once.
    """
    global _session_listeners
    if not _watched and _session_listeners:
        event.remove(Session, "after_begin", _on_after_begin)
        event.remove(Session, "after_flush", _on_after_flush)
        _session_listeners = False


def is_watched(engine: Any) -> bool:
    """True while ``engine`` (or the sync engine of an AsyncEngine) is watched.

    An engine made with ``execution_options()`` is watched with its parent.
    """
    return _in_watched(_sync_engine(engine))
