from __future__ import annotations

import contextlib
import itertools
import os
import re
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapper, ORMExecuteState, Session, loading
from sqlalchemy.orm.attributes import instance_state
from sqlalchemy.orm.context import QueryContext
from sqlalchemy.pool import Pool

from . import blocks, catalog, collector, indexes, mysql_index, sqlite_index, sqltext
from .blocks import IgnoreBlock
from .callsite import Sites, find_callsite, find_flush
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
        # Without the tag the connection counts as a scope of its own.
        with contextlib.suppress(AttributeError, TypeError):
            setattr(connection, _SCOPE_ATTR, scope_of_session(session))
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _fail(exc, "after_begin listener (session scopes)")


def _is_refresh_eager_load(execution_options: Any) -> bool:
    """Whether a load with a parent is a mapped eager loader run by a refresh.

    ``session.refresh(obj)`` (and the reload of expired attributes) of an
    object with a ``lazy="selectin"`` or ``lazy="subquery"`` relationship loads
    the collection through the immediate loader, one parent per statement: the
    statement carries a parent state, like a lazy load, but it is the mapped
    eager strategy at work and ``selectinload`` would change nothing.

    The immediate loader hands its statement the outer query's context as the
    private execution option ``sa_top_level_orm_context`` (read here and in
    ``orm_select`` only; the same from SQLAlchemy 2.0.13 to 2.1); for a
    refresh its ``refresh_state`` is set. An ``immediateload()`` of a plain
    query has none and stays a lazy load, and a lazy load that user code (a
    reconstructor, a ``load`` event) runs while the loader builds the
    children has no such option at all. Without the option, on a SQLAlchemy
    that dropped it, such loads count as lazy loads: a possible false
    positive, never a missed lazy load.
    """
    top = execution_options.get("sa_top_level_orm_context")
    return getattr(top, "refresh_state", None) is not None


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


def _reset_for_tests() -> None:
    global _error, _failure
    _error = None
    _failure = None


class OrmSelect(NamedTuple):
    """What the ORM keeps on one SELECT it runs; see ``orm_select``."""

    # ``_sa_orm_load_options``; None for a query that sets none of them
    load_options: Any
    # the statement's ``_compile_options``
    compile_options: Any
    # the execution options of the cursor execution
    execution_options: Any


_new_tuple = tuple.__new__


def orm_select(context: Any) -> OrmSelect | None:
    """The options of the ORM query a cursor execution runs.

    None for any other statement: Core, text, DDL. The ORM keeps the load
    options of a SELECT in the execution options (``_sa_orm_load_options``:
    ``_lazy_loaded_from``, ``_refresh_state``, ``_is_user_refresh``) and its
    compile options on the statement (``_current_path``,
    ``_for_refresh_state``, ``_only_load_props``); ``ORMExecuteState`` reads
    the same. They are private to SQLAlchemy, so the listeners read them only
    through this function and the two that read the execution options
    (``_loader_session``, ``_is_refresh_eager_load``), unchanged from 2.0.13
    to 2.1. Unlike the state of a ``do_orm_execute`` listener, they belong to
    this one execution: no other statement (an autoflush, a begin listener,
    another Session or task on the connection) can take them.
    """
    statement = getattr(context, "invoked_statement", None)
    if not _is_orm(statement):
        return None
    options = getattr(context, "execution_options", None) or {}
    # tuple.__new__ directly: half the cost of OrmSelect(...), per ORM SELECT
    return _new_tuple(
        OrmSelect,
        (
            options.get("_sa_orm_load_options"),
            getattr(statement, "_compile_options", None),
            options,
        ),
    )


def _loader_session(execution_options: Any) -> Any:
    """The Session of the query a selectin or immediate loader runs for.

    The loader passes the outer query's context as the private execution
    option ``sa_top_level_orm_context`` (part of ``orm_select``'s reading);
    None without it (a subquery loader).
    """
    top = execution_options.get("sa_top_level_orm_context")
    return getattr(top, "session", None)


def column_load_of(
    load_options: Any,
    options: Any,
    session: Any,
    ignores: tuple[IgnoreBlock, ...] = (),
    sites: Sites | None = None,
) -> tuple[Any, ColumnLoadEvent] | None:
    """The instance state a column load reloads, and its event; None without one.

    ``load_options`` and ``options`` are the load and compile options of the
    load's SELECT (``orm_select``); ``session`` is the Session it runs in,
    ``ignores`` the ``ignore()`` blocks active there and ``sites`` the call
    site cache to use (the in-app unit's own). It counts nothing.
    """
    instance = getattr(load_options, "_refresh_state", None)
    if instance is None:
        return None
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
        find_callsite(sites),
        scope=scope_of_session(session),
        ignores=ignores,
    )
    return instance, load


