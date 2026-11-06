from __future__ import annotations

import functools
import itertools
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from .blocks import IgnoreBlock, Sieve, covering
from .collector import QueryCollector
from .events import (
    CAUSE_DEFERRED,
    CallSite,
    ColumnLoadEvent,
    RelationshipLoadEvent,
    StatementEvent,
)
from .findings import (
    KIND_COLUMN_LOAD,
    KIND_RELATIONSHIP,
    KIND_REPEAT,
    KIND_WRITE_LOOP,
)
from .sqltext import is_transaction_control, is_write, skip_prefix

_STRING = re.compile(r"'(?:[^']|'')*'")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")
_PLACEHOLDER = r"""\?|%s|%\(\w+\)s|:\w+|\$\d+|\d+(?:\.\d+)?|'(?:[^']|'')*'"""
# A type name as drivers render it: INTEGER, DOUBLE PRECISION, VARCHAR(20)[],
# TIMESTAMP(3) WITH TIME ZONE, public.mood, "Sales"."Mood".
_NAME = r'(?:"(?:[^"]|"")+"|[A-Za-z_]\w*)'
_WORD = r"\s+[A-Za-z_]\w*"
_TYPE = (
    rf"{_NAME}(?:\s*\.\s*{_NAME})*(?:{_WORD})*"
    rf"(?:\s*\(\s*\d+(?:\s*,\s*\d+)*\s*\)(?:{_WORD})*)?"
    r"(?:\s*\[\s*\d*\s*\])*"
)
# PostgreSQL drivers may cast each bound value: $1::INTEGER or CAST(? AS INTEGER).
_ITEM = (
    rf"(?:CAST\s*\(\s*(?:{_PLACEHOLDER})\s+AS\s+{_TYPE}\s*\)"
    rf"|(?:{_PLACEHOLDER})(?:\s*::\s*{_TYPE})?)"
)
_IN_LIST = re.compile(
    rf"\bIN\s*\(\s*{_ITEM}(?:\s*,\s*{_ITEM})*\s*\)",
    re.IGNORECASE,
)
_DOLLAR_PARAM = re.compile(r"\$\d+")
_WS = re.compile(r"\s+")
# Pagination: OFFSET, or LIMIT with a range comparison on a parameter (keyset).
_OFFSET = re.compile(r"\bOFFSET\b", re.IGNORECASE)
_LIMIT = re.compile(r"\bLIMIT\b|\bFETCH\s+(?:FIRST|NEXT)\b|\bTOP\b", re.IGNORECASE)
_RANGE = re.compile(r"[<>]=?\s*(?:\?|%s|%\(\w+\)s|:\w+|\$\d+)")
# A bound parameter in the statement text, and what names its value: the
# position for ? and %s, the name for %(name)s and :name, the number for $1.
# String literals and quoted names come first so that what is inside them is
# skipped.
_BIND = re.compile(
    r"'(?:[^']|'')*'"
    r'|"(?:[^"]|"")*"'
    r"|(?P<pos>\?|%s)|%\((?P<name>\w+)\)s"
    r"|(?<![:\w]):(?P<colon>[A-Za-z_]\w*)|\$(?P<num>\d+)"
)
# What the text just before a parameter says it is (searched on a short tail).
_CAST_OPEN = r"(?:CAST\s*\(\s*)?"
_BEFORE_OFFSET = re.compile(rf"\bOFFSET\s*{_CAST_OPEN}$", re.IGNORECASE)
_BEFORE_LIMIT = re.compile(
    rf"(?:\bLIMIT|\bFETCH\s+(?:FIRST|NEXT)|\bTOP)\s*\(?\s*{_CAST_OPEN}$",
    re.IGNORECASE,
)
# <, <=, >, >= but not <> (not equal).
_BEFORE_RANGE = re.compile(rf"(?<![<>!=])(?:<=?|>=?)\s*{_CAST_OPEN}$")
# LIMIT 4 OFFSET 8 rendered with literal values.
_PAGE_LITERAL = re.compile(r"\b(LIMIT|OFFSET)\s+(\d+)\b", re.IGNORECASE)
# A chunk is a slice of a longer list: at least this many values per IN list.
# Shorter lists usually come from one item's own rows (an order's two
# addresses, a customer's ten orders), which is a loop, not batching. Batch
# sizes in loaders and ORMs are larger (SQLAlchemy's selectinload uses 500).
MIN_CHUNK = 50
# Distinct instances that must load columns on their own to count as N+1: as
# high as the repeat rule, because touching a few objects after a commit is
# ordinary test code.
DEFAULT_COLUMN_LOAD_THRESHOLD = 5
_T = TypeVar("_T")

