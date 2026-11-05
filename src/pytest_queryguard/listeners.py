from __future__ import annotations

import contextlib
import itertools
import os
import re
import sys
import time
import traceback
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapper, ORMExecuteState, Session, loading, strategies
from sqlalchemy.orm.attributes import instance_state
from sqlalchemy.orm.context import QueryContext
from sqlalchemy.orm.session import SessionTransaction
from sqlalchemy.pool import Pool

from . import blocks, catalog, collector, indexes, mysql_index, sqlite_index, sqltext
from .callsite import find_callsite, find_flush
from .events import (
    CAUSE_DEFERRED,
    CAUSE_EXPIRED,
    CAUSE_REFRESH,
    CallSite,
    ColumnLoadEvent,
    ParentLoad,
    RelationshipLoadEvent,
    StatementEvent,
)

_installed = False
_index_installed = False
_error: BaseException | None = None
_failure: Failure | None = None
# Scope numbers for the repeat rules: one per Session, one per connection that
# no Session opened. Process-wide and never reused; the number is stored on the
# Session or Connection object, so it cannot be mixed up by a recycled id().
_scopes = itertools.count(1)
_SCOPE_ATTR = "_queryguard_scope"
# The attribution of an ORM execution waits on its own Session, and a
# connection finds that Session through the weak reference that after_begin
# stores on it. Nothing here is per thread: asyncio tasks share a thread, and
# statements of other sessions or of begin listeners run between an ORM
# execution and its SELECT.
_PENDING_ATTR = "_queryguard_pending"
_SESSION_ATTR = "_queryguard_session"
# The objects the latest flush of a Session wrote, for the refresh that a
# repository runs after each create or update (see _on_after_flush).
_WRITTEN_ATTR = "_queryguard_written"


@dataclass(frozen=True, slots=True)
class Failure:
    """What went wrong inside the plugin, for the warning and the summary."""

    source: str  # the listener or step that raised
    error: BaseException
    raised_at: str | None  # "file.py:12 in function", innermost frame

    @property
    def summary(self) -> str:
        return f"{type(self.error).__name__}: {self.error}"


def internal_error() -> BaseException | None:
    """The first exception swallowed by a listener, if any."""
    return _error


def internal_failure() -> Failure | None:
    """Which listener or step failed first, and why; None without an error.

    The error itself is the one ``internal_error()`` returns. When it was set
    without a source (``swap_error()``), the source is "unknown".
    """
    if _error is None:
        return None
    if _failure is not None and _failure.error is _error:
        return _failure
    return Failure("unknown", _error, _innermost(_error))


def _innermost(exc: BaseException) -> str | None:
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return None
    last = frames[-1]
    return f"{os.path.basename(last.filename)}:{last.lineno} in {last.name}"


def _scope_of(owner: Any) -> int | None:
    """The scope number of a Session or Connection, assigned on first use."""
    try:
        scope = owner.__dict__.get(_SCOPE_ATTR)
        if scope is None:
            scope = next(_scopes)
            setattr(owner, _SCOPE_ATTR, scope)
        return scope  # type: ignore[no-any-return]
    except (AttributeError, TypeError):
        return None


def scope_of_session(session: Any) -> int | None:
    """The scope of a Session: the same number for all its transactions."""
    return _scope_of(session)


def scope_of_connection(conn: Any) -> int | None:
    """The scope of a statement's connection.

    A connection a Session opened (see ``_on_after_begin``) belongs to that
    Session; any other connection is its own scope, and ``engine.connect()``
    gives a new one for each checkout.
    """
    return _scope_of(conn)


def _on_after_begin(session: Session, transaction: Any, connection: Any) -> None:
    """Tell the connection which Session it serves, for every transaction."""
    if _error is not None:
        return
    try:
        # Without the tags the connection counts as a scope of its own and no
        # attribution is handed to its statements.
        with contextlib.suppress(AttributeError, TypeError):
            setattr(connection, _SCOPE_ATTR, scope_of_session(session))
            # Every live Session that began on this connection: an external
            # connection can serve several (the "test in a transaction" fixture
            # with a second Session joined to it).
            refs = connection.__dict__.get(_SESSION_ATTR, ())
            if not any(ref() is session for ref in refs):
                live = tuple(ref for ref in refs if ref() is not None)
                setattr(connection, _SESSION_ATTR, (*live, weakref.ref(session)))
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _fail(exc, "after_begin listener (session scopes)")


# Orders the ORM executions of all Sessions: of the Sessions that share a
# connection, the one that executed last owns the connection's next SELECT.
_executions = itertools.count(1)
# The collector generation of the latest parked attribution that names
# something. While it differs from the current generation no
# connection can have a pending attribution that _take_pending would hand out,
# so the cursor listener skips the lookup.
_pending_generation = -1