class _Labels(NamedTuple):
    """What a statement is for, from ``_orm_load``."""

    relationship: str | None
    column_load: ColumnLoadEvent | None
    lazy: bool
    # the call site found for the load, reused for the statement
    site: CallSite | None


# A statement that is no load (or one that cannot be classified).
_NO_LABELS = _Labels(None, None, False, None)


def _relationship_load(
    prop: Any,
    parent: Any,
    session: Any,
    conn: Any,
    ignores: tuple[IgnoreBlock, ...],
    sites: Sites | None = None,
    parents: bool = True,
) -> RelationshipLoadEvent:
    """The event of a relationship load; ``parent`` is None for a loader.

    ``parents`` False leaves out where the parent came from (``_parent_load``):
    the in-app mode does not note the origins of instances.
    """
    key: object = None
    if parent is not None:
        key = parent.identity_key
        if key is None:
            key = id(parent)
    # Without a Session (a subquery loader) the connection carries the scope
    # of the Session that began on it last.
    scope = scope_of_connection(conn) if session is None else scope_of_session(session)
    return RelationshipLoadEvent(
        str(prop),
        key,
        find_callsite(sites),
        getattr(prop, "uselist", None) is False,
        scope,
        None if parent is None or not parents else _parent_load(parent, prop),
        ignores,
    )


def _plugin_written(session: Any) -> _Written | None:
    """The plugin's record of the Session's latest flush (``_on_after_flush``)."""
    return getattr(session, "__dict__", {}).get(_WRITTEN_ATTR)


def _orm_load(
    conn: Any,
    orm: OrmSelect,
    targets: tuple[collector.QueryCollector, ...],
    ignores: tuple[IgnoreBlock, ...],
    sites: Sites | None = None,
    written: Callable[[Any], _Written | None] = _plugin_written,
    parents: bool = True,
) -> _Labels | None:
    """Record the relationship or column load an ORM SELECT runs for.

    ``targets`` are the collectors that want loads (not the statements-only
    ones of the queryguard fixture), ``ignores`` the active ``ignore()``
    blocks, which every event carries. Returns the statement's labels, or
    None for a top-level query (no relationship or column load). Strict mode
    decides in ``_on_orm_execute_strict``, before the SELECT; never here.

    The in-app mode calls it as well, with its unit's call site cache
    (``sites``), its own record of the latest flush (``written``, called with
    the Session) and without parent sites (``parents``), so both label a
    load the same way.
    """
    load_options = orm.load_options
    options = orm.compile_options
    if getattr(options, "_for_refresh_state", False):
        instance = getattr(load_options, "_refresh_state", None)
        session = getattr(instance, "session", None)
        found = column_load_of(load_options, options, session, ignores, sites)
        if found is None:
            return _NO_LABELS
        instance, column = found
        if column_load_counts(column, instance, written(session)):
            for target in targets:
                target.add_column_load(column)
        return _Labels(None, column, False, column.callsite)
    path = getattr(options, "_current_path", None)
    if path is None or path.is_root:
        return None
    prop = path[-1]
    parent = getattr(load_options, "_lazy_loaded_from", None)
    if parent is not None and _is_refresh_eager_load(orm.execution_options):
        # An eager loader's query: label the statement, count no lazy load.
        return _Labels(str(prop), None, False, None)
    if parent is not None:
        session = parent.session
    else:
        session = _loader_session(orm.execution_options)
    load = _relationship_load(prop, parent, session, conn, ignores, sites, parents)
    for target in targets:
        target.add_relationship_load(load)
    return _Labels(load.relationship, None, parent is not None, load.callsite)


# The cartesian-load check (cartesian.py) while it is enabled: called with the
# invoked statement, its call site, the ignore() blocks and the collectors
# that want loads, for each top-level ORM SELECT. Set by cartesian.enable().
cartesian_check: Callable[..., None] | None = None

_is_write = sqltext.is_write


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
        write = _is_write(statement)
        ignores = blocks.current()
        # Loads only for the collectors that want them: with only the
        # statements-only ones of the queryguard fixture, no load work.
        full = collector.full_active()
        orm = None if write or not full else orm_select(context)
        labels = None if orm is None else _orm_load(conn, orm, full, ignores)
        stmt = _record_statement(
            conn,
            statement,
            parameters,
            context,
            executemany,
            write,
            orm,
            targets,
            ignores,
            _NO_LABELS if labels is None else labels,
        )
        check = cartesian_check
        if check is not None and orm is not None and labels is None:
            check(context.invoked_statement, stmt.callsite, ignores, full)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _fail(exc, "before_cursor_execute listener (statements)")


