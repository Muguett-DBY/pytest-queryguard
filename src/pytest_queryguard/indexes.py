"""Missing-index check: plan each SELECT with sequential scans disabled.

See docs/design-0.2.md for the reasoning behind each rule here.
"""

from __future__ import annotations

import contextlib
import json
import re
import weakref
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from . import advisor, blocks, hypopg, partial, predicates
from . import catalog as catalogs
from .callsite import find_callsite
from .collector import QueryCollector
from .detectors import collapse_lists
from .events import Advice, Catalog, ScanEvent, ScanIssue, Verification
from .sqltext import QUOTED, skip_prefix
from .tablenames import table_matches

MIN_SERVER_VERSION = 16
# The newest PostgreSQL release the check is tested on (CI and the plan
# fixtures in tests/fixtures/plans). A newer server is still checked, with one
# notice per session: its plans may print conditions in a new way.
MAX_TESTED_VERSION = 18
ASYNCPG = "asyncpg"  # the one async driver the check supports
_SAVEPOINT = "queryguard_explain"
_SETTINGS = (
    "SELECT set_config('enable_seqscan', 'off', true), "
    "set_config('lock_timeout', '1s', true), "
    "set_config('statement_timeout', '5s', true), "
    "current_schema()"
)
_EXPLAIN = "EXPLAIN (GENERIC_PLAN, VERBOSE, FORMAT JSON) "

# ------------------------------------------------------------ statement text

_VERB = re.compile(r"(?:SELECT|WITH)\b", re.IGNORECASE)

# Tokens that are copied unchanged, then the placeholders that are rewritten.
_TOKEN = re.compile(
    rf"""
      (?P<copy>{QUOTED})
    | (?P<named>%\((?P<name>[^)]*)\)s)
    | (?P<positional>%s)
    | (?P<percent>%%)
    """,
    re.VERBOSE | re.DOTALL,
)


def is_checked_statement(sql: str) -> bool:
    """Only SELECT statements (and WITH queries) are explained.

    Leading comments, such as sqlcommenter tags, are skipped.
    """
    start = skip_prefix(sql)
    return start is not None and _VERB.match(sql, start) is not None


def to_numbered(
    sql: str, paramstyle: str, *, unescape_quoted: bool = False
) -> str | None:
    """The statement with $1, $2, ... placeholders, or None for an unknown style.

    ``unescape_quoted`` turns ``%%`` into ``%`` inside quoted text and
    comments too, as psycopg and psycopg2 do; pg8000 leaves them alone.
    """
    if paramstyle == "numeric_dollar":
        return sql
    if paramstyle not in ("format", "pyformat"):
        return None
    numbers: dict[str, int] = {}
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        if match["copy"] is not None:
            copy = match["copy"]
            return copy.replace("%%", "%") if unescape_quoted else copy
        if match["percent"] is not None:
            return "%"
        if match["named"] is not None:
            name = match["name"]
            if name not in numbers:
                count += 1
                numbers[name] = count
            return f"${numbers[name]}"
        count += 1
        return f"${count}"

    return _TOKEN.sub(replace, sql)


# ---------------------------------------------------------------- plan walk

_SYSTEM_SCHEMAS = frozenset({"pg_catalog", "information_schema", "pg_toast"})
_INDEX_SCANS = ("Index Scan", "Index Only Scan")
_BITMAP_SCAN = "Bitmap Index Scan"
# The PostgreSQL release that can skip-scan the leading column of an index.
SKIP_SCAN_VERSION = 18

# (schema, index name) -> name of the index's first key column
LeadingColumns = Mapping[tuple[str, str], str]
# (schema, table) -> names of its varchar columns
VarcharColumns = Mapping[tuple[str, str], frozenset[str]]


@dataclass(frozen=True, slots=True)
class _Context:
    current: str
    leading: LeadingColumns
    varchar: VarcharColumns


@dataclass(frozen=True, slots=True)
class _Above:
    """What sits directly above a plan node and shapes the index it needs."""

    sort: tuple[str, ...] = ()  # the keys of a Sort node right above
    limit: bool = False  # a Limit above, directly or through that Sort


_NO_ABOVE = _Above()


@dataclass(frozen=True, slots=True)
class _Relation:
    schema: str | None
    table: str
    alias: str


def find_scans(
    plan: Mapping[str, Any],
    current_schema: str | None = None,
    leading: LeadingColumns | None = None,
    varchar: VarcharColumns | None = None,
) -> list[ScanIssue]:
    """Plan nodes that read a whole table, or a whole index, to apply a condition.

    Tables in ``current_schema`` (``public`` when unknown) are named without
    their schema, so the fix hint works as written on that connection.
    ``leading`` enables the check for an index condition on a trailing
    column of a multicolumn btree index. ``varchar`` names the columns whose
    ``::text`` cast an index on the plain column still serves.
    """
    found: list[ScanIssue] = []
    ctx = _Context(current_schema or "public", leading or {}, varchar or {})
    _walk(plan, None, ctx, found, _NO_ABOVE)
    return found


def index_names(plan: Mapping[str, Any]) -> set[tuple[str, str]]:
    """(schema, index) of every index scan in the plan that has an Index Cond."""
    names: set[tuple[str, str]] = set()

    def visit(node: object, heap: _Relation | None) -> None:
        if not isinstance(node, Mapping):
            return
        rel = _relation(node)
        index = _text(node, "Index Name")
        owner = rel or heap
        if index and node.get("Index Cond") and owner is not None and owner.schema:
            names.add((owner.schema, index))
        below = rel if node.get("Node Type") == "Bitmap Heap Scan" else heap
        for child in _children(node):
            visit(child, below)

    visit(plan, None)
    return names


def _children(node: Mapping[str, Any]) -> list[object]:
    children = node.get("Plans")
    return children if isinstance(children, list) else []


def _text(node: Mapping[str, Any], key: str) -> str | None:
    value = node.get(key)
    return value if isinstance(value, str) and value else None


def _relation(node: Mapping[str, Any]) -> _Relation | None:
    table = _text(node, "Relation Name")
    if table is None:
        return None
    return _Relation(_text(node, "Schema"), table, _text(node, "Alias") or table)


def _above_children(node: Mapping[str, Any], above: _Above) -> _Above:
    kind = node.get("Node Type")
    if kind == "Limit":
        return _Above(limit=True)
    if kind == "Sort":
        keys = node.get("Sort Key")
        if isinstance(keys, list) and all(isinstance(k, str) for k in keys):
            return _Above(tuple(keys), above.limit)
    return _NO_ABOVE


def _walk(
    node: object,
    heap: _Relation | None,
    ctx: _Context,
    found: list[ScanIssue],
    above: _Above,
) -> None:
    if not isinstance(node, Mapping):
        return
    rel = _relation(node)
    issue = _issue(node, rel or heap, ctx, above)
    if issue is not None:
        found.append(issue)
    # A Bitmap Index Scan names no table; it reads the one of its heap scan.
    below = rel if node.get("Node Type") == "Bitmap Heap Scan" else heap
    inside = _above_children(node, above)
    for child in _children(node):
        _walk(child, below, ctx, found, inside)


def _whole_bitmap_index(node: Mapping[str, Any]) -> str | None:
    """The index a Bitmap Heap Scan reads in full, without an index condition.

    Only a Bitmap Index Scan directly below the heap scan counts: below a
    BitmapAnd or BitmapOr another index narrows the read or adds to it. The
    heap scan's Recheck Cond does not matter here; without an Index Cond it
    is only the predicate of a partial index, which every row of the index
    meets.
    """
    for child in _children(node):
        if (
            isinstance(child, Mapping)
            and child.get("Node Type") == _BITMAP_SCAN
            and not child.get("Index Cond")
        ):
            return _text(child, "Index Name") or "?"
    return None