def _set_pending(
    session: Any,
    name: str | None,
    column_load: ColumnLoadEvent | None = None,
    lazy: bool = False,
    site: CallSite | None = None,
) -> None:
    """Park the attribution of the ORM execution of ``session``.

    An execution with nothing to attribute is recorded too, so that it
    supersedes an older attribution parked by another Session of the same
    connection whose SELECT never ran.
    """
    global _pending_generation
    generation = collector.generation()
    # Never lowered: a thread still in an older measurement must not hide the
    # attribution another thread parked in the current one.
    if (name is not None or column_load is not None) and (
        generation > _pending_generation
    ):
        _pending_generation = generation
    try:
        session.__dict__[_PENDING_ATTR] = (
            next(_executions),
            generation,
            name,
            column_load,
            lazy,
            site,
        )
    except (AttributeError, TypeError):
        pass


def _latest(conn: Any) -> tuple[Any, Any]:
    """(Session, parked entry) of the latest ORM execution on ``conn``."""
    best: Any = None
    owner: Any = None
    for ref in conn.__dict__.get(_SESSION_ATTR, ()):
        session = ref()
        if session is None:
            continue
        entry = session.__dict__.get(_PENDING_ATTR)
        if entry is not None and (best is None or entry[0] > best[0]):
            best, owner = entry, session
    return owner, best


def _take_pending(
    conn: Any,
) -> tuple[str | None, ColumnLoadEvent | None, bool, CallSite | None]:
    """(relationship, column load, lazy, call site) of the ORM execution that
    ``conn`` serves.

    ``lazy`` is True for a lazy load, False for the query of an eager loader such
    as selectinload, which carries the same relationship label.

    Only the Sessions that began on the connection are asked; a statement on
    any other connection leaves every parked attribution alone.
    """
    try:
        session, entry = _latest(conn)
        if entry is None or (entry[2] is None and entry[3] is None):
            return None, None, False, None
        session.__dict__[_PENDING_ATTR] = (entry[0], entry[1], None, None, False, None)
    except (AttributeError, TypeError):
        return None, None, False, None
    if entry[1] == collector.generation():
        return entry[2], entry[3], entry[4], entry[5]
    return None, None, False, None


# Code of the method that opens a Session's connection: it runs the engine's
# begin event and the after_begin listeners. Statements issued while it is on
# the stack belong to those listeners, never to the ORM execution waiting for
# its SELECT. None when this SQLAlchemy has no such method (then nothing is
# skipped, as without the check).
_BEGIN_CODE = getattr(
    getattr(SessionTransaction, "_connection_for_bind", None), "__code__", None
)


def _inside_begin() -> bool:
    if _BEGIN_CODE is None:
        return False
    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_code is _BEGIN_CODE:
            return True
        frame = frame.f_back  # type: ignore[assignment]
    return False


# Code of the post-load step that runs an "immediate" load. SQLAlchemy swaps a
# selectin or subquery relationship for it when it refreshes an object (the
# mapped eager loader needs a result of several parents, a refresh has one), and
# an explicit immediateload() or lazy="immediate" runs it too. None when this
# SQLAlchemy has no such method (then every load with a parent counts as lazy).
# The class is named ImmediateLoader in older releases and _ImmediateLoader later.
_IMMEDIATE_LOADER = getattr(
    strategies, "_ImmediateLoader", getattr(strategies, "ImmediateLoader", None)
)
_IMMEDIATE_CODE = getattr(
    getattr(_IMMEDIATE_LOADER, "_load_for_path", None), "__code__", None
)


# How many frames up from the listener the immediate loader may be. It is five
# or six frames away (listener, event dispatch, Session.execute, the lazy
# loader); the bound keeps a lazy load outside a refresh from walking the
# whole pytest stack.
_MAX_LOADER_DEPTH = 40


