"""Cartesian loads: one SELECT that joins two collections side by side.

``select(Customer).options(joinedload(Customer.orders),
joinedload(Customer.notes))`` is one query, so no count rule sees it, but each
customer comes back once per pair of an order and a note: 20 orders and 20
notes are 400 rows for one customer. EF Core warns about this ("multiple
collection includes"); this module does the same for SQLAlchemy.

The check reads the loader options of each top-level ORM SELECT when it runs,
with the parser of the unused eager-load check (``eager.loader_paths``). The
options split into statements wherever a step is not joined: a
``selectinload``, a ``subqueryload``, or a ``defaultload``/``lazyload`` of a
lazy relationship runs a SELECT of its own. Inside one statement, two joined
collections are a product when neither is on the path of the other; the finding
names the topmost collection of each of the two branches. ``joinedload(A.bs)
.joinedload(B.cs)`` is a chain whose rows grow with the number of ``cs``, not a
product, and is left alone.

Loader strategies set on the mapping (``relationship(lazy="joined")``) join
as an option would, so the relationships of each entity the SELECT loads are
followed the way SQLAlchemy does: a strategy of the mapping counts where no
option names the relationship and no wildcard (``lazyload("*")``) covers it,
through eager steps only (not into a lazy load that may never run), and not
into a mapper already on the path (a cycle) unless ``join_depth`` allows it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.sql import Select

from . import blocks, collector, listeners
from ._compat import (
    ANNOTATIONS,
    COMPILE_OPTIONS,
    ELEMENT_PATH,
    ENABLE_EAGERLOADS,
    LOAD_CONTEXT,
    PARENT_ENTITY,
    RAW_COLUMNS,
    RELATIONSHIP_TOKEN,
    WITH_OPTIONS,
)
from .blocks import IgnoreBlock
from .eager import LOADERS, loader_paths
from .events import CallSite, CartesianLoadEvent
from .findings import KIND_CARTESIAN_LOAD, Finding
from .warnings import QueryGuardInternalWarning, warn_internal

_enabled = 0
_broken = False

# A path of a loader option chain: (Class.relationship, loader) per step.
Path_ = tuple[tuple[str, str], ...]
# steps that load nothing: their children are never loaded either
_NOT_LOADED = frozenset(("noload", "raise", "raise_on_sql"))
_OPTION_NAMES = {**LOADERS, "select": "lazyload"}
# strategies that load with the query (or right after it): the strategies of
# the mapping below them run too, so they are followed
_EAGER = frozenset(LOADERS)
# deeper than this, a mapping's chain of eager loads is not followed
_MAX_DEPTH = 8
# the result for a statement without options, by its entities
_by_entities: dict[tuple[Any, ...], tuple[str, tuple[Path_, ...]] | None] = {}
_MAX_CACHED = 1024


def _effective(strategy: str | None, prop: Any) -> str:
    """The strategy a step really uses: defaultload() keeps the mapping's."""
    if strategy is not None:
        return strategy
    lazy = getattr(prop, "lazy", "select")
    return lazy if isinstance(lazy, str) else "select"


def _roots(statement: Any) -> tuple[Any, ...]:
    """The entities (Mapper or AliasedInsp) whose rows a SELECT loads.

    Columns of an entity (``select(A.id)``) load no instances and are left
    out, and so is everything when the query turns eager loads off.
    ``from_statement()`` is no ``Select``: SQLAlchemy adds no joins to it.
    """
    if not isinstance(statement, Select):
        return ()
    if not getattr(getattr(statement, COMPILE_OPTIONS, None), ENABLE_EAGERLOADS, True):
        return ()
    roots: list[Any] = []
    for column in getattr(statement, RAW_COLUMNS, ()):
        if not getattr(column, "is_selectable", False):
            continue
        entity = getattr(column, ANNOTATIONS, {}).get(PARENT_ENTITY)
        if entity is not None and entity not in roots:
            roots.append(entity)
    return tuple(roots)


def _wildcards(options: Any) -> tuple[bool, set[tuple[Any, ...]]]:
    """Where a relationship wildcard (``lazyload("*")``) sets the strategy.

    (whether one applies to the entities themselves, the keys of the
    relationships below whose own relationships one applies to)
    """
    top = False
    below: set[tuple[Any, ...]] = set()
    for option in options:
        elements = getattr(option, LOAD_CONTEXT, None)
        if elements is None:  # an unbound wildcard: its path is the token
            token = getattr(option, ELEMENT_PATH, None)
            if isinstance(token, tuple) and token and _is_token(token[-1]):
                top = True
            continue
        for element in elements:
            path: Any = getattr(getattr(element, ELEMENT_PATH, None), "path", ())
            if len(path) >= 2 and _is_token(path[-1]):
                below.add((path[0], *path[1:-1:2]))
    return top, below