def _issue(
    node: Mapping[str, Any], rel: _Relation | None, ctx: _Context, above: _Above
) -> ScanIssue | None:
    if rel is None or rel.schema in _SYSTEM_SCHEMAS:
        return None
    node_type = node.get("Node Type")
    index = _text(node, "Index Name")
    label = f"{node_type} using {index}" if index else str(node_type)
    varchar = ctx.varchar.get((rel.schema or "", rel.table), frozenset())
    if node_type == "Seq Scan":
        # Rule 1: the table is read in full to apply its filter.
        condition = _text(node, "Filter")
        if condition is None or _only_not_equal(condition):
            return None
        return _make(rel, "Seq Scan", "filter", condition, ctx, node, above)
    if node_type == "Bitmap Heap Scan":
        # Rule 2 again: before ANALYZE, a whole index (an INCLUDE index or a
        # partial index, say) can be read as a bitmap instead of an index or
        # index-only scan.
        whole = _whole_bitmap_index(node)
        condition = _text(node, "Filter")
        if whole is None or condition is None:
            return None
        if equality_column(condition, rel.alias, varchar) is None:
            return None
        return _make(
            rel,
            f"Bitmap Heap Scan using {whole}",
            "filter",
            condition,
            ctx,
            node,
            above,
            walked=f"index {whole}",
        )
    if node_type not in (*_INDEX_SCANS, _BITMAP_SCAN):
        return None
    index_cond = _text(node, "Index Cond")
    if index_cond is None:
        # Rule 2: a whole index walked to avoid the disabled sequential scan,
        # for example the primary key for ORDER BY id LIMIT n. Pagination with
        # a range or IS NULL filter does that on purpose, so only an equality
        # on one column, which an index would serve directly, is reported.
        condition = _text(node, "Filter")
        if node_type == _BITMAP_SCAN or condition is None:
            return None
        if equality_column(condition, rel.alias, varchar) is None:
            return None
        return _make(
            rel,
            label,
            "filter",
            condition,
            ctx,
            node,
            above,
            walked=f"index {index}" if index else None,
        )
    # Rule 3: the index condition uses only trailing columns of a multicolumn
    # index, which PostgreSQL before 18 serves by reading the whole index.
    first = ctx.leading.get((rel.schema or "", index or ""))
    if first is None:
        return None
    columns = condition_columns(index_cond, rel.alias)
    if first in columns or equality_column(index_cond, rel.alias, varchar) is None:
        return None
    return _make(
        rel,
        label,
        "index condition",
        index_cond,
        ctx,
        node,
        above,
        walked=f"index {index}" if index else None,
    )


_CONSTANT = re.compile(r"'.*'|[-+]?\d+(\.\d+)?|true|false", re.IGNORECASE | re.DOTALL)


def _constant(expr: predicates.Expr) -> bool:
    """A value written into the SQL: a literal, a cast of one, an array of them."""
    if isinstance(expr, predicates.Lit):
        return _CONSTANT.fullmatch(expr.text) is not None
    if isinstance(expr, predicates.Cast):
        return _constant(expr.operand)
    if isinstance(expr, predicates.Call) and expr.name.lower() in (
        "array",
        "any",
        "all",
    ):
        return bool(expr.args) and all(_constant(arg) for arg in expr.args)
    return False


def _not_equal(expr: predicates.Expr) -> bool:
    """``<>``, ``<> ALL (...)`` or ``NOT (x = ANY (...))`` against a parameter.

    No btree index serves it. Against a constant written into the SQL
    (``status <> 'done'``) a partial index ``WHERE status <> 'done'`` does,
    and is worth it when the excluded value is the common one, so that stays
    reportable.
    """
    if isinstance(expr, predicates.Not):
        expr = expr.operand
        if not (isinstance(expr, predicates.Op) and expr.op == "="):
            return False
    elif not (isinstance(expr, predicates.Op) and expr.op in ("<>", "!=")):
        return False
    return not (_constant(expr.left) or _constant(expr.right))


def _only_not_equal(condition: str) -> bool:
    """True when every condition ANDed together is a not-equal comparison.

    A sequential scan is what the planner must do then, indexed column or not,
    so there is no index to add (see ``_not_equal`` for constants). One condition an index could serve (``a = $1
    AND b <> $2``) makes the scan reportable as before; text the parser does
    not know is reportable too.
    """
    tree = predicates.parse(condition)
    if tree is None:
        return False
    items = (
        tree.items
        if isinstance(tree, predicates.Bool) and tree.op == "AND"
        else (tree,)
    )
    return all(_not_equal(item) for item in items)


def _make(
    rel: _Relation,
    label: str,
    kind: str,
    condition: str,
    ctx: _Context,
    node: Mapping[str, Any],
    above: _Above,
    walked: str | None = None,
) -> ScanIssue:
    """``walked`` names an existing index the plan reads end to end ("index x")."""
    varchar = ctx.varchar.get((rel.schema or "", rel.table), frozenset())
    column = hint_column(condition, rel.alias, varchar)
    conditions = tuple(
        text for key in ("Index Cond", "Filter") if (text := _text(node, key))
    )
    return ScanIssue(
        rel.schema,
        rel.table,
        label,
        kind,
        condition,
        column,
        show_schema=rel.schema not in (None, ctx.current),
        expression=column is None and _needs_expression(condition, rel.alias, varchar),
        index=walked,
        alias=rel.alias,
        conditions=conditions,
        sort_keys=above.sort,
        limit=above.limit,
    )


# ---------------------------------------------------------------- fix hint

_LITERAL = re.compile(r"'(?:[^']|'')*'")
_OP = r"(?<![-<>=!~#@&|*+/%^])(?P<op><=|>=|=|<|>)(?![-<>=~#@&|])"
# The plan prints (col)::text for every cast to text, but an index on the
# plain column serves it only for varchar (checked: not char(n), name, enums
# or numbers), so the cast is accepted only for known varchar columns.
_TEXT_CAST = r"(?P<cast>::text)?"
_LEFT_OPERAND = re.compile(rf"\)*{_TEXT_CAST}\)*\s*{_OP}\s")
_RIGHT_OPERAND = re.compile(rf"\)*{_TEXT_CAST}\)*(?:$|\s+(?:AND|OR)\b)")
_BEFORE_RIGHT = re.compile(rf"{_OP}\s+\(*$")
_ANY_CAST = re.compile(r"\)*::(?P<text>text\b)?")


def _strip_literals(condition: str) -> str:
    return _LITERAL.sub("''", condition)


def _unquote(name: str) -> str:
    if name.startswith('"'):
        return name[1:-1].replace('""', '"')
    return name


def _column_refs(text: str, alias: str) -> list[re.Match[str]]:
    quoted = '"' + alias.replace('"', '""') + '"'
    names = "|".join(re.escape(a) for a in dict.fromkeys((alias, quoted)))
    pattern = rf'(?<![\w$."])(?:{names})\.(?P<col>"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'
    return list(re.finditer(pattern, _strip_literals(text)))


def condition_columns(condition: str, alias: str) -> set[str]:
    """The unquoted names of the columns of ``alias`` the condition mentions."""
    tree = predicates.parse(condition)
    if tree is not None:
        return {predicates.unquote(ref.name) for ref, _ in _tree_refs(tree, alias)}
    return {_unquote(ref["col"]) for ref in _column_refs(condition, alias)}


def _casts_to_text(condition: object) -> bool:
    if not isinstance(condition, str):
        return False
    tree = predicates.parse(condition)
    if tree is None:
        return ")::text" in condition
    return any(
        isinstance(node, predicates.Cast) and node.type_name == "text"
        for node, _ in predicates.walk(tree)
    )