def _is_refresh_eager_load(parent: Any, prop: Any) -> bool:
    """Whether the load running now is a mapped eager loader run by a refresh.

    ``session.refresh(obj)`` (and the reload of expired attributes) of an
    object with a ``lazy="selectin"`` or ``lazy="subquery"`` relationship loads
    the collection through the immediate loader, one parent per statement: the
    statement carries a parent state, like a lazy load, but it is the mapped
    eager strategy at work and ``selectinload`` would change nothing. The
    outer query of that step is a refresh (``refresh_state`` is set); an
    ``immediateload()`` of a plain query has none and stays a lazy load.

    Only the loader's own query counts: ``parent`` must be one of the states it
    loads and ``prop`` its relationship. A lazy load that user code (a
    reconstructor, a ``load`` event) runs while the loader builds the children
    is a lazy load like anywhere else.
    """
    if _IMMEDIATE_CODE is None:
        return False
    frame: Any = sys._getframe(1)
    for _ in range(_MAX_LOADER_DEPTH):
        if frame is None:
            return False
        if frame.f_code is _IMMEDIATE_CODE:
            local = frame.f_locals
            if getattr(local.get("context"), "refresh_state", None) is None:
                return False
            if getattr(local.get("self"), "parent_property", None) is not prop:
                return False
            return any(state is parent for state, _ in local.get("states") or ())
        frame = frame.f_back
    return False


@dataclass(slots=True)
class _Written:
    """The objects one flush of a Session inserted or updated."""

    # InstanceState objects: they hold their object by a weak reference only,
    # so the set keeps no user object alive (and is replaced by the next flush).
    states: set[Any]
    # The classes whose refresh after this flush was not counted already.
    refreshed: set[type] = field(default_factory=set)


def _on_after_flush(session: Session, flush_context: Any) -> None:
    """Remember which objects this flush wrote, for ``is_refresh_after_write``.

    Only the latest flush counts: a newer one replaces the set.
    """
    if _error is not None:
        return
    try:
        if not collector.full_active():
            session.__dict__.pop(_WRITTEN_ATTR, None)
            return
        session.__dict__[_WRITTEN_ATTR] = flush_record(session)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _fail(exc, "after_flush listener (refresh after write)")


def flush_record(session: Any) -> _Written:
    """The objects the flush running now writes, from its ``after_flush`` event.

    The caller stores the record: the plugin and the in-app mode each keep
    their own.
    """
    # Still the state before the flush here: new and dirty are what it
    # wrote. An object in dirty without a net change (an attribute set to
    # its own value) got no UPDATE, so a refresh of it reads back nothing.
    states = {instance_state(obj) for obj in session.new}
    states.update(
        instance_state(obj) for obj in session.dirty if session.is_modified(obj)
    )
    return _Written(states)


def is_refresh_after_write(written: _Written | None, instance: Any) -> bool:
    """Whether this refresh reads back an object the latest flush wrote.

    A repository's ``create()`` or ``update()`` flushes one object and
    refreshes it to read server defaults and onupdate values: a statement the
    caller asked for, once per write, and not a loop of reads. One refresh per
    class and flush is let through, so refreshing every object of a bulk
    ``add_all()`` one by one is still counted, as is a refresh of an object
    that was only read.

    ``written`` is the record of the Session's latest flush (``flush_record``);
    a refresh that is let through is taken off it.
    """
    if written is None or instance not in written.states:
        return False
    cls = instance.class_
    if cls in written.refreshed:
        return False
    written.refreshed.add(cls)
    written.states.discard(instance)
    return True


def column_load_counts(load: ColumnLoadEvent, instance: Any, written: Any) -> bool:
    """Whether a column load counts, given the record of the latest flush.

    A refresh that reads back that flush's write keeps its column-load label
    (so it is not a repeated statement either) but is not counted.
    """
    return load.cause != CAUSE_REFRESH or not is_refresh_after_write(written, instance)


def _nested_code(function: Any, name: str, variable: str) -> Any:
    """The code of the function ``name`` defined in ``function`` that reads
    ``variable`` from it; None when this SQLAlchemy has no such function."""
    for const in getattr(getattr(function, "__code__", None), "co_consts", ()):
        if getattr(const, "co_name", None) == name and variable in const.co_freevars:
            return const
    return None