# How the count rules group what they count: one group per test, or one per
# Session (or connection checkout). See docs/how-detection-works.md.
SCOPE_TEST = "test"
SCOPE_SESSION = "session"
SCOPES = (SCOPE_TEST, SCOPE_SESSION)


def collapse_lists(sql: str) -> str:
    """Collapse whitespace and parameter lists, so IN lists of any length match."""
    return _IN_LIST.sub("IN (...)", _WS.sub(" ", sql).strip())


@functools.lru_cache(maxsize=2048)
def normalize(sql: str) -> str:
    """Reduce a statement to a shape shared by all its executions.

    Pure, and a suite repeats the same statement texts, so the last 2048 are
    remembered.
    """
    # Lists first, so every placeholder style collapses to the same text.
    out = collapse_lists(sql)
    out = _STRING.sub("?", out)
    # Numbered parameters ($1, $2) become "?" so they compare equal.
    out = _DOLLAR_PARAM.sub("?", out)
    return _NUMBER.sub("?", out)


@dataclass(frozen=True, slots=True)
class RelationshipFinding:
    relationship: str
    count: int
    first: CallSite | None
    sample_sql: str | None
    scalar: bool = False  # many-to-one or one-to-one: joinedload, not selectinload
    # With scope "test": how many Sessions the loads came from. With scope
    # "session" it stays 1 and ``scoped`` is set: the count is for one Session.
    scopes: int = 1
    scoped: bool = False
    # Where the parents were loaded, when every load knows it: the first site,
    # and how many other sites there were.
    parent_site: CallSite | None = None
    other_parent_sites: int = 0
    # The query class and relationship chain that load the parents, when every
    # load has the same one (see events.ParentLoad); None when unknown.
    parent_root: str | None = None
    parent_path: tuple[tuple[str, bool], ...] = ()


@dataclass(frozen=True, slots=True)
class RepeatFinding:
    sql: str
    count: int
    first: CallSite | None
    # Found by the small-repeat rule: a few runs from one line, each with
    # different parameters, below the repeat threshold.
    varying: bool = False
    # See RelationshipFinding: Sessions or connection checkouts counted over.
    scopes: int = 1
    scoped: bool = False


@dataclass(frozen=True, slots=True)
class WriteLoopFinding:
    """One INSERT, UPDATE or DELETE shape run row by row from one line."""

    sql: str
    verb: str  # "INSERT", "UPDATE" or "DELETE"
    count: int
    first: CallSite | None
    scopes: int = 1
    scoped: bool = False


@dataclass(frozen=True, slots=True)
class ColumnLoadFinding:
    """Instances of one class that each loaded columns in their own statement."""

    owner: str  # the mapped class
    attributes: tuple[str, ...]  # what was loaded (deferred columns only)
    cause: str  # events.CAUSE_*
    count: int  # loads
    instances: int  # distinct instances
    first: CallSite | None
    sample_sql: str | None
    scopes: int = 1
    scoped: bool = False

    @property
    def label(self) -> str:
        if self.cause == CAUSE_DEFERRED and self.attributes:
            return f"{self.owner}.{','.join(self.attributes)}"
        return f"{self.owner} ({self.cause})"


Detection = RelationshipFinding | RepeatFinding | ColumnLoadFinding | WriteLoopFinding
# The name this union had before findings.Finding existed.
Finding = Detection


def _split(
    items: Sequence[_T], scope: Callable[[_T], object], scoped: bool
) -> list[list[_T]]:
    """One group with everything, or one group per scope (in order of first use)."""
    if not scoped:
        return [list(items)]
    groups: dict[object, list[_T]] = {}
    for item in items:
        groups.setdefault(scope(item), []).append(item)
    return list(groups.values())


def _scope_count(items: Sequence[_T], scope: Callable[[_T], object]) -> int:
    return len({scope(item) for item in items})