def _inside_function(text: str, start: int) -> bool:
    depth = 0
    for i in range(start - 1, -1, -1):
        char = text[i]
        if char == ")":
            depth += 1
        elif char == "(":
            if depth:
                depth -= 1
            elif i and (text[i - 1].isalnum() or text[i - 1] == "_"):
                return True
    return False


def _operator(text: str, start: int, end: int, cast_ok: bool) -> str | None:
    """The comparison a plain column reference takes part in, if any."""
    if _inside_function(text, start):
        return None
    before, after = text[:start], text[end:]
    match = None
    if before.endswith("("):
        match = _LEFT_OPERAND.match(after)
        op = match["op"] if match else None
    if match is None:
        right = _BEFORE_RIGHT.search(before)
        match = _RIGHT_OPERAND.match(after) if right else None
        op = right["op"] if right and match else None
    if match is None or (match["cast"] and not cast_ok):
        return None
    return op


_COMPARISON_OPS = frozenset({"=", "<", "<=", ">", ">="})
# Calls that wrap a column without hiding it from an index (a column inside
# them is still no plain comparison, but needs no expression index either).
# "row" is a row comparison, (a, b) > ($1, $2), which a btree on (a, b) serves.
_NOT_FUNCTIONS = frozenset({"any", "all", "some", "array", "row"})


def _in_function(parents: Sequence[predicates.Expr]) -> bool:
    return any(
        isinstance(p, predicates.Call) and p.name.lower() not in _NOT_FUNCTIONS
        for p in parents
    )


def _tree_refs(
    tree: predicates.Expr, alias: str
) -> list[tuple[predicates.Col, tuple[predicates.Expr, ...]]]:
    return [
        (node, parents)
        for node, parents in predicates.walk(tree)
        if isinstance(node, predicates.Col)
        and predicates.unquote(node.qualifier) == alias
    ]


def _tree_comparisons(
    tree: predicates.Expr, alias: str, varchar: Collection[str]
) -> tuple[str, set[str]] | None:
    refs = _tree_refs(tree, alias)
    columns = {ref.name for ref, _ in refs}
    if len(columns) != 1:
        return None
    column = columns.pop()
    cast_ok = _unquote(column) in varchar
    ops: set[str] = set()
    for ref, parents in refs:
        # NOT (x = ANY (...)), the NOT IN of PostgreSQL 17 and newer, is no
        # comparison an index on x serves.
        if _in_function(parents) or any(isinstance(p, predicates.Not) for p in parents):
            return None
        operand: predicates.Expr = ref
        above = list(parents)
        if above and isinstance(above[-1], predicates.Cast):
            cast = above.pop()
            if not (cast.type_name == "text" and cast_ok):  # type: ignore[union-attr]
                return None
            operand = cast
        parent = above[-1] if above else None
        if (
            not isinstance(parent, predicates.Op)
            or parent.op not in _COMPARISON_OPS
            or not (parent.left is operand or parent.right is operand)
        ):
            return None
        ops.add(parent.op)
    return column, ops


def _comparisons(
    condition: str, alias: str, varchar: Collection[str]
) -> tuple[str, set[str]] | None:
    """(column, operators) when the condition compares exactly one plain column.

    The parsed condition decides; text matching is the fallback for syntax
    the parser does not know.
    """
    tree = predicates.parse(condition)
    if tree is not None:
        return _tree_comparisons(tree, alias, varchar)
    return _regex_comparisons(condition, alias, varchar)


def _regex_comparisons(
    condition: str, alias: str, varchar: Collection[str]
) -> tuple[str, set[str]] | None:
    text = _strip_literals(condition)
    refs = _column_refs(text, alias)
    columns = {ref["col"] for ref in refs}
    if len(columns) != 1:
        return None
    column = columns.pop()
    cast_ok = _unquote(column) in varchar
    ops: set[str] = set()
    for ref in refs:
        op = _operator(text, ref.start(), ref.end(), cast_ok)
        if op is None:
            return None
        ops.add(op)
    return column, ops


def hint_column(
    condition: str, alias: str, varchar: Collection[str] = frozenset()
) -> str | None:
    """The one column an index on the scanned table would serve, if obvious.

    ``varchar`` names the columns of the table that are varchar, whose
    ``::text`` cast an index on the plain column still serves.
    """
    found = _comparisons(condition, alias, varchar)
    return None if found is None else found[0]


def equality_column(
    condition: str, alias: str, varchar: Collection[str] = frozenset()
) -> str | None:
    """The column when the condition is only equalities (= or = ANY) on it."""
    found = _comparisons(condition, alias, varchar)
    if found is None or found[1] != {"="}:
        return None
    return found[0]


def _needs_expression(condition: str, alias: str, varchar: Collection[str]) -> bool:
    """True when a column is wrapped in a function or cast an index cannot see."""
    tree = predicates.parse(condition)
    if tree is None:
        return _regex_needs_expression(condition, alias, varchar)
    for ref, parents in _tree_refs(tree, alias):
        if _in_function(parents):
            return True
        parent = parents[-1] if parents else None
        if isinstance(parent, predicates.Cast) and not (
            parent.type_name == "text" and _unquote(ref.name) in varchar
        ):
            return True
    return False


def _regex_needs_expression(
    condition: str, alias: str, varchar: Collection[str]
) -> bool:
    text = _strip_literals(condition)
    for ref in _column_refs(text, alias):
        if _inside_function(text, ref.start()):
            return True
        cast = _ANY_CAST.match(text, ref.end())
        if cast and not (cast["text"] and _unquote(ref["col"]) in varchar):
            return True
    return False


# PostgreSQL's reserved keywords, which need quotes as table names.
_RESERVED_WORDS = """
    all analyse analyze and any array as asc asymmetric authorization binary both
    case cast check collate collation column concurrently constraint create cross
    current_catalog current_date current_role current_schema current_time
    current_timestamp current_user default deferrable desc distinct do else end
    except false fetch for foreign freeze from full grant group having ilike in
    initially inner intersect into is isnull join lateral leading left like limit
    localtime localtimestamp natural not notnull null offset on only or order outer
    overlaps placing primary references returning right select session_user
    similar some symmetric system_user table tablesample then to trailing true
    union unique user using variadic verbose when where window with
"""
_RESERVED = frozenset(_RESERVED_WORDS.split())
_PLAIN_NAME = re.compile(r"[a-z_][a-z0-9_$]*")


def quote_ident(name: str) -> str:
    if _PLAIN_NAME.fullmatch(name) and name not in _RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'


def table_label(issue: ScanIssue) -> str:
    """The table as the fix hint should name it."""
    if issue.schema is None or not issue.show_schema:
        return quote_ident(issue.table)
    return f"{quote_ident(issue.schema)}.{quote_ident(issue.table)}"


def is_ignored(issue: ScanIssue, patterns: Iterable[str]) -> bool:
    return any(table_matches(p, issue.schema, issue.table) for p in patterns)


def scan_key(issue: ScanIssue) -> tuple[object, ...]:
    """What makes scans the same finding (see ``select_findings``)."""
    if issue.column is not None:
        return (issue.schema, issue.table, issue.column)
    return (issue.schema, issue.table, issue.node, issue.kind, issue.condition)


def select_findings(
    scans: Sequence[ScanEvent], ignore: Iterable[str]
) -> list[ScanEvent]:
    """One event per missing index, first occurrence first, minus ignored.

    Scans are the same finding when they name the same column of the same
    table (the same query under two aliases), or else the same condition.
    """
    patterns = tuple(ignore)
    seen: set[tuple[object, ...]] = set()
    found: list[ScanEvent] = []
    for scan in scans:
        issue = scan.issue
        key = scan_key(issue)
        if key in seen or is_ignored(issue, patterns):
            continue
        seen.add(key)
        found.append(scan)
    return found


