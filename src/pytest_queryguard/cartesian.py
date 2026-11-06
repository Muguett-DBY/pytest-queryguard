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
product, and is left alone. Loader strategies set on the mapping
(``relationship(lazy="joined")``) are not options of the query and are not
seen, except on a ``defaultload()`` step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import blocks, collector, listeners
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


def _effective(strategy: str | None, prop: Any) -> str:
    """The strategy a step really uses: defaultload() keeps the mapping's."""
    if strategy is not None:
        return strategy
    lazy = getattr(prop, "lazy", "select")
    return lazy if isinstance(lazy, str) else "select"


def _collections(options: Any) -> tuple[str, tuple[Path_, ...]] | None:
    """(root class, collection paths) of the first product among the options."""
    strategies: dict[tuple[Any, ...], tuple[str, str | None]] = {}
    roots: dict[tuple[Any, ...], Any] = {}
    for path, strategy in loader_paths(options):
        key = (path[0], *path[1::2])
        strategies[key] = (_effective(strategy, path[-2]), strategy)
        roots[key] = path[0]
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
        # most statements have no loader options: one attribute read, then out
        options = getattr(statement, "_with_options", ())
        if not options or not getattr(statement, "is_select", False):
            return
        found = _collections(options)
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