def _hidden(
    sieve: Sieve | None,
    events: Sequence[Any],
    kind: str,
    held: list[IgnoreBlock],
) -> bool:
    """True when ``events`` all ran inside ignore() blocks for ``kind``.

    The blocks go to ``held``; they count as used (``_note_hidden``) only
    when no other group of the same finding is reported instead.
    """
    if sieve is None:
        return False
    blocks = covering(events, kind)
    held.extend(blocks)
    return bool(blocks)


def _note_hidden(sieve: Sieve | None, held: list[IgnoreBlock]) -> None:
    if sieve is not None:
        sieve.note(held)


def _parents(
    loads: Sequence[RelationshipLoadEvent],
) -> tuple[CallSite | None, int, str | None, tuple[tuple[str, bool], ...]]:
    """(parent site, other sites, root, path) for a finding; see RelationshipFinding.

    A site or a path is given only when every load knows one: a load whose
    parent was created in the test, not loaded, could come from anywhere.
    """
    parents = [load.parent for load in loads]
    sites: dict[CallSite, None] = {}
    for parent in parents:
        if parent is None or parent.callsite is None:
            sites.clear()
            break
        sites[parent.callsite] = None
    first = next(iter(sites), None)
    root: str | None = None
    path: tuple[tuple[str, bool], ...] = ()
    known = parents[0]
    if known is not None and known.root is not None:
        way = (known.root, known.path)
        if all(p is not None and (p.root, p.path) == way for p in parents):
            root, path = way
    return first, max(len(sites) - 1, 0), root, path


def _n_plus_one_loads(
    loads: Sequence[RelationshipLoadEvent], threshold: int
) -> list[RelationshipLoadEvent]:
    """The loads of one relationship that make an N+1, in their order.

    As in strict mode, the parents are grouped by the query line that loaded
    them: the loads of a group with ``threshold`` distinct parents or more
    count, so two single-object lookups at two lines are no N+1 (#307). When
    the query of any parent is unknown (made by the test, loaded before the
    measurement, read by two queries) it may belong to any group, so all the
    loads form one group: an unknown origin never hides a loop.
    """
    groups: dict[CallSite, list[RelationshipLoadEvent]] = {}
    for load in loads:
        parent = load.parent
        if parent is None or parent.callsite is None:
            return list(loads)
        groups.setdefault(parent.callsite, []).append(load)
    if len(groups) == 1:
        return list(loads)
    keep: set[int] = set()
    for group in groups.values():
        if len({load.parent_key for load in group}) >= threshold:
            keep.update(id(load) for load in group)
    return [load for load in loads if id(load) in keep]


def _relationship_findings(
    collector: QueryCollector,
    threshold: int,
    scoped: bool = False,
    sieve: Sieve | None = None,
) -> list[RelationshipFinding]:
    by_name: dict[str, list[RelationshipLoadEvent]] = {}
    for load in collector.relationship_loads:
        if load.parent_key is not None:
            by_name.setdefault(load.relationship, []).append(load)
    found: list[RelationshipFinding] = []
    for name, loads in by_name.items():
        best: list[RelationshipLoadEvent] | None = None
        best_parents = 0
        held: list[IgnoreBlock] = []
        for scope_loads in _split(loads, lambda load: load.scope, scoped):
            group = _n_plus_one_loads(scope_loads, threshold)
            parents = len({load.parent_key for load in group})
            if parents >= threshold and parents > best_parents:
                if _hidden(sieve, group, KIND_RELATIONSHIP, held):
                    continue
                best, best_parents = group, parents
        if best is None:
            _note_hidden(sieve, held)
            continue
        scope = best[0].scope
        # A lazy load is the statement the finding counts; the query of an
        # eager loader carries the same label and is used only when no lazy
        # load was recorded (events beyond the cap).
        sample = None
        best_rank = 0
        for stmt in collector.statements:
            if stmt.relationship != name:
                continue
            rank = (2 if stmt.lazy_load else 1) * 2 + (
                1 if not scoped or stmt.scope == scope else 0
            )
            if rank > best_rank:
                sample, best_rank = stmt.sql, rank
                if rank == 5:
                    break
        site = next((load.callsite for load in best if load.callsite), None)
        found.append(
            RelationshipFinding(
                name,
                len(best),
                site,
                sample,
                best[-1].scalar,
                1 if scoped else _scope_count(best, lambda load: load.scope),
                scoped,
                *_parents(best),
            )
        )
    return sorted(found, key=lambda f: -f.count)


