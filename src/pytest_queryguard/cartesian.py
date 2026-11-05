"""Cartesian loads: one SELECT that joins two collections side by side.

``select(Customer).options(joinedload(Customer.orders),
joinedload(Customer.notes))`` is one query, so no count rule sees it, but each
customer comes back once per pair of an order and a note: 20 orders and 20
notes are 400 rows for one customer. EF Core warns about this ("multiple
collection includes"); this module does the same for SQLAlchemy.

The check reads the loader options of each top-level ORM SELECT, as the
unused eager-load check does. It flags a statement when two collections are
loaded with ``joinedload`` through joins only (no ``selectinload`` or
``subqueryload`` on the way, which would start a separate query) and neither
is on the path of the other: ``joinedload(A.bs).joinedload(B.cs)`` is a chain
whose rows grow with the number of ``cs``, not a product, and is left alone.
Loader strategies set on the mapping (``relationship(lazy="joined")``) are not
options of the query and are not seen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from . import collector
from .callsite import find_callsite
from .events import CartesianLoadEvent
from .findings import KIND_CARTESIAN_LOAD, Finding

_enabled = 0


_LOADERS = {
    "joined": "joinedload",
    "selectin": "selectinload",
    "subquery": "subqueryload",
    "immediate": "immediateload",
}

# A path of a loader option chain: (Class.relationship, loader) per step.
Path_ = tuple[tuple[str, str], ...]


def _collections(options: Any) -> tuple[str, tuple[Path_, ...]] | None:
    """(root class, joined collection paths) when two of them are side by side.

    The options split into statements where a step is not joined (a
    ``selectinload`` runs its own SELECT); two joined collections are side
    by side when they are in the same statement and neither is on the path
    of the other.
    """
    strategies: dict[tuple[Any, ...], str] = {}
    root = None
    for option in options:
        for element in getattr(option, "context", ()):
            strategy = getattr(element, "strategy", None)
            path = getattr(getattr(element, "path", None), "path", None)
            if not strategy or not path or len(path) < 3 or strategy[0][0] != "lazy":
                continue
            if "eager_from_alias" in getattr(element, "local_opts", {}):
                continue  # contains_eager(): the query joins on purpose
            strategies[tuple(path[1::2])] = str(strategy[0][1])
            if root is None:
                root = getattr(path[0], "class_", None)
    # joined collection paths by the statement that loads them: the prefix up
    # to the last step that is not joined
    statements: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
    for props, name in strategies.items():
        if name != "joined" or not props[-1].uselist:
            continue
        steps = [strategies.get(props[:i]) for i in range(1, len(props))]
        if any(step is None or step not in _LOADERS for step in steps):
            continue  # a step that is not loaded (lazyload, noload, raiseload)
        start = max(
            (i for i, step in enumerate(steps, 1) if step != "joined"), default=0
        )
        statements.setdefault(props[:start], []).append(props)
    side_by_side: set[tuple[Any, ...]] = set()
    for joined in statements.values():
        for a in joined:
            for b in joined:
                if a is not b and a[: len(b)] != b and b[: len(a)] != a:
                    side_by_side.add(a)
    if not side_by_side or root is None:
        return None
    paths = tuple(
        sorted(
            tuple(
                (
                    f"{p.parent.class_.__name__}.{p.key}",
                    _LOADERS[strategies[props[: i + 1]]],
                )
                for i, p in enumerate(props)
            )
            for props in side_by_side
        )
    )
    return root.__name__, paths


def _on_orm_execute(state: Any) -> None:
    active = collector.active()
    if not active:
        return
    try:
        # most statements have no loader options: one attribute read, then out
        options = getattr(state.statement, "_with_options", ())
        if not options:
            return
        if not state.is_select or state.is_relationship_load or state.is_column_load:
            return
        found = _collections(options)
        if found is None:
            return
        load = CartesianLoadEvent(found[1], found[0], find_callsite())
        for c in active:
            c.add_cartesian_load(load)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        from .listeners import record_error

        record_error(exc, "do_orm_execute listener (cartesian loads)")


def enable() -> None:
    """Install the listener; calls nest, see ``disable()``."""
    global _enabled
    _enabled += 1
    if _enabled == 1:
        event.listen(Session, "do_orm_execute", _on_orm_execute)


def disable() -> None:
    global _enabled
    if _enabled == 0:
        return
    _enabled -= 1
    if _enabled == 0:
        event.remove(Session, "do_orm_execute", _on_orm_execute)


def label(collections: tuple[Path_, ...]) -> str:
    """``Customer.notes + Customer.orders``: the last relationship of each path."""
    return " + ".join(path[-1][0] for path in collections)


def _option(path: Path_, last: str) -> str:
    steps = [f"{loader}({name})" for name, loader in path[:-1]]
    return ".".join([*steps, f"{last}({path[-1][0]})"])


def findings(c: collector.QueryCollector, *, cwd: Path | None = None) -> list[Finding]:
    """One finding per combination of collections, with the statements counted."""
    from .messages import _display_path, _location, kind_line

    grouped: dict[tuple[Path_, ...], list[CartesianLoadEvent]] = {}
    for load in c.cartesian_loads:
        grouped.setdefault(load.collections, []).append(load)
    result = []
    for collections, loads in grouped.items():
        first = loads[0]
        site = first.callsite
        where = "<unknown location>" if site is None else _location(site, cwd)
        names = [path[-1][0] for path in collections]
        times = "once" if len(loads) == 1 else f"{len(loads)} times"
        keep, *split = collections
        options = ", ".join(
            [_option(keep, "joinedload"), *(_option(p, "selectinload") for p in split)]
        )
        message = "\n".join(
            [
                f"  {' and '.join(names)} were joined into one query side by side, {times}",
                f"  first at {where}",
                kind_line(KIND_CARTESIAN_LOAD, label(collections)),
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
            )
        )
    return result