def _is_token(step: Any) -> bool:
    return isinstance(step, str) and step.startswith(RELATIONSHIP_TOKEN)


def _mapping_defaults(
    root: Any,
    strategies: dict[tuple[Any, ...], tuple[str, str | None]],
    roots: dict[tuple[Any, ...], Any],
    wildcards: set[tuple[Any, ...]],
) -> None:
    """Add the relationships that the mapping loads eagerly below ``root``.

    Mirrors SQLAlchemy's loaders: an option or a wildcard wins over the
    mapping; a strategy of the mapping is followed only from an eager step,
    and stops at a mapper already on the path unless the relationship has a
    ``join_depth``, which then limits the depth.
    """
    stack: list[tuple[tuple[Any, ...], tuple[Any, ...]]] = [((root,), (root.mapper,))]
    while stack:
        key, mappers = stack.pop()
        if key in wildcards:
            continue
        for prop in mappers[-1].relationships:
            child = (*key, prop)
            if child in strategies:
                name = strategies[child][0]
            else:
                name = _effective(None, prop)
                if name not in _EAGER:
                    continue
                depth = len(child) - 1
                if prop.join_depth:
                    if depth > prop.join_depth:
                        continue
                elif any(m.isa(prop.mapper) for m in mappers):
                    continue
                strategies[child] = (name, None)
                roots[child] = root
            if name in _EAGER and len(child) <= _MAX_DEPTH:
                stack.append((child, (*mappers, prop.mapper)))


def _collections(
    options: Any, entities: tuple[Any, ...] = ()
) -> tuple[str, tuple[Path_, ...]] | None:
    """(root class, collection paths) of the first product among the options
    and, for each of ``entities``, the eager loads of the mapping."""
    strategies: dict[tuple[Any, ...], tuple[str, str | None]] = {}
    roots: dict[tuple[Any, ...], Any] = {}
    for path, strategy in loader_paths(options):
        key = (path[0], *path[1::2])
        strategies[key] = (_effective(strategy, path[-2]), strategy)
        roots[key] = path[0]
    if entities:
        top, below = _wildcards(options)
        if not top:
            for root in entities:
                _mapping_defaults(root, strategies, roots, below)
    # joined collections by the statement that loads them: the root mapper
    # and the prefix up to the last step that is not joined
    statements: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
    for key, (name, _) in strategies.items():
        props = key[1:]
        if name != "joined" or not props[-1].uselist:
            continue
        steps = [
            strategies.get(key[: i + 1], (_effective(None, props[i - 1]), None))[0]
            for i in range(1, len(props))
        ]
        if any(step in _NOT_LOADED for step in steps):
            continue
        start = max(
            (i for i, step in enumerate(steps, 1) if step != "joined"), default=0
        )
        statements.setdefault((key[0], *props[:start]), []).append(key)
    for keys in statements.values():
        for i, a in enumerate(keys):
            for b in keys[i + 1 :]:
                pair = _branches(a, b)
                if pair is not None:
                    paths = tuple(sorted(_path(k, strategies) for k in pair))
                    return roots[a].class_.__name__, paths
    return None


def _branches(a: tuple[Any, ...], b: tuple[Any, ...]) -> tuple[Any, Any] | None:
    """The topmost collection of each branch where ``a`` and ``b`` part, if any."""
    common = 0
    while common < min(len(a), len(b)) and a[common] is b[common]:
        common += 1
    if common in (len(a), len(b)):
        return None  # one is on the path of the other: a chain
    tops = []
    for key in (a, b):
        start = max(common, 1)  # key[0] is the root mapper
        top = next(key[: i + 1] for i in range(start, len(key)) if key[i].uselist)
        tops.append(top)
    return tops[0], tops[1]


def _path(key: tuple[Any, ...], strategies: dict[Any, tuple[str, str | None]]) -> Path_:
    steps = []
    for i in range(1, len(key)):
        prop = key[i]
        given = strategies.get(key[: i + 1], ("select", None))[1]
        loader = "defaultload" if given is None else _OPTION_NAMES.get(given, given)
        steps.append((f"{prop.parent.class_.__name__}.{prop.key}", loader))
    return tuple(steps)