# ---------------------------------------------------------------- EXPLAIN


@dataclass(frozen=True, slots=True)
class ExplainError:
    message: str
    sqlstate: str | None = None


def _error_message(exc: Exception) -> str:
    detail = exc.args[0] if exc.args else exc
    if isinstance(detail, Mapping) and "M" in detail:  # pg8000 error fields
        return str(detail["M"])
    lines = str(detail).strip().splitlines()
    return lines[0] if lines else type(exc).__name__


@dataclass(frozen=True, slots=True)
class Explained:
    plan: Mapping[str, Any]
    current_schema: str | None
    leading: LeadingColumns = field(default_factory=dict)
    varchar: VarcharColumns = field(default_factory=dict)
    catalog_error: str | None = None
    # With an advisor: the issues of the plan, with their advice, found inside
    # the savepoint; None when the caller finds them itself.
    issues: tuple[ScanIssue, ...] | None = None
    advisor_notice: str | None = None
    # Issues left out because a partial index may serve the statement.
    partial_skipped: tuple[ScanIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class AdvisorOptions:
    """How run_explain() advises on the missing indexes it finds."""

    hypopg: str = "auto"  # one of hypopg.MODES
    server_major: int | None = None


class SavepointError(Exception):
    """The savepoint around EXPLAIN could not be opened or rolled back."""


def _sql_literal(text: str) -> str | None:
    # Only for names read from the plan; backslashes would depend on
    # standard_conforming_strings, so such names are not looked up.
    if "\\" in text or "\x00" in text:
        return None
    return "'" + text.replace("'", "''") + "'"


# EXPLAIN VERBOSE prints the schema of a temporary table as "pg_temp", but
# pg_namespace has "pg_temp_<n>" for it: the connection's own one is found
# with pg_my_temp_schema(). Such entries belong to one connection.
TEMP_SCHEMA = "pg_temp"
_SCHEMA_NAME = (
    f"CASE WHEN n.oid = pg_my_temp_schema() THEN '{TEMP_SCHEMA}' ELSE n.nspname END"
)


def _name_filter(names: Iterable[tuple[str, str]]) -> str | None:
    """A condition on ``n`` and ``c`` that matches these (schema, name) pairs."""
    pairs, temp = [], []
    for schema, name in sorted(names):
        schema_sql, name_sql = _sql_literal(schema), _sql_literal(name)
        if schema_sql is None or name_sql is None:
            continue
        if schema == TEMP_SCHEMA:
            temp.append(name_sql)
        else:
            pairs.append(f"({schema_sql}, {name_sql})")
    conditions = []
    if pairs:
        conditions.append(f"(n.nspname, c.relname) IN ({', '.join(pairs)})")
    if temp:
        conditions.append(
            f"(n.oid = pg_my_temp_schema() AND c.relname IN ({', '.join(temp)}))"
        )
    return " OR ".join(conditions) if conditions else None


def _name_arrays(
    names: Iterable[tuple[str, str]],
) -> tuple[list[str], list[str], list[str]]:
    """The names _name_filter() would keep, as the three arrays of a lookup.

    Schemas and relations are parallel arrays of ordinary (schema, name) pairs;
    the third holds the names of temporary relations, whose schema is the
    connection's own.
    """
    schemas: list[str] = []
    relations: list[str] = []
    temp: list[str] = []
    for schema, name in sorted(names):
        if _sql_literal(schema) is None or _sql_literal(name) is None:
            continue
        if schema == TEMP_SCHEMA:
            temp.append(name)
        else:
            schemas.append(schema)
            relations.append(name)
    return schemas, relations, temp


# A lookup: its SQL and, for asyncpg, the parameters of that SQL.
_Lookup = tuple[str, tuple[Any, ...]]

# asyncpg prepares and caches (100, least recently used first out) every
# distinct statement text it fetches rows for. The names must therefore not
# be literals in the text: with array parameters each lookup is one cached
# statement however many tables and indexes the suite touches.
_NAMES_ARRAYS = (
    "((n.nspname, c.relname) IN (SELECT * FROM unnest($1::text[], $2::text[])) "
    "OR (n.oid = pg_my_temp_schema() AND c.relname = ANY($3::text[])))"
)

_LEADING_LOOKUP = (
    f"SELECT {_SCHEMA_NAME}, c.relname, a.attname "
    "FROM pg_index i "
    "JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_am am ON am.oid = c.relam "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
    "WHERE {names} AND am.amname = 'btree'"
)

_VARCHAR_LOOKUP = (
    f"SELECT {_SCHEMA_NAME}, c.relname, a.attname "
    "FROM pg_attribute a "
    "JOIN pg_class c ON c.oid = a.attrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_type t ON t.oid = a.atttypid "
    "WHERE {names} "
    "AND a.attnum > 0 AND NOT a.attisdropped AND t.typname = 'varchar'"
)


# The indexes of tables, for the advisor: here ``c`` is the table.
_TABLE_INDEXES_LOOKUP = (
    f"SELECT {_SCHEMA_NAME}, c.relname, ic.relname, am.amname, i.indisvalid, "
    "ARRAY(SELECT CASE WHEN k.attnum = 0 THEN NULL ELSE "
    "(SELECT a.attname::text FROM pg_attribute a "
    "WHERE a.attrelid = i.indrelid AND a.attnum = k.attnum) END "
    "FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
    "WHERE k.ord <= i.indnkeyatts ORDER BY k.ord), "
    "pg_get_expr(i.indpred, i.indrelid) "
    "FROM pg_index i "
    "JOIN pg_class c ON c.oid = i.indrelid "
    "JOIN pg_class ic ON ic.oid = i.indexrelid "
    "JOIN pg_am am ON am.oid = ic.relam "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE {names}"
)


# The valid partial indexes of tables, with the columns of each table, so that
# the columns of an index predicate can be told from other words in it.
_PARTIAL_LOOKUP = (
    f"SELECT {_SCHEMA_NAME}, c.relname, ic.relname, "
    "pg_get_expr(i.indpred, i.indrelid), "
    "ARRAY(SELECT a.attname::text FROM pg_attribute a "
    "WHERE a.attrelid = i.indrelid AND a.attnum > 0 AND NOT a.attisdropped) "
    "FROM pg_index i "
    "JOIN pg_class c ON c.oid = i.indrelid "
    "JOIN pg_class ic ON ic.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE {names} AND i.indpred IS NOT NULL AND i.indisvalid"
)


# The partitioned table at the top of each partition. An index created on it
# is created on every partition, also on those attached later, so a finding
# on a partition names it instead (see _to_roots()). Inheritance children are
# not partitions: an index on their parent does not cover them.
_ROOT_LOOKUP = (
    f"SELECT {_SCHEMA_NAME}, c.relname, "
    f"CASE WHEN rn.oid = pg_my_temp_schema() THEN '{TEMP_SCHEMA}' "
    "ELSE rn.nspname END, r.relname "
    "FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_class r ON r.oid = pg_partition_root(c.oid) "
    "JOIN pg_namespace rn ON rn.oid = r.relnamespace "
    "WHERE {names} AND c.relispartition"
)


def _lookup(
    template: str, names: Iterable[tuple[str, str]], asyncpg: bool
) -> _Lookup | None:
    if asyncpg:
        schemas, relations, temp = _name_arrays(names)
        if not (schemas or temp):
            return None
        return template.format(names=_NAMES_ARRAYS), (schemas, relations, temp)
    condition = _name_filter(names)
    if condition is None:
        return None
    return template.format(names=f"({condition})"), ()


def _leading_lookup(
    names: Iterable[tuple[str, str]], asyncpg: bool = False
) -> _Lookup | None:
    # Only btree: a BRIN or GIN index on (a, b) serves b = $1 as well as a.
    return _lookup(_LEADING_LOOKUP, names, asyncpg)


def _varchar_lookup(
    tables: Iterable[tuple[str, str]], asyncpg: bool = False
) -> _Lookup | None:
    return _lookup(_VARCHAR_LOOKUP, tables, asyncpg)


def _partial_indexes(
    cursor: Any, tables: set[tuple[str, str]], catalog: Catalog | None, asyncpg: bool
) -> dict[tuple[str, str], tuple[partial.PartialIndex, ...]]:
    """The valid partial indexes of each table, read once per session.

    Like _read_catalog(), this takes the cache dict once, so a clear() in the
    middle only orphans what was read before it.
    """
    cache = catalog.partial if catalog is not None else {}
    known = {t: v for t in tables if (v := cache.get(t)) is not None}
    missing = tables - known.keys()
    found: dict[tuple[str, str], list[partial.PartialIndex]] = {t: [] for t in missing}
    for r in _rows(cursor, _lookup(_PARTIAL_LOOKUP, missing, asyncpg)):
        if r[3] is None:
            continue
        found.setdefault((str(r[0]), str(r[1])), []).append(
            partial.PartialIndex(
                str(r[0]),
                str(r[1]),
                str(r[2]),
                str(r[3]),
                frozenset(str(c) for c in r[4]),
            )
        )
    for table, indexes_ in found.items():
        known[table] = tuple(indexes_)
        if table[0] != TEMP_SCHEMA:
            cache[table] = known[table]
    return known


def partition_roots(
    cursor: Any, tables: set[tuple[str, str]], catalog: Catalog | None, asyncpg: bool
) -> dict[tuple[str, str], tuple[str, str]]:
    """The partitioned root of each of ``tables`` that is a partition.

    Read once per session, like _read_catalog(): the cache dict is taken once.
    """
    cache = catalog.roots if catalog is not None else {}
    known = {t: cache[t] for t in tables if t in cache}
    missing = tables - known.keys()
    found: dict[tuple[str, str], tuple[str, str] | None] = dict.fromkeys(missing)
    for r in _rows(cursor, _lookup(_ROOT_LOOKUP, missing, asyncpg)):
        found[(str(r[0]), str(r[1]))] = (str(r[2]), str(r[3]))
    for table, root in found.items():
        known[table] = root
        if table[0] != TEMP_SCHEMA:
            cache[table] = root
    return {table: root for table, root in known.items() if root is not None}


def _same_condition(issue: ScanIssue) -> str:
    """The condition with the scan's alias left out: partitions are scanned as
    events_1, events_2, ... with otherwise the same condition."""
    stripped = predicates.without_qualifier(issue.condition, issue.alias)
    return issue.condition if stripped is None else stripped


def to_roots(
    issues: Sequence[ScanIssue],
    roots: Mapping[tuple[str, str], tuple[str, str]],
    current_schema: str | None,
    varchar: VarcharColumns,
) -> tuple[tuple[ScanIssue, ...], dict[tuple[str, str], frozenset[str]]]:
    """Issues on partitions named after their partitioned root, one per root.

    Two partitions read in full for the same condition are one missing index
    on the root. ``varchar`` gains the roots, with the columns of their
    partitions.
    """
    current = current_schema or "public"
    widened = dict(varchar)
    out: list[ScanIssue] = []
    seen: set[tuple[object, ...]] = set()
    for issue in issues:
        root = roots.get((issue.schema or "", issue.table))
        if root is None:
            out.append(issue)
            continue
        widened.setdefault(
            root, varchar.get((issue.schema or "", issue.table), frozenset())
        )
        key = (root, issue.kind, issue.column, issue.expression, _same_condition(issue))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            replace(
                issue,
                schema=root[0],
                table=root[1],
                show_schema=root[0] != current,
            )
        )
    return tuple(out), widened