def _column_load_findings(
    collector: QueryCollector,
    threshold: int,
    scoped: bool = False,
    sieve: Sieve | None = None,
) -> list[ColumnLoadFinding]:
    groups: dict[tuple[str, tuple[str, ...], str], list[ColumnLoadEvent]] = {}
    for load in collector.column_loads:
        if load.cause != CAUSE_DEFERRED:
            attrs: tuple[str, ...] = ()
        else:
            attrs = load.attributes
        groups.setdefault((load.owner, attrs, load.cause), []).append(load)
    found: list[ColumnLoadFinding] = []
    for (owner, attrs, cause), all_loads in groups.items():
        best: list[ColumnLoadEvent] | None = None
        best_keys = 0
        held: list[IgnoreBlock] = []
        for loads in _split(all_loads, lambda load: load.scope, scoped):
            keys = len({load.instance_key for load in loads})
            if keys >= threshold and keys > best_keys:
                if _hidden(sieve, loads, KIND_COLUMN_LOAD, held):
                    continue
                best, best_keys = loads, keys
        if best is None:
            _note_hidden(sieve, held)
            continue
        site = next((load.callsite for load in best if load.callsite), None)
        sample = next((load.sql for load in best if load.sql), None)
        found.append(
            ColumnLoadFinding(
                owner,
                attrs,
                cause,
                len(best),
                best_keys,
                site,
                sample,
                1 if scoped else _scope_count(best, lambda load: load.scope),
                scoped,
            )
        )
    return sorted(found, key=lambda f: -f.count)


def _value_rows(sql: str) -> int:
    """The number of row tuples in the VALUES list of a statement (1 without one)."""
    match = re.search(r"\bVALUES\b", sql, re.IGNORECASE)
    if match is None:
        return 1
    rows, depth, i, n = 0, 0, match.end(), len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":  # a string literal; '' inside it is an escaped quote
            end = i + 1
            while end < n:
                if sql[end] == "'" and sql[end + 1 : end + 2] == "'":
                    end += 2
                elif sql[end] == "'":
                    break
                else:
                    end += 1
            i = end
        elif ch == "(":
            if depth == 0:
                rows += 1
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and ch not in " ,\n\t":
            break  # RETURNING, ON CONFLICT ...: the list is over
        i += 1
    return max(rows, 1)


def _write_loop_findings(
    collector: QueryCollector,
    threshold: int,
    scoped: bool = False,
    sieve: Sieve | None = None,
) -> list[WriteLoopFinding]:
    """The same write, one row per statement, from one line.

    ``executemany`` and multi-row ``VALUES`` statements are already batched,
    so they are never counted, and the statements of one ORM flush count as
    one: SQLAlchemy sent them as it had to.
    """
    groups: dict[tuple[str, object], list[StatementEvent]] = {}
    for stmt in collector.statements:
        if stmt.executemany or not is_write(stmt.sql):
            continue
        if _value_rows(_WS.sub(" ", stmt.sql)) > 1:
            continue
        site = stmt.callsite
        where = None if site is None else (site.filename, site.lineno)
        groups.setdefault((normalize(stmt.sql), where), []).append(stmt)
    found = []
    for (sql, _), all_stmts in groups.items():
        best: list[StatementEvent] | None = None
        best_count = 0
        held: list[IgnoreBlock] = []
        for stmts in _split(all_stmts, lambda s: s.scope, scoped):
            # All statements of one ORM flush count as one write: the unit of
            # work sends them one by one on a dialect without insertmanyvalues,
            # and the code can only call flush() less often, not batch them
            # further.
            flushes = {s.flush for s in stmts if s.flush is not None}
            count = len(flushes) + sum(1 for s in stmts if s.flush is None)
            if count >= threshold and count > best_count:
                if _hidden(sieve, stmts, KIND_WRITE_LOOP, held):
                    continue
                best, best_count = stmts, count
        if best is None:
            _note_hidden(sieve, held)
            continue
        first = best[0].sql
        start = skip_prefix(first) or 0
        verb = first[start : start + 6].upper()  # every write verb has 6 letters
        found.append(
            WriteLoopFinding(
                sql,
                verb,
                best_count,
                best[0].callsite,
                1 if scoped else _scope_count(best, lambda s: s.scope),
                scoped,
            )
        )
    return sorted(found, key=lambda f: -f.count)