def _record_statement(
    conn: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
    write: bool,
    orm: OrmSelect | None,
    targets: tuple[collector.QueryCollector, ...],
    ignores: tuple[IgnoreBlock, ...],
    labels: _Labels,
) -> StatementEvent:
    column_load = labels.column_load
    if column_load is not None:
        column_load.sql = statement
    site = labels.site
    stmt = StatementEvent(
        statement,
        site if site is not None else find_callsite(),
        labels.relationship,
        column_load=column_load is not None,
        lazy_load=labels.lazy,
        parameters=None if executemany else parameters,
        executemany=bool(executemany),
        flush=find_flush() if write else None,
        scope=scope_of_connection(conn),
        ignores=ignores,
    )
    for target in targets:
        target.add_statement(stmt)
    if context is not None:
        if orm is not None:  # only with collectors that want loads
            # Read back when the ORM turns the rows into instances.
            setattr(context, _SITE_ATTR, stmt.callsite)
            if labels.relationship is None:
                _note_read(context.invoked_statement, stmt.callsite)
        # Read back by the after listener; per execution, so statements
        # that overlap in time (asyncio tasks) never mix up. Taken last,
        # so the time spent finding the call site is not counted.
        context._queryguard_timing = (stmt, targets, time.perf_counter())
    return stmt


def _on_orm_execute_strict(state: ORMExecuteState) -> None:
    """Strict mode: raise at the lazy load that makes the N+1, before its SELECT.

    Registered only while a session runs with ``queryguard_strict``
    (``enable_strict``), so the default run keeps no ``do_orm_execute``
    listener. Raising here, before the ORM autoflushes and takes a
    connection, keeps the error out of the engine: no ``handle_error``
    listener sees it, no cursor is opened, nothing is flushed. Only the
    strict decision is made here; the cursor listener records the load when
    its SELECT runs. A load that raises has no SELECT, so it is recorded here.
    """
    if _error is not None:
        return
    strict: BaseException | None = None
    try:
        full = collector.full_active()
        if not any(target.strict is not None for target in full):
            return
        if not state.is_relationship_load:
            return
        parent = state.lazy_loaded_from
        if parent is None or _is_refresh_eager_load(state.execution_options):
            return  # only lazy loads raise
        prop = state.loader_strategy_path[-1]  # type: ignore[index]
        load = _relationship_load(prop, parent, state.session, None, blocks.current())
        for target in full:
            if target.strict is not None:
                # every strict collector counts the load; the first error wins
                # (strict_check skips a load its ignore() blocks cover)
                error = target.strict_check(load)
                if strict is None:
                    strict = error
        if strict is not None:
            for target in full:
                target.add_relationship_load(load)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _fail(exc, "do_orm_execute listener (strict mode)")
        return
    if strict is not None:
        # queryguard_strict: the N+1 fails here, at the attribute access, with
        # the test's own traceback (outside the try: this is meant to raise)
        raise strict


_strict_users = 0


def enable_strict() -> None:
    """Install the strict-mode listener; calls nest, see ``disable_strict()``."""
    global _strict_users
    _strict_users += 1
    if _strict_users == 1:
        event.listen(Session, "do_orm_execute", _on_orm_execute_strict)
        use_heal()


def disable_strict() -> None:
    global _strict_users
    if _strict_users == 0:
        return
    _strict_users -= 1
    if _strict_users == 0:
        event.remove(Session, "do_orm_execute", _on_orm_execute_strict)
        drop_heal()


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

    Registered with the plugin's ``do_orm_execute`` listeners, those of the
    unused eager-load check (``eager.py``) and of strict mode
    (``enable_strict``), through ``use_heal()``, and only where
    ``_leaks_load_options()`` is true. The in-app mode registers no
    ``do_orm_execute`` listener. With a listener, SQLAlchemy prepares the
    statement twice and hands the first result to the second pass. For a loader statement (``sa_top_level_orm_context``) that
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


_heal_users = 0


def use_heal() -> None:
    """Register ``_on_orm_execute_heal`` while a plugin listener of the event
    is on, where this SQLAlchemy needs it; calls nest, see ``drop_heal()``."""
    global _heal_users
    _heal_users += 1
    if _heal_users == 1 and _leaks_load_options():
        event.listen(Session, "do_orm_execute", _on_orm_execute_heal)


def drop_heal() -> None:
    global _heal_users
    if _heal_users == 0:
        return
    _heal_users -= 1
    if _heal_users == 0 and event.contains(
        Session, "do_orm_execute", _on_orm_execute_heal
    ):
        event.remove(Session, "do_orm_execute", _on_orm_execute_heal)


def install() -> None:
    """Register the class-level listeners once per process."""
    global _installed
    if _installed:
        return
    # No do_orm_execute listener: the cursor listener reads the ORM's load
    # options from the SELECT itself (orm_select). Any such listener makes
    # SQLAlchemy prepare each ORM statement twice, and on some releases leak
    # load options (see _on_orm_execute_heal). Only strict mode
    # (enable_strict) and the unused eager-load check add one.
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