def _split_partial(
    cursor: Any, issues: Sequence[ScanIssue], catalog: Catalog | None, asyncpg: bool
) -> tuple[tuple[ScanIssue, ...], tuple[ScanIssue, ...]]:
    """(kept, skipped): issues on a table whose partial index the generic plan
    cannot rule out are skipped (see partial.py)."""
    tables = {(i.schema, i.table) for i in issues if i.schema}
    by_table = _partial_indexes(cursor, tables, catalog, asyncpg)
    kept: list[ScanIssue] = []
    skipped: list[ScanIssue] = []
    for issue in issues:
        found = by_table.get((issue.schema or "", issue.table))
        if found and partial.may_serve(found, issue.conditions, issue.alias):
            skipped.append(issue)
        else:
            kept.append(issue)
    return tuple(kept), tuple(skipped)


def _text_cast_tables(plan: Mapping[str, Any]) -> set[tuple[str, str]]:
    """(schema, table) of every scan whose conditions cast a column to text."""
    tables: set[tuple[str, str]] = set()

    def visit(node: object, heap: _Relation | None) -> None:
        if not isinstance(node, Mapping):
            return
        rel = _relation(node) or heap
        conditions = (node.get(k) for k in ("Filter", "Index Cond", "Recheck Cond"))
        if (
            rel is not None
            and rel.schema
            and any(_casts_to_text(c) for c in conditions)
        ):
            tables.add((rel.schema, rel.table))
        below = _relation(node) if node.get("Node Type") == "Bitmap Heap Scan" else heap
        for child in _children(node):
            visit(child, below)

    visit(plan, None)
    return tables


def _rows(cursor: Any, lookup: _Lookup | None) -> list[Any]:
    if lookup is None:
        return []
    sql, params = lookup
    cursor.execute(sql, *params) if params else cursor.execute(sql)
    return list(cursor.fetchall() or ())


def _read_catalog(
    cursor: Any,
    plan: Mapping[str, Any],
    catalog: Catalog,
    leading: bool,
    asyncpg: bool = False,
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], frozenset[str]]]:
    """Fill ``catalog`` for this plan; raises on a database error.

    ``catalogs.clear`` rebinds the cache dicts, and it can run at any moment,
    from another thread or from the garbage collector. The dicts are taken
    once, here, and only they are read and written: a clear in the middle
    leaves them orphaned, so rows read before it are never stored in the new
    cache. This call still answers from what it read; the next one reads the
    catalog again. No lock is held, so a clear that runs between this call's
    reads and its writes is the only case that costs one extra read.
    """
    leading_cache = catalog.leading
    varchar_cache = catalog.varchar
    names = index_names(plan) if leading else set()
    known_leading = {
        name: col for name, col in list(leading_cache.items()) if name in names
    }
    missing = names - known_leading.keys()
    found = {
        (str(r[0]), str(r[1])): str(r[2])
        for r in _rows(cursor, _leading_lookup(missing, asyncpg))
    }
    for name in missing:
        known_leading[name] = found.get(name)
        if name[0] != TEMP_SCHEMA:
            leading_cache[name] = found.get(name)
    tables = _text_cast_tables(plan)
    known = {t: v for t in tables if (v := varchar_cache.get(t)) is not None}
    missing_tables = tables - known.keys()
    columns: dict[tuple[str, str], set[str]] = {t: set() for t in missing_tables}
    for r in _rows(cursor, _varchar_lookup(missing_tables, asyncpg)):
        columns.setdefault((str(r[0]), str(r[1])), set()).add(str(r[2]))
    for table, names_ in columns.items():
        known[table] = frozenset(names_)
        if table[0] != TEMP_SCHEMA:
            varchar_cache[table] = known[table]
    leading_map = {name: col for name, col in known_leading.items() if col is not None}
    return leading_map, known