# Statements the database's own catalogs answer, which SQLAlchemy and Alembic
# run for schema reflection. The application cannot batch them, so the repeat
# rules do not count them (budgets and the report still do):
#   - a PRAGMA (SQLite: table_info, index_list, foreign_key_list, ...);
#   - a read of a SQLite catalog table: sqlite_master, sqlite_schema,
#     sqlite_temp_master or sqlite_temp_schema, with or without main./temp.;
#   - a read of anything in the pg_catalog or information_schema schema
#     (PostgreSQL; information_schema also exists in MySQL and MariaDB).
# Matched on the normalized text, where string literals are already gone, so a
# value 'sqlite_master' in a WHERE clause does not hide a statement; nor do
# comments, CTEs or aliases (_SHADOWS_CATALOG). A statement that joins an
# application table with a real catalog is excluded too.
# Not excluded: MySQL's SHOW and DESCRIBE, and PostgreSQL catalog tables read
# without the pg_catalog schema (pg_class on the search path).
_CATALOG = re.compile(
    r"""
      \A[\s(]*PRAGMA\b
    | (?<![\w$.])(?:"?(?:main|temp)"?\s*\.\s*)?"?sqlite_(?:temp_)?(?:master|schema)"?(?![\w$])
    | (?<![\w$.])"?(?:pg_catalog|information_schema)"?\s*\.
    """,
    re.IGNORECASE | re.VERBOSE,
)


