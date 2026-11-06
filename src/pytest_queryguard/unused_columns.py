"""Experimental: wide rows that a test loads and reads only a few columns of.

Nothing here runs unless ``queryguard_unused_columns`` is ``warn`` or
``fail``: ``enable()`` is the only thing that installs listeners or touches
SQLAlchemy, and ``disable()`` takes everything out again.

How it works (see docs/unused-columns.md):

* Which columns were loaded: the ``load`` and ``refresh`` instance events of
  a query's rows. The plugin's own listeners (listeners.py) have already
  noted the query's call site and load path on the instance; only instances
  of the query's own entities count (load path of length one), not those of
  relationship loads. The loaded columns are, for a new instance, the mapped
  column attributes in its ``__dict__``, worked out once per query and
  class; for an instance the Session had, those the ``refresh`` event says
  the query populated. The instance is noted by ``id()``, with its state,
  in its group: (class, file and function of the query).
* Which columns were read: ``InstrumentedAttribute.__get__`` is wrapped
  (``readhook``, shared with the unused eager-load check) while a test body
  is measured, and each read of a noted instance adds the attribute's name to
  its group. The state is compared on each read, so an ``id()`` that another
  object reuses after the instance is gone is not taken for it.

A group is reported when its class loaded at least ``MIN_LOADED`` columns and
at most ``MAX_READ_SHARE`` of them were read, counting the columns the ORM
needs (primary key, foreign keys and the other columns of relationships, the
discriminator, the version counter) and the columns of composites (read
without the descriptor) as read, and some attribute of some of
its instances was read at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Mapper

from . import blocks, collector, listeners, readhook
from ._compat import RUNID, QueryContext
from .blocks import IgnoreBlock
from .events import CallSite
from .findings import KIND_UNUSED_COLUMNS, Finding
from .listeners import _ORIGIN_ATTR
from .warnings import QueryGuardInternalWarning, warn_internal

# A finding needs a wide row: at least this many columns loaded...
MIN_LOADED = 8
# ...and at most this share of them read (the columns the ORM needs count).
MAX_READ_SHARE = 0.5
# The never-read columns the message names; the rest are counted.
_SHOWN = 12
# Instances noted per test body; past it, the groups that load more give no
# verdict (their reads would be missed).
MAX_INSTANCES = 100_000


@dataclass(slots=True, eq=False)
class Group:
    """The instances of one class that the queries of one function loaded."""

    owner: type
    mapper: Any
    callsite: CallSite
    loaded: set[str] = field(default_factory=set)
    reads: set[str] = field(default_factory=set)
    # some instances were not noted (MAX_INSTANCES): no verdict
    incomplete: bool = False
    # the ignore() blocks active at each query (see blocks.py), in order
    ignores: dict[tuple[IgnoreBlock, ...], None] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.owner.__name__


@dataclass(slots=True, eq=False)
class Tracker:
    """What one test body loaded and read."""

    groups: dict[tuple[type, str, str], Group] = field(default_factory=dict)
    # id(instance) -> (its state, the reads of its group): readhook.instances
    instances: dict[int, tuple[Any, set[str]]] = field(default_factory=dict)
    # the query whose rows are being turned into instances, and the group of
    # each class among them, so the work is done once per query and class
    run: Any = None
    run_groups: dict[type, Group] = field(default_factory=dict)
    # the classes whose new instances in this query were looked at
    run_fresh: set[type] = field(default_factory=set)
    previous: Tracker | None = None


class _Columns:
    """The column attributes of a mapper, and those the ORM itself needs."""

    __slots__ = ("keys", "needed", "primary")

    def __init__(self, mapper: Any) -> None:
        related: set[Any] = set()
        for rel in mapper.relationships:
            related.update(rel.local_columns)
        special = {mapper.polymorphic_on, mapper.version_id_col} - {None}
        # a composite reads its columns without the descriptor once it is
        # built: its reads cannot be seen, so its columns count as used
        for comp in mapper.composites:
            special.update(c for p in comp.props for c in p.columns)
        primary = set(mapper.primary_key)
        self.keys: tuple[str, ...] = tuple(p.key for p in mapper.column_attrs)
        self.primary = frozenset(
            p.key for p in mapper.column_attrs if any(c in primary for c in p.columns)
        )
        self.needed = frozenset(
            p.key
            for p in mapper.column_attrs
            if any(
                c in related
                or c in special
                or getattr(c, "primary_key", False)
                or getattr(c, "foreign_keys", None)
                for c in p.columns
            )
        )


_columns: dict[Any, _Columns] = {}
_MAX_MAPPERS = 1024

_current: Tracker | None = None
_enabled = 0
_broken = False
_warned = False


def _broke(exc: Exception, where: str) -> None:
    """Turn the feature off for the rest of the process and say so, once."""
    global _broken, _warned
    _broken = True
    readhook.instances = None
    readhook.instances_failed = None
    if not _warned:
        _warned = True
        warn_internal(
            QueryGuardInternalWarning(
                "pytest-queryguard disabled the unused-columns check after an "
                f"internal error in {where}: {exc!r}"
            )
        )


def _read_failed(exc: Exception) -> None:
    _broke(exc, "the attribute read hook")


def _columns_of(mapper: Any) -> _Columns:
    found = _columns.get(mapper)
    if found is None:
        if len(_columns) >= _MAX_MAPPERS:
            _columns.clear()
        found = _columns[mapper] = _Columns(mapper)
    return found


def _group(tracker: Tracker, state: Any, site: CallSite) -> Group:
    owner = state.class_
    key = (owner, site.filename, site.function)
    group = tracker.groups.get(key)
    if group is None:
        group = Group(owner, state.mapper, site)
        tracker.groups[key] = group
    group.ignores[blocks.current()] = None
    return group


def _note_loaded(group: Group, state: Any, attrs: Any) -> None:
    """Add the columns the query populated on ``state`` (``attrs``: all in
    ``__dict__`` when None) to the group."""
    keys = _columns_of(state.mapper).keys
    populated = state.dict if attrs is None else attrs
    group.loaded.update(k for k in keys if k in populated)


def _note(state: Any, context: Any, attrs: Any, fresh: bool) -> None:
    tracker = _current
    if tracker is None or _broken:
        return
    try:
        # Session.merge() fires it without a query context
        run = getattr(context, RUNID, None)
        if run is None:
            return
        # (call site, load path, generation), noted by the plugin's listener
        # of the same event just before this one
        origin = state.__dict__.get(_ORIGIN_ATTR)
        if origin is None or origin[2] != collector._generation:
            return  # not seen by the plugin in this measurement
        site, path = origin[0], origin[1]
        if site is None or path is None:
            return  # an ambiguous origin
        if len(path.path) != 1:
            return  # loaded through a relationship, not by the query's entities
        if tracker.run != run:
            tracker.run = run
            tracker.run_groups = {}
            tracker.run_fresh = set()
        owner = state.class_
        group = tracker.run_groups.get(owner)
        if group is None:
            group = tracker.run_groups[owner] = _group(tracker, state, site)
        if fresh:
            if owner not in tracker.run_fresh:
                # every new instance of the query has the same columns
                tracker.run_fresh.add(owner)
                _note_loaded(group, state, None)
        else:
            # it may hold columns set or loaded before: only what this query
            # populated counts
            _note_loaded(group, state, attrs)
        instances = tracker.instances
        if len(instances) >= MAX_INSTANCES:
            group.incomplete = True
            return
        instance = state.obj()
        if instance is not None:
            instances[id(instance)] = (state, group.reads)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _broke(exc, "the load listener")


def _on_load(state: Any, context: Any) -> None:
    _note(state, context, None, True)


def _on_refresh(state: Any, context: Any, attrs: Any) -> None:
    # a query's rows for instances the Session had (expired), not a refresh
    # of one instance (an expired attribute, session.refresh())
    if isinstance(context, QueryContext) and context.refresh_state is None:
        _note(state, context, attrs, False)


def enable() -> None:
    """Install the listeners and the read hook; calls nest, see ``disable()``."""
    global _enabled
    _enabled += 1
    if _enabled > 1:
        return
    # the plugin's listeners note the call site first; they are registered
    # once per process, so before these
    listeners.install()
    readhook.install()
    readhook.instances_failed = _read_failed
    event.listen(Mapper, "load", _on_load, raw=True)
    event.listen(Mapper, "refresh", _on_refresh, raw=True)


def disable() -> None:
    """Undo one ``enable()``; the last one restores SQLAlchemy as it was."""
    global _enabled, _current
    if _enabled == 0:
        return
    _enabled -= 1
    if _enabled > 0:
        return
    _current = None
    readhook.instances = None
    readhook.instances_failed = None
    readhook.uninstall()
    event.remove(Mapper, "load", _on_load)
    event.remove(Mapper, "refresh", _on_refresh)
    _columns.clear()


def is_enabled() -> bool:
    return _enabled > 0 and not _broken


def start() -> Tracker:
    """Begin tracking the test body; pair with ``stop()``."""
    global _current
    tracker = Tracker(previous=_current)
    _current = tracker
    _watch_reads()
    return tracker


def stop(tracker: Tracker) -> None:
    global _current
    _current = tracker.previous
    _watch_reads()


def _watch_reads() -> None:
    """Point the read hook at the instances of the test body measured now."""
    tracker = _current
    if tracker is not None and _enabled > 0 and not _broken:
        readhook.instances = tracker.instances
    else:
        readhook.instances = None


@dataclass(slots=True, frozen=True)
class Unused:
    """A group that read few of the columns it loaded."""

    group: Group
    loaded: tuple[str, ...]  # in mapper order
    unused: tuple[str, ...]
    keep: tuple[str, ...]  # for load_only(): read or needed, primary key aside


def unused(tracker: Tracker, sieve: blocks.Sieve | None = None) -> list[Unused]:
    """The groups that read at most ``MAX_READ_SHARE`` of a wide row.

    With a ``sieve``, one whose queries all ran inside ``ignore()`` blocks for
    the kind is left out.
    """
    found: list[Unused] = []
    for group in tracker.groups.values():
        if not group.reads or group.incomplete:
            continue  # nothing ever looked at these instances: cannot tell
        columns = _columns_of(group.mapper)
        loaded = tuple(k for k in columns.keys if k in group.loaded)
        if len(loaded) < MIN_LOADED:
            continue
        used = {k for k in loaded if k in group.reads or k in columns.needed}
        if len(used) > len(loaded) * MAX_READ_SHARE:
            continue
        if sieve is not None and sieve.drops_tags(group.ignores, KIND_UNUSED_COLUMNS):
            continue
        found.append(
            Unused(
                group,
                loaded,
                tuple(k for k in loaded if k not in used),
                tuple(k for k in loaded if k in used and k not in columns.primary),
            )
        )
    return found


def _load_only(item: Unused) -> str:
    owner = item.group.label
    keep = (
        item.keep
        or tuple(k for k in item.loaded if k in _columns_of(item.group.mapper).primary)[
            :1
        ]
    )
    return f"load_only({', '.join(f'{owner}.{k}' for k in keep)})"


def findings(
    tracker: Tracker, *, cwd: Path | None = None, sieve: blocks.Sieve | None = None
) -> list[Finding]:
    """One finding per class and query function that left most columns unread."""
    if _broken:
        return []
    try:
        from .messages import _display_path, _first_at, _stack, docs_line, kind_line

        result: list[Finding] = []
        for item in unused(tracker, sieve):
            group = item.group
            site = group.callsite
            stack = _stack(site, cwd)
            names = ", ".join(item.unused[:_SHOWN])
            if len(item.unused) > _SHOWN:
                names += f" and {len(item.unused) - _SHOWN} more"
            message = "\n".join(
                [
                    (
                        f"  {group.label}: {len(item.loaded)} columns loaded, "
                        f"{len(item.unused)} never read"
                    ),
                    *_first_at(site, stack, cwd),
                    kind_line(KIND_UNUSED_COLUMNS, group.label),
                    docs_line(KIND_UNUSED_COLUMNS),
                    f"  never read: {names}",
                    f"  fix: load only what is used, .options({_load_only(item)}),",
                    "  or defer() the large columns that are never read",
                    "  (experimental: if they are read later, or by code the plugin",
                    "  cannot see, ignore it with allow_nplusone(kind=KIND) or",
                    "  queryguard_ignore, KIND being unused-columns)",
                ]
            )
            result.append(
                Finding(
                    kind=KIND_UNUSED_COLUMNS,
                    label=group.label,
                    file=_display_path(site.filename, cwd),
                    function=site.function,
                    message=message,
                    line=site.lineno,
                    stack=stack,
                )
            )
        return result
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _broke(exc, "the analysis")
        return []