# Where the parents of a lazy load came from. A read stores its call site on
# its execution context, which its result keeps (_SITE_ATTR); each instance a
# query loads gets (call site, load path, measurement) on its InstanceState
# (_ORIGIN_ATTR), so it goes away with the object. The measurement is the
# collector generation: an origin noted in another test, or outside the
# current record_queries() block, is not used.
_SITE_ATTR = "_queryguard_site"
_ORIGIN_ATTR = "_queryguard_origin"
# The generator in loading.instances() that turns the rows of one result into
# instances: it holds the result's cursor and its query context. And the
# function that builds one instance from a row, which holds the load path of
# the rows it builds. None when this SQLAlchemy has no such function: then no
# parent site, or no load path of a reread instance, is known.
_CHUNKS_CODE = _nested_code(getattr(loading, "instances", None), "chunks", "cursor")
_INSTANCE_CODE = _nested_code(
    getattr(loading, "_instance_processor", None), "_instance", "load_path"
)
# How many frames up from the listener the rows of the query may be: a few for
# a top-level query, more for a joined or selectin child several levels down.
# Walked once per query, not per row.
_MAX_QUERY_DEPTH = 200
# (runid, call site) of the query whose instances were seen last: the rows of
# one query come in one after another, so the walk runs once per query.
_last_query: tuple[int, CallSite | None] = (0, None)
# The live collectors that want loads (not the statements-only ones of the
# queryguard fixture); the per-row listeners only need to know if there is
# one, without a call per row. collector.py never rebinds the list.
_collectors = collector._full_stack
# id(statement) -> (statement, call site) of the reads of the current
# measurement: the query context that turns a result into instances holds the
# statement it ran, so its call site is found without a walk up the stack.
# _AMBIGUOUS when one statement object ran from two places (its results may
# be read in any order); then, and for a statement not here, the stack is
# walked. Holds the statements, so it is emptied for each measurement and
# takes no more once full (a statement not here is found by the walk). Lazy
# loads and eager loader statements are left out: their instances are found
# through the parent or the outer query.
_reads: dict[int, tuple[Any, CallSite | None]] = {}
_reads_generation = -1
_MAX_READS = 256
_AMBIGUOUS = CallSite(filename="", lineno=0, function="")
# (statement, call site) of the latest read, unless that statement is
# ambiguous: the rows of a query are nearly always turned into instances right
# after it ran, so this finds the site even once _reads is full (a loop that
# builds a new select() per iteration fills it after 256).
_last_read: tuple[Any, CallSite | None] = (None, None)


def _is_orm(statement: Any) -> bool:
    """Whether a statement may load ORM instances (a select of entities)."""
    attrs = getattr(statement, "_propagate_attrs", None)
    return attrs is not None and attrs.get("compile_state_plugin") == "orm"


def _note_read(statement: Any, site: CallSite | None) -> None:
    """Remember the call site of a read, for _query_site."""
    global _reads_generation, _last_read
    generation = collector._generation
    if generation != _reads_generation:
        _reads.clear()
        _reads_generation = generation
    key = id(statement)
    entry = _reads.get(key)
    if entry is None or entry[0] is not statement:
        if len(_reads) < _MAX_READS:
            _reads[key] = (statement, site)
        _last_read = (statement, site)
    elif entry[1] is site:
        _last_read = (statement, site)
    else:
        _reads[key] = (statement, _AMBIGUOUS)
        _last_read = (None, None)


def _query_site(context: Any) -> CallSite | None:
    """The call site of the query whose rows ``context`` turns into instances.

    The instances of a lazy load come from the query that loaded its parent,
    and those of a selectin or subquery loader from the query that it runs
    for; the call site is that query's.
    """
    target = context.top_level_context or context
    parent = getattr(target.load_options, "_lazy_loaded_from", None)
    if parent is not None:
        origin = _current_origin(parent)
        return None if origin is None else origin[0]
    statement = target.query
    last = _last_read
    if last[0] is statement:
        return last[1]
    entry = _reads.get(id(statement))
    if entry is not None and entry[0] is statement and entry[1] is not _AMBIGUOUS:
        return entry[1]
    if _CHUNKS_CODE is None:
        return None
    frame: Any = sys._getframe(2)
    for _ in range(_MAX_QUERY_DEPTH):
        if frame is None:
            return None
        if frame.f_code is _CHUNKS_CODE:
            local = frame.f_locals
            if local.get("context") is target:
                execution = getattr(local.get("cursor"), "context", None)
                return getattr(execution, _SITE_ATTR, None)
        frame = frame.f_back
    return None


def _site_of(context: Any) -> CallSite | None:
    """The call site of the query of ``context``; the listeners call it once
    per query (they check ``_last_query`` first, without a call per row)."""
    global _last_query
    site = _query_site(context)
    _last_query = (context.runid, site)
    return site


def _on_instance_load(state: Any, context: Any) -> None:
    """Note which query loaded a new instance, and by which path.

    Runs for each row that becomes a new instance, so it does no more than
    store a tuple on the state; the query's call site is looked up once.
    """
    if _error is not None or not _collectors:
        return
    try:
        # Session.merge() fires it without a query context for a new object.
        run = getattr(context, "runid", None)
        if run is None:
            return
        last = _last_query
        site = last[1] if last[0] == run else _site_of(context)
        state.__dict__[_ORIGIN_ATTR] = (site, state.load_path, collector._generation)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _fail(exc, "load listener (parent sites)")