# Comments can name anything, and a statement may define a CTE or an alias with
# a catalog's name (WITH sqlite_master AS (...), FROM orders AS pg_catalog);
# neither makes the statement a catalog read.
_SQL_COMMENT = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)
_SHADOWS_CATALOG = re.compile(
    r"""
      (?:\bWITH(?:\s+RECURSIVE)?|,)\s*"?(?:sqlite_(?:temp_)?(?:master|schema)
          |pg_catalog|information_schema)"?\s*(?:\([^()]*\)\s*)?AS\b
    | \bAS\s+"?(?:sqlite_(?:temp_)?(?:master|schema)|pg_catalog|information_schema)
          "?(?![\w$])
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_catalog_statement(sql: str) -> bool:
    """True for schema reflection that the repeat rules do not count."""
    sql = _SQL_COMMENT.sub(" ", sql)
    if _SHADOWS_CATALOG.search(sql):
        return False
    return _CATALOG.search(sql) is not None


def _paginates(sql: str) -> bool:
    if _OFFSET.search(sql):
        return True
    return bool(_LIMIT.search(sql) and _RANGE.search(sql))


_PLAIN, _LIMIT_ROLE, _OFFSET_ROLE, _RANGE_ROLE = "plain", "limit", "offset", "range"


@functools.lru_cache(maxsize=512)
def _layout(sql: str) -> tuple[str, tuple[tuple[str, object, int], ...]]:
    """The text that every run of a statement shares, and its parameters.

    Returns the text with ``IN`` lists collapsed and literal ``LIMIT`` and
    ``OFFSET`` values masked, and one ``(role, key, in_list)`` per parameter:
    the role (plain, limit, offset or range), the key that finds its value in
    the driver's parameters, and the number of the ``IN`` list it belongs to
    (-1 when none). Literal ``LIMIT`` and ``OFFSET`` values become parameters
    with an ``int`` value as key.
    """
    lists = [m.span() for m in _IN_LIST.finditer(sql)]
    slots: list[tuple[str, object, int]] = []
    position = 0
    for m in _BIND.finditer(sql):
        if m.group("pos") is not None:
            key: object = position
            position += 1
        elif m.group("name") is not None:
            key = m.group("name")
        elif m.group("colon") is not None:
            key = m.group("colon")
        elif m.group("num") is not None:
            key = int(m.group("num")) - 1
        else:
            continue  # a string literal or a quoted name
        start = m.start()
        in_list = next((i for i, (a, b) in enumerate(lists) if a <= start < b), -1)
        tail = sql[max(0, start - 40) : start]
        if in_list >= 0:
            role = _PLAIN
        elif _BEFORE_OFFSET.search(tail):
            role = _OFFSET_ROLE
        elif _BEFORE_LIMIT.search(tail):
            role = _LIMIT_ROLE
        elif _BEFORE_RANGE.search(tail):
            role = _RANGE_ROLE
        else:
            role = _PLAIN
        slots.append((role, key, in_list))
    for m in _PAGE_LITERAL.finditer(sql):
        role = _OFFSET_ROLE if m.group(1).upper() == "OFFSET" else _LIMIT_ROLE
        slots.append((role, _Literal(int(m.group(2))), -1))
    text = _PAGE_LITERAL.sub(lambda m: f"{m.group(1)} ?", collapse_lists(sql))
    return text, tuple(slots)


@dataclass(frozen=True, slots=True)
class _Literal:
    """A value written in the statement text, not bound."""

    value: int


@dataclass(frozen=True, slots=True)
class _Run:
    """One run of a statement, its parameter values sorted by what they do."""

    fixed: tuple[object, ...]  # the shared text and the plain values (repr)
    limits: tuple[object, ...]
    cursor: tuple[Any, ...]  # OFFSET values, then range bounds, in text order
    offsets: tuple[Any, ...]
    bounds: tuple[Any, ...]  # the range bounds (keyset cursors) alone
    lists: tuple[tuple[str, ...], ...]  # the values of each IN list (repr)


_MISSING = object()


def _value(parameters: object, key: object) -> object:
    if isinstance(key, _Literal):
        return key.value
    if isinstance(parameters, Mapping):
        return parameters.get(key, _MISSING)
    if (
        isinstance(parameters, Sequence)
        and not isinstance(parameters, (str, bytes))
        and isinstance(key, int)
        and 0 <= key < len(parameters)
    ):
        return parameters[key]
    return _MISSING


def _run(stmt: StatementEvent) -> _Run | None:
    """The parameters of one run by role, or None when they cannot be read."""
    text, slots = _layout(stmt.sql)
    plain: list[str] = []
    limits: list[object] = []
    cursor: list[Any] = []
    offsets: list[Any] = []
    bounds: list[Any] = []
    lists: dict[int, list[str]] = {}
    for role, key, in_list in slots:
        value = _value(stmt.parameters, key)
        if value is _MISSING:
            return None
        if in_list >= 0:
            lists.setdefault(in_list, []).append(repr(value))
        elif role == _LIMIT_ROLE:
            limits.append(value)
        elif role == _OFFSET_ROLE:
            offsets.append(value)
            cursor.append(value)
        elif role == _RANGE_ROLE:
            cursor.append(value)
            bounds.append(value)
        else:
            plain.append(repr(value))
    return _Run(
        (text, *plain),
        tuple(limits),
        tuple(cursor),
        tuple(offsets),
        tuple(bounds),
        tuple(tuple(lists[i]) for i in sorted(lists)),
    )


def _step(before: Any, after: Any) -> int:
    """1 when ``after`` is larger, -1 when smaller, 0 when equal."""
    return int(after > before) - int(after < before)


def _walks(runs: Sequence[_Run]) -> bool:
    """Pagination: the runs walk through one result, page by page.

    Everything but the cursor (the ``OFFSET`` or the keyset bound) is the same
    in every run, the cursor moves the same way from page to page by at least
    a page (an integer keyset bound included), and a page holds more than one
    row. The walk may start again from its first cursor once (a test that
    reads the same list twice); the same walk run more often is a loop.
    ``LIMIT 1 OFFSET n``, one row per statement, is a loop, not pagination.
    """
    first = runs[0]
    if not first.cursor:
        return False
    if any(r.fixed != first.fixed or r.lists != first.lists for r in runs):
        return False
    if any(repr(r.limits) != repr(first.limits) for r in runs):
        return False
    if not first.limits and not first.offsets:
        return False  # a range condition without LIMIT is a filter, not a page
    sizes = [v for v in first.limits if isinstance(v, int) and not isinstance(v, bool)]
    if any(size <= 1 for size in sizes):
        return False
    page = min(sizes, default=None)
    direction, length, restarts, ended = 0, 1, 0, False
    for before, after in itertools.pairwise(runs):
        step = _step(before.cursor, after.cursor)
        if step == 0:
            return False  # the same page again
        if length > 1 and step != direction:
            # A new walk: it must start where the first one did.
            if after.cursor != first.cursor:
                return False
            restarts += 1
            if restarts > 1:
                return False  # the same walk in a loop: a loop-invariant query
            direction, length, ended = 0, 1, False
            continue
        if ended:
            return False  # a short keyset step can only end a walk
        if page is not None and before.offsets != after.offsets:
            moved = [
                abs(b - a)
                for a, b in zip(before.offsets, after.offsets, strict=True)
                if isinstance(a, int) and isinstance(b, int)
            ]
            if any(m < page for m in moved):
                return False  # pages overlap: a sliding window, not paging
        if before.bounds != after.bounds:
            distance = _keyset_moved(before.bounds, after.bounds)
            if distance is None or page is None:
                return False  # a string or date bound: its step says nothing
            # The page before a short step was not full, so it was the last.
            ended = distance < page
        direction, length = step, length + 1
    return length > 1


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _keyset_moved(before: Sequence[Any], after: Sequence[Any]) -> int | None:
    """How far a keyset cursor moved, or None when that cannot be measured.

    The next cursor of a keyset walk is the last key of the page before, so on
    a unique integer key it moves by at least the page size after every full
    page; only the last page, the one that was not full, moves it less. A
    bound that moves by less again and again (each row of a loop,
    ``WHERE id > :row_id LIMIT 3``) is not paging. A string or a date bound
    cannot be measured.
    """
    steps: list[int] = []
    for a, b in zip(before, after, strict=True):
        if a == b:
            continue
        if not (_is_int(a) and _is_int(b)):
            return None
        steps.append(abs(int(b) - int(a)))
    return min(steps, default=None)


def _partitions(runs: Sequence[_Run]) -> bool:
    """Chunked ``IN``: the runs load slices of one list of keys.

    One ``IN`` list differs from run to run and everything else is the same;
    the lists do not share a value; and every list has the same size, at least
    ``MIN_CHUNK``, except one shorter list (the remainder).
    """
    first = runs[0]
    if not first.lists:
        return False
    for r in runs:
        if (r.fixed, r.cursor, len(r.lists)) != (
            first.fixed,
            first.cursor,
            len(first.lists),
        ):
            return False
        if repr(r.limits) != repr(first.limits):
            return False
    varying = [
        i
        for i in range(len(first.lists))
        if any(r.lists[i] != first.lists[i] for r in runs)
    ]
    if len(varying) != 1:
        return False
    chunks = [r.lists[varying[0]] for r in runs]
    size = max(len(c) for c in chunks)
    if size < MIN_CHUNK or sum(len(c) != size for c in chunks) > 1:
        return False
    seen: set[str] = set()
    for chunk in chunks:
        seen.update(chunk)
    return len(seen) == sum(len(c) for c in chunks)


def _batching(stmts: Sequence[StatementEvent]) -> bool:
    """True when the runs of one statement shape are pagination or chunks.

    Read from the statements and their bound parameters only; anything that
    cannot be read (an unknown placeholder style, missing parameters) counts as
    not batching, so the statements are reported as before.
    """
    if len(stmts) < 2:
        return False
    sql = stmts[0].sql
    pages = _paginates(sql)
    if not pages and _IN_LIST.search(sql) is None:
        return False
    try:
        runs = []
        for stmt in stmts:
            run = _run(stmt)
            if run is None:
                return False
            runs.append(run)
        return (pages and _walks(runs)) or _partitions(runs)
    except Exception:  # noqa: BLE001  # odd parameter values: report as before
        return False


def _repeat_groups(collector: QueryCollector) -> dict[str, list[StatementEvent]]:
    """The statements the repeat rules count, by normalized text.

    Writes, transaction control (BEGIN, COMMIT, SAVEPOINT, ...) and schema
    reflection are left out: a loop that commits each pass runs one BEGIN and
    one COMMIT per pass. Budgets still count them.
    """
    groups: dict[str, list[StatementEvent]] = {}
    for stmt in collector.statements:
        if stmt.relationship is not None or stmt.column_load:
            continue
        if is_write(stmt.sql) or is_transaction_control(stmt.sql):
            continue
        groups.setdefault(normalize(stmt.sql), []).append(stmt)
    return {key: g for key, g in groups.items() if not is_catalog_statement(key)}


def _small_repeat_findings(
    collector: QueryCollector,
    small: int,
    threshold: int,
    scoped: bool = False,
    skip: frozenset[str] = frozenset(),
    sieve: Sieve | None = None,
) -> list[RepeatFinding]:
    """A few runs of one statement from one line, each with other parameters.

    Between ``small`` and ``threshold - 1`` runs: the repeat rule's threshold
    would not report them. Only a loop looks like this, so the rule asks for
    one call site and a different set of parameters (or literals) each time,
    and leaves pagination alone.
    """
    found: list[RepeatFinding] = []
    for key, all_stmts in _repeat_groups(collector).items():
        if key in skip or _paginates(all_stmts[0].sql):
            continue
        best: list[StatementEvent] | None = None
        held: list[IgnoreBlock] = []
        for stmts in _split(all_stmts, lambda s: s.scope, scoped):
            if not small <= len(stmts) < threshold or _batching(stmts):
                continue
            sites = {
                (s.callsite.filename, s.callsite.lineno) if s.callsite else None
                for s in stmts
            }
            if len(sites) != 1 or None in sites:
                continue
            runs = {(_WS.sub(" ", s.sql), repr(s.parameters)) for s in stmts}
            if len(runs) == len(stmts) and (best is None or len(stmts) > len(best)):
                if _hidden(sieve, stmts, KIND_REPEAT, held):
                    continue
                best = stmts
        if best is None:
            _note_hidden(sieve, held)
        else:
            found.append(
                RepeatFinding(
                    key,
                    len(best),
                    best[0].callsite,
                    True,
                    1 if scoped else _scope_count(best, lambda s: s.scope),
                    scoped,
                )
            )
    return sorted(found, key=lambda f: -f.count)


def _repeat_findings(
    collector: QueryCollector,
    threshold: int,
    scoped: bool = False,
    sieve: Sieve | None = None,
) -> tuple[list[RepeatFinding], set[str]]:
    """The findings, and the statements a finding inside ignore() blocks had."""
    found: list[RepeatFinding] = []
    hidden: set[str] = set()
    for key, all_stmts in _repeat_groups(collector).items():
        best: list[StatementEvent] | None = None
        held: list[IgnoreBlock] = []
        for stmts in _split(all_stmts, lambda s: s.scope, scoped):
            if len(stmts) < threshold or (best is not None and len(stmts) <= len(best)):
                continue
            if not _batching(stmts):
                if _hidden(sieve, stmts, KIND_REPEAT, held):
                    continue
                best = stmts
        if best is None:
            if held:
                hidden.add(key)
            _note_hidden(sieve, held)
            continue
        site = next((s.callsite for s in best if s.callsite), None)
        found.append(
            RepeatFinding(
                key,
                len(best),
                site,
                False,
                1 if scoped else _scope_count(best, lambda s: s.scope),
                scoped,
            )
        )
    return sorted(found, key=lambda f: -f.count), hidden


def detect(
    collector: QueryCollector,
    *,
    relationship_threshold: int,
    repeat_threshold: int,
    column_load_threshold: int = DEFAULT_COLUMN_LOAD_THRESHOLD,
    small_repeat_threshold: int | None = None,
    write_loop_threshold: int | None = None,
    scope: str = SCOPE_TEST,
    sieve: Sieve | None = None,
) -> list[Detection]:
    """Run the count rules; ``scope`` is "test" (all) or "session" (per Session).

    With a ``sieve``, a finding whose events all ran inside ``ignore()``
    blocks for its kind is left out, and the sieve notes those blocks. Not
    when the collector stopped storing events (``max_events``): the events it
    only counted may have run outside the blocks.
    """
    if collector.truncated:
        sieve = None
    scoped = scope == SCOPE_SESSION
    findings: list[Detection] = []
    findings.extend(
        _relationship_findings(collector, relationship_threshold, scoped, sieve)
    )
    findings.extend(
        _column_load_findings(collector, column_load_threshold, scoped, sieve)
    )
    repeats, hidden = _repeat_findings(collector, repeat_threshold, scoped, sieve)
    findings.extend(repeats)
    if small_repeat_threshold is not None and small_repeat_threshold < repeat_threshold:
        findings.extend(
            _small_repeat_findings(
                collector,
                small_repeat_threshold,
                repeat_threshold,
                scoped,
                frozenset(f.sql for f in repeats) | hidden,
                sieve,
            )
        )
    if write_loop_threshold is not None:
        findings.extend(
            _write_loop_findings(collector, write_loop_threshold, scoped, sieve)
        )
    return findings