def _read_plan(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, list) and value and isinstance(value[0], Mapping):
        plan = value[0].get("Plan")
        if isinstance(plan, Mapping):
            return plan
    return None


# asyncpg prepares every statement it fetches rows for, and PostgreSQL then
# takes the $n inside EXPLAIN as parameters of the EXPLAIN. PL/pgSQL's
# EXECUTE parses its string like the simple query protocol, so a DO block
# (no parameters, sent with asyncpg's simple-protocol execute()) runs the
# EXPLAIN and keeps the plan in a setting local to the savepoint.
_PLAN_SETTING = "queryguard.plan"
_READ_PLAN = f"SELECT current_setting('{_PLAN_SETTING}')"


def _free_tag(name: str, text: str) -> str:
    """A dollar-quote tag that first occurs in ``text + tag`` at its end."""
    tag, number = f"${name}$", 0
    while (text + tag).find(tag) != len(text):
        number += 1
        tag = f"${name}_{number}$"
    return tag


def explain_block(sql: str) -> str:
    """A DO block that explains ``sql`` into the queryguard.plan setting."""
    explain = _EXPLAIN + sql
    sql_tag = _free_tag("qg_sql", explain)
    body = (
        "DECLARE queryguard_plan text; BEGIN "
        f"EXECUTE {sql_tag}{explain}{sql_tag} INTO queryguard_plan; "
        f"PERFORM set_config('{_PLAN_SETTING}', queryguard_plan, true); END"
    )
    do_tag = _free_tag("qg_do", body)
    return f"DO {do_tag}{body}{do_tag}"


class _AsyncpgCursor:
    """The DB-API cursor calls run_explain() makes, on a raw asyncpg connection.

    ``adapted`` is SQLAlchemy's adapted connection; its public run_async()
    awaits a call on the raw asyncpg connection from this sync listener.
    SELECTs go through fetch(), which prepares them and keeps them in
    asyncpg's statement cache, so their text must not vary: the plan and
    settings reads are constant, and the catalog lookups take their names as
    array parameters. Anything else goes through execute() without
    parameters, which uses the simple query protocol and prepares nothing.
    """

    def __init__(self, adapted: Any) -> None:
        self._adapted = adapted
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, *params: Any) -> None:
        if is_checked_statement(sql):
            rows = self._adapted.run_async(lambda conn: conn.fetch(sql, *params))
            self._rows = [tuple(row) for row in rows]
        else:
            self._adapted.run_async(lambda conn: conn.execute(sql))
            self._rows = []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def close(self) -> None:
        self._rows = []


def run_explain(
    dbapi_connection: Any,
    sql: str,
    *,
    leading: bool = False,
    catalog: Catalog | None = None,
    asyncpg: bool = False,
    advise: AdvisorOptions | None = None,
    skip_partial: bool = False,
) -> Explained | ExplainError:
    """Plan ``sql`` on the test's own connection without changing its state.

    With a ``catalog``, the varchar columns of tables whose conditions cast
    to text and, with ``leading``, the first key column of each btree index
    used with an index condition are read inside the same savepoint, unless
    ``catalog`` already has them; it is updated. A failed lookup leaves the
    plan usable and is returned as ``catalog_error``.
    Database errors from EXPLAIN come back as ExplainError. An error while
    opening or rolling back the savepoint raises SavepointError: the
    connection's state is then unknown, and the caller stops checking.
    With ``asyncpg``, ``dbapi_connection`` is SQLAlchemy's adapted asyncpg
    connection, and the EXPLAIN runs inside a DO block (see explain_block()).
    With ``advise``, the missing indexes of the plan are found here, inside the
    savepoint, and each gets a suggested index (see advisor.py), verified with
    hypopg when it is installed. A failure of the advisor leaves the issues
    without advice and is returned as ``advisor_notice``.
    With ``skip_partial``, the issues are found here as well, and those on a
    table with a partial index the generic plan cannot rule out come back as
    ``partial_skipped`` instead of in ``issues``.
    """
    cursor = _AsyncpgCursor(dbapi_connection) if asyncpg else dbapi_connection.cursor()
    try:
        undo = _begin(cursor, bool(getattr(dbapi_connection, "autocommit", False)))
        try:
            row: Any = None
            settings: Any = None
            error: ExplainError | None = None
            columns: dict[tuple[str, str], str] = {}
            varchar: dict[tuple[str, str], frozenset[str]] = {}
            catalog_error: str | None = None
            issues: tuple[ScanIssue, ...] | None = None
            advisor_notice: str | None = None
            skipped: tuple[ScanIssue, ...] = ()
            try:
                cursor.execute(_SETTINGS)
                settings = cursor.fetchone()
                row = _explain(cursor, sql, asyncpg)
            except Exception as exc:  # noqa: BLE001  # any database error from EXPLAIN
                error = ExplainError(
                    _error_message(exc), getattr(exc, "sqlstate", None)
                )
            plan = None if error else _read_plan(row[0] if row else None)
            if plan is not None and catalog is not None:
                try:
                    columns, varchar = _read_catalog(
                        cursor, plan, catalog, leading, asyncpg
                    )
                except Exception as exc:  # noqa: BLE001  # reported once by check()
                    catalog_error = _error_message(exc)
            if (
                plan is not None
                and (advise is not None or skip_partial)
                and catalog_error is None
            ):
                schema = settings[-1] if settings else None
                current = schema if isinstance(schema, str) else None
                issues = tuple(find_scans(plan, current, columns, varchar))
                tables = {(i.schema, i.table) for i in issues if i.schema}
                lookups_ok = True
                if tables:
                    try:
                        roots = partition_roots(cursor, tables, catalog, asyncpg)
                        issues, varchar = to_roots(issues, roots, current, varchar)
                    except Exception as exc:  # noqa: BLE001  # fail open: name the partitions
                        # the savepoint is aborted now: no more lookups in it
                        lookups_ok = False
                        advisor_notice = (
                            "partition lookup failed, findings name the partitions "
                            f"and get no advice: {_error_message(exc)}"
                        )
                if issues and skip_partial and lookups_ok:
                    try:
                        issues, skipped = _split_partial(
                            cursor, issues, catalog, asyncpg
                        )
                    except Exception as exc:  # noqa: BLE001  # fail open: keep the findings
                        advisor_notice = f"partial index lookup failed, findings kept: {_error_message(exc)}"
                if issues and advise is not None and lookups_ok:
                    try:
                        issues, notice = _advise(
                            cursor, sql, issues, varchar, catalog, advise, asyncpg
                        )
                        advisor_notice = advisor_notice or notice
                    except Exception as exc:  # noqa: BLE001  # fail open: no advice
                        advisor_notice = f"index advisor: no suggestions this time: {_error_message(exc)}"
        except Exception:
            # A bug after the savepoint opened: put the user's connection back
            # as it was (no open savepoint or transaction, no changed setting)
            # before the error stops the plugin.
            with contextlib.suppress(Exception):
                _manage(cursor, undo)
            raise
        _manage(cursor, undo)
    finally:
        cursor.close()
    if error is not None:
        return error
    if plan is None:
        return ExplainError(f"unexpected EXPLAIN output: {row!r:.100}")
    schema = settings[-1] if settings else None
    return Explained(
        plan,
        schema if isinstance(schema, str) else None,
        columns,
        varchar,
        catalog_error,
        issues,
        advisor_notice,
        skipped,
    )