def _on_instance_refresh(state: Any, context: Any, attrs: Any) -> None:
    """Note the query that read the row of an instance the Session already had.

    A query that returns objects created in the test (or expired since) loads
    their columns again and fires this, not ``load``. A refresh of one object
    (an expired attribute, ``session.refresh()``) keeps the earlier origin: it
    does not say where the object came from. An object that two queries of
    different places or paths read has no known origin: which result the code
    goes on with cannot be told.
    """
    if _error is not None or not _collectors:
        return
    try:
        # A bulk UPDATE or DELETE that synchronizes the Session fires it
        # without a query context, and a composite attribute with a marker.
        if not isinstance(context, QueryContext) or context.refresh_state is not None:
            return
        # The state's load_path is not updated for an instance the Session
        # already had; the path of this query's rows is in the frame that
        # builds the instance, a few frames up.
        path = None
        if _INSTANCE_CODE is not None:
            frame: Any = sys._getframe(1)
            for _ in range(4):
                if frame is None:
                    break
                if frame.f_code is _INSTANCE_CODE:
                    path = frame.f_locals.get("load_path")
                    break
                frame = frame.f_back
        last = _last_query
        site = last[1] if last[0] == context.runid else _site_of(context)
        before = _current_origin(state)
        if before is not None and (before[0] is not site or before[1] != path):
            site = path = None
        state.__dict__[_ORIGIN_ATTR] = (site, path, collector._generation)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _fail(exc, "refresh listener (parent sites)")


# (load path, relationship) -> (root class, path) for the hint; see _loader_path.
_paths: dict[tuple[Any, Any], tuple[str | None, tuple[tuple[str, bool], ...]]] = {}
_MAX_PATHS = 1024


def _loader_path(
    path: Any, prop: Any
) -> tuple[str | None, tuple[tuple[str, bool], ...]]:
    """The query class and relationship chain that load ``prop``'s parent.

    ``path`` is the parent's load path: (Order mapper, Order.customer, Customer
    mapper) for a customer that ``select(Order)`` loaded through
    ``Order.customer``. The root is None when the path cannot be written as
    ``select(Root).options(loader(A.b).loader(B.c))``: an aliased class,
    ``of_type()``, a relationship of a subclass, or no path at all.
    """
    elements = getattr(path, "path", None)
    if not elements or len(elements) % 2 == 0:
        return None, ()
    key = (elements, prop)
    found = _paths.get(key)
    if found is not None:
        return found
    entities, props = elements[0::2], elements[1::2]
    found = None, ()
    if all(isinstance(entity, Mapper) for entity in entities) and entities[-1].isa(
        prop.parent
    ):
        chain = []
        for entity, step, child in zip(entities[:-1], props, entities[1:], strict=True):
            if getattr(step, "mapper", None) is not child or not entity.isa(
                step.parent
            ):
                break
            chain.append((f"{entity.class_.__name__}.{step.key}", not step.uselist))
        else:
            found = entities[0].class_.__name__, tuple(chain)
    if len(_paths) >= _MAX_PATHS:
        _paths.clear()
    _paths[key] = found
    return found


def _current_origin(state: Any) -> tuple[Any, ...] | None:
    """(call site, load path, generation) of an instance, if noted in this
    measurement."""
    origin = state.__dict__.get(_ORIGIN_ATTR)
    if origin is None or origin[2] != collector._generation:
        return None
    return origin  # type: ignore[no-any-return]


def _parent_load(parent: Any, prop: Any) -> ParentLoad | None:
    """Where the parent of this lazy load came from; None when unknown."""
    origin = _current_origin(parent)
    if origin is None:
        return None
    site, path, _ = origin
    root, chain = _loader_path(path, prop)
    if site is None and root is None:
        return None
    return ParentLoad(site, root, chain)


def _has_pending(conn: Any) -> bool:
    try:
        entry = _latest(conn)[1]
        return entry is not None and (entry[2] is not None or entry[3] is not None)
    except (AttributeError, TypeError):
        return False


def _reset_for_tests() -> None:
    global _error, _failure
    _error = None
    _failure = None


def _load_options(state: ORMExecuteState) -> Any:
    """The load options of an ORM execution, also for ``from_statement()``.

    Before SQLAlchemy 2.0.30 ``ORMExecuteState.is_select`` is False for
    ``select(Entity).from_statement(...)`` and ``load_options`` then raises.
    The loader of a joined-inheritance subclass column runs exactly such a
    statement, so read the options from the execution options directly.
    """
    try:
        return state.load_options
    except InvalidRequestError:
        return state.execution_options.get("_sa_orm_load_options")


