"""Experimental: relationships that a test eager-loads and never reads.

Nothing here runs unless ``queryguard_unused_eager_load`` is ``warn`` or
``fail``: ``enable()`` is the only thing that installs listeners or touches
SQLAlchemy, and ``disable()`` takes everything out again.

How it works, and why this way (see docs/unused-eager-load.md):

* Which relationships were eager-loaded: a ``do_orm_execute`` listener reads
  the loader options of each top-level ORM ``SELECT`` (``selectinload()``,
  ``joinedload()``, ``subqueryload()``, ``immediateload()``) and notes the
  relationship and the call site of the query. Loader strategies configured on
  the mapping (``relationship(lazy="selectin")``) are not options of a query
  and are not tracked.
* Which relationships were read: a plain read of a loaded attribute is a
  dictionary lookup that fires no event, so while the feature is on,
  ``InstrumentedAttribute.__get__`` is wrapped to record ``(class, name)``
  of every read made inside the measured test body. Reads made by the unit of
  work (flush, cascades, backrefs) go through the attribute implementation
  and do not pass here; deleting an instance exempts the relationships that
  cascade the delete, which SQLAlchemy loads for that purpose.

A finding needs three things: the relationship was eager-loaded in the test
body, no instance of the class had that relationship read, and some instance
of the class had another attribute read (so the code under test does use its
instances through attribute access, which rules out code that reads
``__dict__`` directly, and queries whose rows were never looked at).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import InstrumentedAttribute, RelationshipProperty, Session

from .callsite import find_callsite
from .events import CallSite
from .findings import KIND_UNUSED_EAGER_LOAD, Finding
from .warnings import QueryGuardInternalWarning, warn_internal

LOADERS = {
    "joined": "joinedload",
    "selectin": "selectinload",
    "subquery": "subqueryload",
    "immediate": "immediateload",
}


@dataclass(slots=True, eq=False)
class EagerLoad:
    """One relationship a query asked to load eagerly."""

    owner: type
    prop: Any  # the RelationshipProperty
    loader: str  # "selectinload", ...
    callsite: CallSite | None
    # the relationship above this one in the option chain, as (class, name)
    parent: tuple[type, str] | None = None

    @property
    def key(self) -> str:
        return str(self.prop.key)

    @property
    def label(self) -> str:
        return f"{self.owner.__name__}.{self.key}"


@dataclass(slots=True, eq=False)
class Tracker:
    """What one test body eager-loaded, read and deleted."""

    loads: list[EagerLoad] = field(default_factory=list)
    reads: set[tuple[type, str]] = field(default_factory=set)
    deleted: set[type] = field(default_factory=set)
    previous: Tracker | None = None


_current: Tracker | None = None
_enabled = 0
_original: Any = None
_broken = False
_warned = False


def _tracked_get(self: Any, instance: Any, owner: Any) -> Any:
    tracker = _current
    if tracker is not None and instance is not None and not _broken:
        try:
            tracker.reads.add((type(instance), self.key))
        except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
            _broke(exc, "the attribute read hook")
    return _original(self, instance, owner)


def _broke(exc: Exception, where: str) -> None:
    """Turn the feature off for the rest of the process and say so, once."""
    global _broken, _warned
    _broken = True
    if not _warned:
        _warned = True
        warn_internal(
            QueryGuardInternalWarning(
                "pytest-queryguard disabled the unused eager-load check after an "
                f"internal error in {where}: {exc!r}"
            )
        )


def loader_paths(options: Any) -> Iterator[tuple[tuple[Any, ...], str | None]]:
    """(path, strategy) of every relationship that loader options name.

    ``path`` alternates mappers and relationships, from the query's entity;
    ``strategy`` is the lazy= name (``"joined"``, ``"selectin"``, ...) or None
    for ``defaultload()``. Wildcards (``joinedload("*")``) and
    ``contains_eager()`` (a join made on purpose, to filter) are left out.
    Shared by the unused eager-load and the cartesian-load checks.
    """
    for option in options:
        for element in getattr(option, "context", ()):
            path = getattr(getattr(element, "path", None), "path", None)
            if not path or len(path) < 3 or len(path) % 2 == 0:
                continue
            if not all(isinstance(p, RelationshipProperty) for p in path[1::2]):
                continue
            if "eager_from_alias" in getattr(element, "local_opts", {}):
                continue
            strategy = getattr(element, "strategy", None)
            if not strategy:
                yield tuple(path), None
            elif strategy[0][0] == "lazy":
                yield tuple(path), str(strategy[0][1])


def _elements(options: Any) -> list[EagerLoad]:
    """The eager loads the loader options of a statement ask for."""
    found: list[EagerLoad] = []
    for path, strategy in loader_paths(options):
        loader = LOADERS.get(strategy) if strategy is not None else None
        if loader is None:
            continue
        prop = path[-2]
        parent = None
        if len(path) >= 5:
            above = path[-4]
            parent = (above.parent.class_, above.key)
        found.append(EagerLoad(prop.parent.class_, prop, loader, None, parent))
    return found


def _on_orm_execute(state: Any) -> None:
    tracker = _current
    if tracker is None or _broken:
        return
    try:
        if not state.is_select or state.is_relationship_load or state.is_column_load:
            return
        loads = _elements(getattr(state.statement, "_with_options", ()))
        if not loads:
            return
        site = find_callsite()
        for load in loads:
            load.callsite = site
            tracker.loads.append(load)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _broke(exc, "the do_orm_execute listener")


def _on_before_flush(session: Any, flush_context: Any, instances: Any) -> None:
    tracker = _current
    if tracker is None or _broken:
        return
    try:
        tracker.deleted.update(type(obj) for obj in session.deleted)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _broke(exc, "the before_flush listener")


def enable() -> None:
    """Install the listeners and the read hook; calls nest, see ``disable()``."""
    global _enabled, _original
    _enabled += 1
    if _enabled > 1:
        return
    _original = InstrumentedAttribute.__get__
    InstrumentedAttribute.__get__ = _tracked_get  # type: ignore[method-assign]
    event.listen(Session, "do_orm_execute", _on_orm_execute)
    event.listen(Session, "before_flush", _on_before_flush)


def disable() -> None:
    """Undo one ``enable()``; the last one restores SQLAlchemy as it was."""
    global _enabled, _current
    if _enabled == 0:
        return
    _enabled -= 1
    if _enabled > 0:
        return
    _current = None
    InstrumentedAttribute.__get__ = _original  # type: ignore[method-assign]
    event.remove(Session, "do_orm_execute", _on_orm_execute)
    event.remove(Session, "before_flush", _on_before_flush)


def is_enabled() -> bool:
    return _enabled > 0 and not _broken


def start() -> Tracker:
    """Begin tracking the test body; pair with ``stop()``."""
    global _current
    tracker = Tracker(previous=_current)
    _current = tracker
    return tracker


def stop(tracker: Tracker) -> None:
    global _current
    _current = tracker.previous


def _related(a: type, b: type) -> bool:
    return issubclass(a, b) or issubclass(b, a)


def _was_read(tracker: Tracker, owner: type, key: str) -> bool:
    return any(k == key and _related(t, owner) for t, k in tracker.reads)


def _deleted_with_cascade(tracker: Tracker, load: EagerLoad) -> bool:
    cascade = getattr(load.prop, "cascade", None)
    if not (
        getattr(cascade, "delete", False) or getattr(cascade, "delete_orphan", False)
    ):
        return False
    return any(_related(t, load.owner) for t in tracker.deleted)


def unused(tracker: Tracker) -> list[EagerLoad]:
    """The eager loads (first of each relationship and call site) nothing read."""
    found: list[EagerLoad] = []
    seen: set[tuple[str, str, object]] = set()
    touched = {t for t, _ in tracker.reads}
    for load in tracker.loads:
        site = load.callsite
        ident = (
            load.label,
            load.loader,
            None if site is None else (site.filename, site.function),
        )
        if ident in seen:
            continue
        seen.add(ident)
        if _was_read(tracker, load.owner, load.key):
            continue
        if not any(_related(t, load.owner) for t in touched):
            continue  # nothing ever looked at these instances: cannot tell
        if _deleted_with_cascade(tracker, load):
            continue
        if load.parent is not None and not _was_read(tracker, *load.parent):
            continue  # the relationship above is the finding
        found.append(load)
    return found


def _display(site: CallSite | None, cwd: Path | None) -> tuple[str | None, str]:
    from .messages import _display_path, _location

    if site is None:
        return None, "  first at <unknown location>"
    return _display_path(site.filename, cwd), f"  first at {_location(site, cwd)}"


def findings(tracker: Tracker, *, cwd: Path | None = None) -> list[Finding]:
    """One finding per eager load the test body never read; never raises."""
    if _broken:
        return []
    try:
        from .messages import kind_line

        result: list[Finding] = []
        for load in unused(tracker):
            site = load.callsite
            file, where = _display(site, cwd)
            option = f"{load.loader}({load.label})"
            message = "\n".join(
                [
                    f"  {load.label} was loaded with {load.loader} and never read",
                    where,
                    kind_line(KIND_UNUSED_EAGER_LOAD, load.label),
                    f"  fix: remove {option} from the query, or load it only where used",
                    "  (experimental: if it is read later, or by code the plugin",
                    "  cannot see, ignore it with allow_nplusone(kind=KIND) or",
                    "  queryguard_ignore, KIND being unused-eager-load)",
                ]
            )
            result.append(
                Finding(
                    kind=KIND_UNUSED_EAGER_LOAD,
                    label=load.label,
                    file=file,
                    function=None if site is None else site.function,
                    message=message,
                    line=None if site is None else site.lineno,
                )
            )
        return result
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _broke(exc, "the analysis")
        return []