def _explain(cursor: Any, sql: str, asyncpg: bool) -> Any:
    """Run EXPLAIN for ``sql`` and return the first row of its output."""
    if asyncpg:
        cursor.execute(explain_block(sql))
        cursor.execute(_READ_PLAN)
    elif _is_psycopg(cursor):
        # psycopg prepares a statement once a connection has run it five
        # times; EXPLAIN of a statement with $n placeholders and no parameters
        # then fails ("bind message supplies 0 parameters").
        cursor.execute(_EXPLAIN + sql, prepare=False)
    else:
        cursor.execute(_EXPLAIN + sql)
    return cursor.fetchone()


def _is_psycopg(cursor: Any) -> bool:
    return type(cursor).__module__.split(".", 1)[0] == "psycopg"


_SEQSCAN = "SELECT set_config('enable_seqscan', '{}', true)"


def _advise(
    cursor: Any,
    sql: str,
    issues: Sequence[ScanIssue],
    varchar: VarcharColumns,
    catalog: Catalog | None,
    advise: AdvisorOptions,
    asyncpg: bool,
) -> tuple[tuple[ScanIssue, ...], str | None]:
    """The issues with their advice; runs inside run_explain's savepoint."""
    tables = {(i.schema, i.table) for i in issues if i.schema}
    existing: dict[tuple[str, str], list[advisor.ExistingIndex]] = {}
    for r in _rows(cursor, _lookup(_TABLE_INDEXES_LOOKUP, tables, asyncpg)):
        columns = tuple(None if c is None else str(c) for c in r[5])
        existing.setdefault((str(r[0]), str(r[1])), []).append(
            advisor.ExistingIndex(
                str(r[2]),
                str(r[3]),
                bool(r[4]),
                columns,
                None if r[6] is None else str(r[6]),
            )
        )
    advised: list[ScanIssue] = []
    # (position in advised, advice) of every suggestion that can be verified,
    # then the partial alternatives
    plain: list[tuple[int, Advice, advisor.Candidate]] = []
    partial: dict[int, advisor.Candidate] = {}
    for issue in issues:
        analysis = None
        if issue.schema:
            analysis = advisor.analyse(
                issue, varchar.get((issue.schema, issue.table), frozenset())
            )
        if analysis is None or analysis.plain is None:
            advised.append(issue)
            continue
        label = table_label(issue)
        advice = advisor.make_advice(
            issue,
            analysis.plain,
            label,
            existing.get((issue.schema or "", issue.table), []),
        )
        advised.append(replace(issue, advice=advice))
        if advice.create_sql is not None:
            plain.append((len(advised) - 1, advice, analysis.plain))
            if analysis.partial is not None:
                partial[len(advised) - 1] = analysis.partial
    if not plain or advise.hypopg == "off":
        return tuple(advised), None
    probed: hypopg.Probe | None = catalog.hypopg if catalog is not None else None
    if probed is None:
        probed = hypopg.probe(cursor)
        if catalog is not None:
            catalog.hypopg = probed
    installed, available = probed
    create = installed is None and advise.hypopg == "on" and available is not None
    if installed is None and not create:
        return tuple(advised), hypopg.notice(probed, advise.hypopg, advise.server_major)

    def plan_with(seqscan: bool) -> Mapping[str, Any]:
        cursor.execute(_SEQSCAN.format("on" if seqscan else "off"))
        found = _read_plan(_explain(cursor, sql, asyncpg)[0])
        if found is None:
            raise ValueError("unreadable plan")
        return found

    ddls = [advice.create_sql or "" for _, advice, _ in plain]
    extra = [
        (position, advisor.create_sql(table_label(advised[position]), candidate))
        for position, candidate in partial.items()
    ]
    ddls += [ddl for _, ddl in extra]
    results = hypopg.verify(
        cursor, plan_with, ddls, asyncpg=asyncpg, create_extension=create
    )
    for (position, advice, _), result in zip(plain, results, strict=False):
        advised[position] = replace(
            advised[position], advice=replace(advice, verification=result)
        )
    for (position, ddl), result in zip(extra, results[len(plain) :], strict=True):
        full = advised[position].advice
        assert full is not None and full.verification is not None
        if _prefer_partial(full.verification, result):
            candidate = partial[position]
            advised[position] = replace(
                advised[position],
                advice=Advice(
                    keys=candidate.keys,
                    where=candidate.where,
                    create_sql=ddl,
                    alembic=advisor.alembic_snippet(advised[position], candidate),
                    verification=result,
                ),
            )
    return tuple(advised), None


def _prefer_partial(full: Verification, partial: Verification) -> bool:
    """A partial index replaces the full one when the planner still uses it and
    hypopg estimates it a fraction of the size."""
    return bool(
        full.used
        and partial.used
        and full.size
        and partial.size is not None
        and partial.size <= advisor.PARTIAL_SIZE_RATIO * full.size
    )


_SAVEPOINT_UNDO = [
    f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}",
    f"RELEASE SAVEPOINT {_SAVEPOINT}",
]

# SQLSTATE of "SAVEPOINT can only be used in transaction blocks"
_NO_ACTIVE_TRANSACTION = "25P01"


def _sqlstate(exc: BaseException) -> str | None:
    """The SQLSTATE of a driver error (asyncpg: sqlstate, pg8000: field C)."""
    code = getattr(exc, "sqlstate", None)
    if code is None and exc.args and isinstance(exc.args[0], Mapping):
        code = exc.args[0].get("C")
    return code if isinstance(code, str) else None


def _begin(cursor: Any, autocommit: bool) -> list[str]:
    """Open the isolation for EXPLAIN and return the statements that undo it.

    A savepoint leaves the connection's transaction as it is. ``autocommit``
    is the driver's setting, not the server's state: an application can open a
    transaction by hand with BEGIN on such a connection, and a ROLLBACK from
    here would end it. So the savepoint comes first on every connection, and
    only a connection with no transaction open (SAVEPOINT fails with
    SQLSTATE 25P01, which aborts nothing in autocommit mode) gets a
    transaction of its own.
    """
    try:
        cursor.execute(f"SAVEPOINT {_SAVEPOINT}")
    except Exception as exc:  # reported as a stopped check, unless a fallback
        if not autocommit or _sqlstate(exc) != _NO_ACTIVE_TRANSACTION:
            raise SavepointError(_error_message(exc)) from exc
        _manage(cursor, ["BEGIN"])
        return ["ROLLBACK"]
    return list(_SAVEPOINT_UNDO)


def _manage(cursor: Any, statements: list[str]) -> None:
    for statement in statements:
        try:
            cursor.execute(statement)
        except Exception as exc:  # reported as a stopped check
            raise SavepointError(_error_message(exc)) from exc


# ---------------------------------------------------------------- listener

# Why the index check stopped for this session, after a broken savepoint.
_stopped: str | None = None


def swap_stopped(new: str | None) -> str | None:
    """Replace the stopped state and return the previous one (per session)."""
    global _stopped
    old, _stopped = _stopped, new
    return old


# Why the catalog lookups stopped for this session, after one failed.
_catalog_error: str | None = None


def swap_catalog_error(new: str | None) -> str | None:
    """Replace the catalog lookup state and return the previous one."""
    global _catalog_error
    old, _catalog_error = _catalog_error, new
    return old


# The asyncpg engines whose database has no PL/pgSQL, found by the
# first failed EXPLAIN on each. PL/pgSQL belongs to a database, and a suite
# can use several; sync engines and the other asyncpg engines are still
# checked.
NO_PLPGSQL_NOTICE = "asyncpg statements were not checked: PL/pgSQL is not installed"
_PLPGSQL_MISSING = 'language "plpgsql" does not exist'
_UNDEFINED_OBJECT = "42704"