def _is_column_load(state: ORMExecuteState) -> bool:
    """True for a refresh, an expired or a deferred column load of an instance.

    ``ORMExecuteState.is_column_load`` is False for a ``from_statement()``
    execution before SQLAlchemy 2.0.30 (see ``_load_options``), so the compile
    options of the statement are asked as well.
    """
    if state.is_column_load:
        return True
    statement = getattr(state, "statement", None)
    options = getattr(statement, "_compile_options", None)
    return bool(getattr(options, "_for_refresh_state", False))


def classify_column_load(state: ORMExecuteState) -> tuple[Any, ColumnLoadEvent] | None:
    """The instance state a column load reloads, and its event; None without one.

    It parks no attribution and counts nothing, so the in-app mode, which
    keeps its own state, labels a load exactly as the plugin does. The caller
    has checked ``_is_column_load(state)``.
    """
    load_options = _load_options(state)
    instance = getattr(load_options, "_refresh_state", None)
    if instance is None:
        return None
    options: Any = getattr(state.statement, "_compile_options", None)
    if not hasattr(options, "_only_load_props"):
        # Without it every load would look like a plain refresh, with a wrong
        # hint. Stop through the internal-error path instead of mislabeling.
        raise AttributeError(
            "SQLAlchemy's column-load compile options have no _only_load_props; "
            "this SQLAlchemy version is not supported by the column-load kind"
        )
    only = options._only_load_props
    expired = instance.expired_attributes
    columns = {prop.key for prop in instance.mapper.column_attrs}
    # session.refresh(obj, ["name"]) expires the names before it loads them,
    # so without the flag it would look like an expired load.
    user_refresh = bool(getattr(load_options, "_is_user_refresh", False))
    if only is None or user_refresh:
        cause, names = CAUSE_REFRESH, ()
    elif instance.expired or (only and set(only) <= expired):
        cause, names = CAUSE_EXPIRED, ()
    else:
        cause = CAUSE_DEFERRED
        names = tuple(sorted(set(only) & columns or only))
    key = instance.key if instance.key is not None else id(instance)
    load = ColumnLoadEvent(
        instance.mapper.class_.__name__,
        names,
        cause,
        key,
        find_callsite(),
        scope=scope_of_session(state.session),
        ignores=blocks.current(),
    )
    return instance, load


def _record_column_load(
    state: ORMExecuteState, targets: tuple[collector.QueryCollector, ...]
) -> None:
    """Note an instance that loads columns it already has a row for."""
    found = classify_column_load(state)
    if found is None:
        _set_pending(state.session, None)
        return
    instance, load = found
    _set_pending(state.session, None, column_load=load, site=load.callsite)
    if not column_load_counts(
        load, instance, state.session.__dict__.get(_WRITTEN_ATTR)
    ):
        return
    for target in targets:
        target.add_column_load(load)


def _on_orm_execute(state: ORMExecuteState) -> None:
    if _error is not None:
        return
    strict: BaseException | None = None
    try:
        # statements-only collectors (the queryguard fixture's) need no loads
        targets = collector.full_active()
        if not targets:
            return
        if _is_column_load(state):
            _record_column_load(state, targets)
            return
        if not state.is_relationship_load:
            _set_pending(state.session, None)
            return
        prop = state.loader_strategy_path[-1]  # type: ignore[index]
        name = str(prop)
        scalar = getattr(prop, "uselist", None) is False
        parent = state.lazy_loaded_from
        if parent is not None and _is_refresh_eager_load(parent, prop):
            # An eager loader's query: label the statement, count no lazy load.
            _set_pending(state.session, name, lazy=False)
            return
        key: object = None
        if parent is not None:
            key = parent.identity_key
            if key is None:
                key = id(parent)
        site = find_callsite()
        _set_pending(state.session, name, lazy=parent is not None, site=site)
        load = RelationshipLoadEvent(
            name,
            key,
            site,
            scalar,
            scope_of_session(state.session),
            None if parent is None else _parent_load(parent, prop),
            blocks.current(),
        )
        for target in targets:
            target.add_relationship_load(load)
            if target.strict is not None:
                # every strict collector counts the load; the first error wins
                error = target.strict_check(load)
                if strict is None:
                    strict = error
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _fail(exc, "do_orm_execute listener (relationship loads)")
        return
    if strict is not None:
        # queryguard_strict: the N+1 fails here, at the attribute access, with
        # the test's own traceback (outside the try: this is meant to raise)
        raise strict


_is_write = sqltext.is_write
_READ_VERBS = frozenset(("SELECT", "WITH"))


def _is_read(sql: str) -> bool:
    """Whether the statement is a SELECT or a WITH (after comments and parentheses)."""
    start = sqltext.skip_prefix(sql)
    if start is None:
        return False
    word = sql[start : start + 6].upper()
    return word == "SELECT" or word.startswith("WITH")