def _check(
    statement: Any,
    site: CallSite | None,
    ignores: tuple[IgnoreBlock, ...],
    active: tuple[collector.QueryCollector, ...],
) -> None:
    """Read the loader options of a top-level ORM SELECT as it runs.

    Called by the plugin's cursor listener (``listeners.cartesian_check``)
    for an ORM query that is no relationship or column load (those carry
    the outer query's options, not where the user wrote them), with the
    statement's call site. Not a ``do_orm_execute`` listener: any listener
    of that event makes SQLAlchemy prepare every ORM statement twice.
    """
    if _broken:
        return
    try:
        if not getattr(statement, "is_select", False):
            return
        options = getattr(statement, WITH_OPTIONS, ())
        entities = _roots(statement)
        if options:
            found = _collections(options, entities)
        elif not entities:
            return
        else:
            # most statements have no loader options: what the mapping
            # loads depends on the entities only, worked out once
            try:
                found = _by_entities[entities]
            except KeyError:
                if len(_by_entities) >= _MAX_CACHED:
                    _by_entities.clear()
                found = _by_entities[entities] = _collections((), entities)
        if found is None:
            return
        load = CartesianLoadEvent(found[1], found[0], site, ignores)
        for c in active:
            c.add_cartesian_load(load)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        _broke(exc)


def _broke(exc: Exception) -> None:
    """Turn this check off for the rest of the process, once, and say so.

    The other checks go on: a loader option the parser does not know must not
    stop the measurement of the whole suite.
    """
    global _broken
    if not _broken:
        _broken = True
        warn_internal(
            QueryGuardInternalWarning(
                f"pytest-queryguard disabled the cartesian-load check after an "
                f"internal error: {exc!r}"
            )
        )


def enable() -> None:
    """Turn the check on in the plugin's cursor listener; calls nest, see
    ``disable()``."""
    global _enabled
    _enabled += 1
    if _enabled == 1:
        listeners.cartesian_check = _check


def disable() -> None:
    global _enabled
    if _enabled == 0:
        return
    _enabled -= 1
    if _enabled == 0:
        listeners.cartesian_check = None
        _by_entities.clear()


def label(collections: tuple[Path_, ...]) -> str:
    """``Customer.notes + Customer.orders``: the last relationship of each path."""
    return " + ".join(path[-1][0] for path in collections)


def _option(path: Path_, last: str) -> str:
    steps = [f"{loader}({name})" for name, loader in path[:-1]]
    return ".".join([*steps, f"{last}({path[-1][0]})"])


def findings(
    c: collector.QueryCollector,
    *,
    cwd: Path | None = None,
    sieve: blocks.Sieve | None = None,
) -> list[Finding]:
    """One finding per combination of collections, with the statements counted.

    With a ``sieve``, one whose statements all ran inside ``ignore()`` blocks
    for the kind is left out.
    """
    from .messages import _display_path, _first_at, _stack, docs_line, kind_line

    if c.truncated:
        sieve = None  # unstored events may have run outside the blocks

    grouped: dict[tuple[Path_, ...], list[CartesianLoadEvent]] = {}
    for load in c.cartesian_loads:
        grouped.setdefault(load.collections, []).append(load)
    result = []
    for collections, loads in grouped.items():
        if sieve is not None and sieve.drops(loads, KIND_CARTESIAN_LOAD):
            continue
        first = loads[0]
        site = first.callsite
        stack = _stack(site, cwd)
        names = [path[-1][0] for path in collections]
        times = "once" if len(loads) == 1 else f"{len(loads)} times"
        keep, *split = collections
        options = ", ".join(
            [_option(keep, "joinedload"), *(_option(p, "selectinload") for p in split)]
        )
        message = "\n".join(
            [
                f"  {' and '.join(names)} were joined into one query side by side, {times}",
                *_first_at(site, stack, cwd),
                kind_line(KIND_CARTESIAN_LOAD, label(collections)),
                docs_line(KIND_CARTESIAN_LOAD),
                "  each row repeats per pair of their items: the rows grow as "
                "their product"
                if len(collections) == 2
                else "  each row repeats per combination of their items",
                (
                    "  fix: load all but one of them with selectinload (one more "
                    "query each, no repeated rows), e.g."
                ),
                f"       select({first.root}).options({options})",
            ]
        )
        result.append(
            Finding(
                kind=KIND_CARTESIAN_LOAD,
                label=label(collections),
                file=None if site is None else _display_path(site.filename, cwd),
                function=None if site is None else site.function,
                message=message,
                line=None if site is None else site.lineno,
                count=len(loads),
                stack=stack,
            )
        )
    return result