def _is_plpgsql_missing(error: ExplainError) -> bool:
    # The SQLSTATE does not depend on lc_messages. It is shared with a missing
    # type or operator in the user's statement, so the message must still name
    # the language (translations keep its quoted name).
    if error.sqlstate == _UNDEFINED_OBJECT:
        return "plpgsql" in error.message.lower()
    return error.message == _PLPGSQL_MISSING


class EngineSet:
    """Engines held weakly: an id is reused once its engine is collected.

    An engine that cannot be weakly referenced is kept by id instead.
    """

    def __init__(self) -> None:
        self._engines: weakref.WeakSet[Any] = weakref.WeakSet()
        self._ids: set[int] = set()

    def add(self, engine: Any) -> None:
        try:
            self._engines.add(engine)
        except TypeError:
            self._ids.add(id(engine))

    def __contains__(self, engine: Any) -> bool:
        try:
            weakref.ref(engine)
        except TypeError:  # WeakSet answers False for these, not TypeError
            return id(engine) in self._ids
        return engine in self._engines

    def __len__(self) -> int:
        return len(self._engines) + len(self._ids)


def engine_key(engine: Any) -> object:
    """A hashable stand-in for an engine that is never equal to a later one."""
    try:
        return weakref.ref(engine)
    except TypeError:
        return id(engine)


_no_plpgsql = EngineSet()


def swap_no_plpgsql(new: EngineSet) -> EngineSet:
    """Replace the engines without PL/pgSQL and return the previous ones."""
    global _no_plpgsql
    old, _no_plpgsql = _no_plpgsql, new
    return old


# One wording for the notice and the help texts (plugin.py repeats it).
SUPPORTED_DATABASES = (
    "PostgreSQL 16 or newer, SQLite, MySQL (tested on 8.4) and MariaDB (tested on 11.8)"
)


def unsupported_reason(dialect: Any) -> str | None:
    """Why statements from this dialect cannot be checked, or None.

    SQLite, MySQL and MariaDB never get here: the listener hands them to their
    own checks, so the notice for another name lists every supported database.
    """
    name = getattr(dialect, "name", "?")
    if name != "postgresql":
        return (
            f"{name} statements were not checked: "
            f"the index check supports {SUPPORTED_DATABASES}"
        )
    driver = getattr(dialect, "driver", "?")
    if getattr(dialect, "is_async", False) and driver != ASYNCPG:
        return (
            f"statements from the async driver {driver} were not checked: "
            "the index check supports asyncpg and sync drivers only"
        )
    version = getattr(dialect, "server_version_info", None)
    if version and isinstance(version[0], int) and version[0] < MIN_SERVER_VERSION:
        shown = ".".join(str(part) for part in version)
        return (
            f"PostgreSQL {shown} statements were not checked: "
            f"the index check needs PostgreSQL {MIN_SERVER_VERSION} or newer"
        )
    paramstyle = getattr(dialect, "paramstyle", "?")
    if to_numbered("", paramstyle) is None:
        return (
            f"statements from {driver} were not checked: "
            f"its paramstyle {paramstyle!r} is not supported by the index check"
        )
    return None


def version_notice(dialect: Any) -> str | None:
    """The notice for a PostgreSQL server newer than the tested ones, or None."""
    version = getattr(dialect, "server_version_info", None)
    if (
        getattr(dialect, "name", None) != "postgresql"
        or not version
        or not isinstance(version[0], int)
        or version[0] <= MAX_TESTED_VERSION
    ):
        return None
    shown = ".".join(str(part) for part in version)
    return (
        f"PostgreSQL {shown} is newer than the versions the index check is "
        f"tested on ({MIN_SERVER_VERSION} to {MAX_TESTED_VERSION}): its "
        "statements are checked, but a finding may be wrong or missing; "
        "please report one that is"
    )


def _advisor_options(
    targets: Sequence[QueryCollector], version: Sequence[object] | None
) -> AdvisorOptions | None:
    wanting = [target for target in targets if target.index_advisor]
    if not wanting:
        return None
    major = version[0] if version and isinstance(version[0], int) else None
    return AdvisorOptions(wanting[0].hypopg, major)


def check(
    conn: Any, statement: str, executemany: bool, targets: Sequence[QueryCollector]
) -> None:
    """Explain one executed statement for the collectors that want it."""
    global _stopped, _catalog_error
    if executemany or not is_checked_statement(statement):
        return
    dialect = conn.dialect
    is_async = getattr(dialect, "is_async", False)
    reason = _stopped or unsupported_reason(dialect)
    if reason is None and is_async and conn.engine in _no_plpgsql:
        reason = NO_PLPGSQL_NOTICE
    if reason is not None:
        for target in targets:
            target.index_notices[reason] = None
        return
    # Literals stay: a partial index may serve one value and not another.
    key = (engine_key(conn.engine), collapse_lists(statement))
    fresh = [target for target in targets if key not in target.explained]
    if not fresh:
        return
    notice = version_notice(dialect)
    for target in fresh:
        target.explained.add(key)
        if notice is not None:
            target.index_notices[notice] = None
    numbered = to_numbered(
        statement,
        dialect.paramstyle,
        unescape_quoted=getattr(dialect, "driver", None) != "pg8000",
    )
    assert numbered is not None  # unsupported_reason() checked the paramstyle
    version = getattr(dialect, "server_version_info", None)
    catalog = None
    if _catalog_error is None:
        catalog = catalogs.for_engine(conn.engine)
    else:
        for target in targets:
            target.index_notices[_catalog_error] = None
    try:
        outcome = run_explain(
            conn.connection.dbapi_connection,
            numbered,
            leading=not version or version[0] < SKIP_SCAN_VERSION,
            catalog=catalog,
            asyncpg=is_async,
            advise=_advisor_options(targets, version),
            skip_partial=True,
        )
    except SavepointError as exc:
        # The test's transaction may be broken now; N+1 detection and budgets
        # still work, but no more EXPLAIN runs on anyone's connection.
        _stopped = (
            "the index check stopped for the rest of the session: "
            f"it could not open or roll back its savepoint: {exc}"
        )
        for target in targets:
            target.index_notices[_stopped] = None
        return
    if isinstance(outcome, Explained) and outcome.catalog_error is not None:
        _catalog_error = (
            "the index check stopped reading the catalog for the rest of the "
            f"session: {outcome.catalog_error}; trailing index columns and "
            "casts to text are not judged"
        )
        for target in targets:
            target.index_notices[_catalog_error] = None
    if isinstance(outcome, ExplainError) and is_async and _is_plpgsql_missing(outcome):
        _no_plpgsql.add(conn.engine)
        for target in targets:
            target.index_notices[NO_PLPGSQL_NOTICE] = None
        return
    if isinstance(outcome, ExplainError):
        error = (outcome.message, statement)
        for target in fresh:
            if error not in target.explain_errors:
                target.explain_errors.append(error)
        return
    if outcome.advisor_notice is not None:
        for target in targets:
            target.index_notices[outcome.advisor_notice] = None
    if outcome.partial_skipped:
        tables = {table_label(issue) for issue in outcome.partial_skipped}
        for target in fresh:
            target.partial_skipped.append(tuple(sorted(tables)))
    found: Sequence[ScanIssue] | None = outcome.issues
    if found is None:
        found = find_scans(
            outcome.plan, outcome.current_schema, outcome.leading, outcome.varchar
        )
    if not found:
        return
    site = find_callsite()
    ignores = blocks.current()
    for issue in found:
        scan = ScanEvent(issue, statement, site, ignores)
        for target in fresh:
            target.scans.append(scan)