def _on_cursor_execute(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    if _error is not None:
        return
    try:
        targets = collector.targets()
        if not targets:
            return
        # The ORM runs do_orm_execute before it autoflushes, so the first
        # statement after a lazy load may be the flush's write, not the load's
        # SELECT. Loads are reads: leave the pending attribution for the SELECT.
        # So is a BEGIN, SAVEPOINT or SET that a begin listener runs, and so is
        # a statement on the connection of another Session.
        write = _is_write(statement)
        relationship, column_load, lazy, site = None, None, False, None
        if (
            not write
            and _pending_generation == collector.generation()
            and _has_pending(conn)
            and _is_read(statement)
            and not _inside_begin()
        ):
            relationship, column_load, lazy, site = _take_pending(conn)
        if column_load is not None:
            column_load.sql = statement
        stmt = StatementEvent(
            statement,
            site if site is not None else find_callsite(),
            relationship,
            column_load=column_load is not None,
            lazy_load=lazy,
            parameters=None if executemany else parameters,
            executemany=bool(executemany),
            flush=find_flush() if write else None,
            scope=scope_of_connection(conn),
            ignores=blocks.current(),
        )
        for target in targets:
            target.add_statement(stmt)
        if context is not None:
            invoked = None if write else getattr(context, "invoked_statement", None)
            if _collectors and _is_orm(invoked):  # only loads need the site
                # Read back when the ORM turns the rows into instances.
                setattr(context, _SITE_ATTR, stmt.callsite)
                if relationship is None:
                    _note_read(invoked, stmt.callsite)
            # Read back by the after listener; per execution, so statements
            # that overlap in time (asyncio tasks) never mix up. Taken last,
            # so the time spent finding the call site is not counted.
            context._queryguard_timing = (stmt, targets, time.perf_counter())
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _fail(exc, "before_cursor_execute listener (statements)")


def _on_timing_after(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    now = time.perf_counter()  # first, so nothing else is counted as database time
    if _error is not None:
        return
    try:
        timing = getattr(context, "_queryguard_timing", None)
        if timing is None:
            return
        context._queryguard_timing = None
        stmt, targets, started = timing
        stmt.duration = now - started
        for target in targets:
            target.add_db_time(stmt.duration)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _fail(exc, "after_cursor_execute listener (statement timing)")


def _on_after_cursor_execute(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    if _error is not None:
        return
    try:
        # Every statement, in fixtures too: DDL clears the catalog cache.
        catalog.observe(conn, statement)
        targets = [target for target in collector.active() if target.index_check]
        if targets and conn.dialect.name == sqlite_index.DIALECT:
            sqlite_index.check(conn, statement, parameters, executemany, targets)
        elif targets and conn.dialect.name in mysql_index.DIALECTS:
            no_parameters = bool(
                getattr(context, "execution_options", {}).get("no_parameters")
            )
            mysql_index.check(
                conn, cursor, statement, parameters, executemany, targets, no_parameters
            )
        elif targets:
            indexes.check(conn, statement, executemany, targets)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _fail(exc, "after_cursor_execute listener (missing-index check)")


def _guarded(
    handler: Callable[..., None], event_name: str | None = None
) -> Callable[..., None]:
    """A transaction or pool listener that never raises into the user's code."""

    name = event_name or getattr(handler, "__name__", "transaction")

    def listener(*args: Any) -> None:
        if _error is not None:
            return
        try:
            handler(*args)
        except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
            _fail(exc, f"{name} listener (catalog cache)")

    return listener


_on_commit = _guarded(catalog.on_commit, "commit")
_on_rollback = _guarded(catalog.on_rollback, "rollback")
_on_commit_twophase = _guarded(catalog.on_commit_twophase, "commit_twophase")
_on_rollback_twophase = _guarded(catalog.on_rollback_twophase, "rollback_twophase")
_on_rollback_savepoint = _guarded(catalog.on_rollback_savepoint, "rollback_savepoint")
_on_invalidate = _guarded(catalog.on_invalidate, "invalidate")


def _fail(exc: Exception, source: str) -> None:
    global _error, _failure
    if _error is None:
        _error = exc
        _failure = Failure(source, exc, _innermost(exc))


def record_error(exc: Exception, source: str = "plugin") -> None:
    """Disable measurement after a bug outside the listeners (first error wins)."""
    _fail(exc, source)


def swap_error(new: BaseException | None) -> BaseException | None:
    """Replace the recorded error and return the previous one.

    The plugin clears it for each session and restores the outer session's
    error afterwards, so in-process nested sessions stay independent.
    """
    global _error
    old, _error = _error, new
    return old


# Options the ORM reads into a statement's load options. On the affected
# versions a loader statement inherits them from the outer statement in the
# first pass and reads them as its own in the second; see _on_orm_execute_heal.
_LEAKED_LOAD_OPTIONS = ("yield_per", "autoflush", "populate_existing", "identity_token")
_MISSING = object()


def _leaks_load_options() -> bool:
    """Whether this SQLAlchemy lets a ``do_orm_execute`` listener leak load options.

    From 2.0.20 (issue 10231, loaders inherit the outer statement's execution
    options) to 2.0.49, and in 2.1.0b1 and 2.1.0b2, any listener makes the
    outer statement's ``yield_per``, ``autoflush`` and similar options become
    load options of its ``selectinload`` statement (SQLAlchemy issue 13301,
    fixed in 2.0.50 and 2.1.0b3). ``yield_per`` then raises "Can't use the ORM
    yield_per feature in conjunction with unique()"; ``autoflush=False`` stops
    the loader from flushing.
    """
    import sqlalchemy

    found = re.match(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?", sqlalchemy.__version__)
    if found is None:
        return False
    major, minor, patch = (int(part) for part in found.groups()[:3])
    if (major, minor) == (2, 0):
        return 20 <= patch < 50
    if (major, minor, patch) == (2, 1, 0) and found.group(4) is not None:
        return found.group(4) == "a" or (
            found.group(4) == "b" and int(found.group(5)) < 3
        )
    return False


def _only_own_listeners(state: Any) -> bool:
    """Whether every ``do_orm_execute`` listener of this execution is the plugin's."""
    todo = getattr(state, "_events_todo", None)
    if todo is None:
        return False
    return all(
        getattr(fn, "__module__", "").startswith("pytest_queryguard.") for fn in todo
    )


def _on_orm_execute_heal(state: Any) -> None:
    """Undo the load options a loader statement inherited only because of listeners.

    Registered only where ``_leaks_load_options()`` is true. With a listener,
    SQLAlchemy prepares the statement twice and hands the first result to the
    second pass. For a loader statement (``sa_top_level_orm_context``) that
    result holds the outer statement's options next to the loader's own load
    options from the first pass; an option whose value differs from those
    load options came from the outer statement. Removing it makes the second
    pass read the options the loader would have read without any listener;
    the outer statement's options are merged back for the cursor as before.

    Nothing happens when a listener that is not the plugin's takes part: that
    listener leaks the options without the plugin as well, and it may read or
    update them, so the plugin leaves SQLAlchemy's behaviour as it is.
    """
    try:
        options = state.local_execution_options
        if "sa_top_level_orm_context" not in options:
            return
        if not _only_own_listeners(state):
            return
        load = options.get("_sa_orm_load_options")
        leaked = {
            key
            for key in _LEAKED_LOAD_OPTIONS
            if key in options and getattr(load, "_" + key, _MISSING) != options[key]
        }
        if not leaked:
            return
        state.local_execution_options = options.__class__(
            {key: value for key, value in options.items() if key not in leaked}
        )
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _fail(exc, "do_orm_execute listener (leaked load options)")


def install() -> None:
    """Register the class-level listeners once per process."""
    global _installed
    if _installed:
        return
    event.listen(Session, "do_orm_execute", _on_orm_execute)
    if _leaks_load_options():
        event.listen(Session, "do_orm_execute", _on_orm_execute_heal)
    event.listen(Session, "after_begin", _on_after_begin)
    event.listen(Session, "after_flush", _on_after_flush)
    event.listen(Mapper, "load", _on_instance_load, raw=True)
    event.listen(Mapper, "refresh", _on_instance_refresh, raw=True)
    event.listen(Engine, "before_cursor_execute", _on_cursor_execute)
    event.listen(Engine, "after_cursor_execute", _on_timing_after)
    _installed = True


def install_index_check() -> None:
    """Register the missing-index listener once per process.

    Separate from install(), so a session without the check pays nothing for
    it on each statement.
    """
    global _index_installed
    if _index_installed:
        return
    event.listen(Engine, "after_cursor_execute", _on_after_cursor_execute)
    # The ends of transactions that ran DDL clear the catalog cache.
    event.listen(Engine, "commit", _on_commit)
    event.listen(Engine, "rollback", _on_rollback)
    event.listen(Engine, "commit_twophase", _on_commit_twophase)
    event.listen(Engine, "rollback_twophase", _on_rollback_twophase)
    event.listen(Engine, "rollback_savepoint", _on_rollback_savepoint)
    event.listen(Pool, "invalidate", _on_invalidate)
    _index_installed = True
