"""Missing-index check for SQLite: plan each statement with EXPLAIN QUERY PLAN.

See docs/index-check-sqlite.md for the rules and their limits. SQLite has no
cost model to switch around, so the check reads the plan the way a person
would: a ``SCAN`` of a table that the statement filters or joins on is a table
read in full, and an ``AUTOMATIC INDEX`` is an index SQLite had to build at
query time. Everything that is not clearly one of those is left alone.

EXPLAIN QUERY PLAN prepares the statement but never steps through it, so it
changes neither data nor the transaction state of the connection.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

from . import advisor, blocks
from .callsite import find_callsite
from .collector import QueryCollector
from .detectors import collapse_lists
from .events import Advice, IndexKey, ScanEvent, ScanIssue
from .indexes import engine_key, quote_ident
from .sqltext import skip_prefix

DIALECT = "sqlite"
_EXPLAIN = "EXPLAIN QUERY PLAN "
_VERB = re.compile(r"(?:SELECT|WITH|UPDATE|DELETE)\b", re.IGNORECASE)
_CONDITION_WORD = re.compile(r"\b(?:WHERE|ON)\b", re.IGNORECASE)


def is_checked_statement(sql: str) -> bool:
    """SELECT, WITH (also WITH ... UPDATE and DELETE), UPDATE and DELETE."""
    start = skip_prefix(sql)
    return start is not None and _VERB.match(sql, start) is not None


# ---------------------------------------------------------------- tokens


class Token(NamedTuple):
    kind: str  # word, qid, str, num, param, op, punct, other
    text: str  # identifiers are unquoted
    start: int
    end: int

    @property
    def upper(self) -> str:
        return self.text.upper() if self.kind == "word" else ""


_TOKEN = re.compile(
    r"""
      (?P<space>\s+)
    | (?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z))
    | (?P<str>'(?:[^']|'')*'?)
    | (?P<qid>"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\])
    | (?P<param>\?\d*|[:@$][^\W\d]\w*)
    | (?P<num>\d[\w.]*|\.\d\w*)
    | (?P<word>[^\W\d]\w*)
    | (?P<op><=|>=|==|<>|!=|\|\||<<|>>|[=<>+\-*/%&|~])
    | (?P<punct>[(),.;])
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    for match in _TOKEN.finditer(sql):
        kind = match.lastgroup
        if kind in ("space", "comment") or kind is None:
            continue
        text = match.group()
        if kind == "qid":
            quote = text[0]
            inner = text[1:-1]
            text = inner if quote == "[" else inner.replace(quote * 2, quote)
        tokens.append(Token(kind, text, match.start(), match.end()))
    return tokens


_RESERVED = frozenset(
    [
        "ALL",
        "AND",
        "AS",
        "ASC",
        "BETWEEN",
        "BY",
        "CASE",
        "CAST",
        "COLLATE",
        "CROSS",
        "CURRENT_DATE",
        "CURRENT_TIME",
        "CURRENT_TIMESTAMP",
        "DELETE",
        "DESC",
        "DISTINCT",
        "ELSE",
        "END",
        "ESCAPE",
        "EXCEPT",
        "EXISTS",
        "FALSE",
        "FILTER",
        "FIRST",
        "FOLLOWING",
        "FROM",
        "FULL",
        "GLOB",
        "GROUP",
        "HAVING",
        "IN",
        "INDEXED",
        "INNER",
        "INSERT",
        "INTERSECT",
        "INTO",
        "IS",
        "ISNULL",
        "JOIN",
        "LAST",
        "LEFT",
        "LIKE",
        "LIMIT",
        "MATCH",
        "MATERIALIZED",
        "NATURAL",
        "NO",
        "NOT",
        "NOTNULL",
        "NULL",
        "NULLS",
        "OF",
        "OFFSET",
        "ON",
        "OR",
        "ORDER",
        "OUTER",
        "OVER",
        "PARTITION",
        "PRECEDING",
        "RANGE",
        "RECURSIVE",
        "REGEXP",
        "RETURNING",
        "RIGHT",
        "ROWS",
        "SELECT",
        "SET",
        "THEN",
        "TRUE",
        "UNBOUNDED",
        "UNION",
        "UPDATE",
        "USING",
        "VALUES",
        "WHEN",
        "WHERE",
        "WINDOW",
        "WITH",
    ]
)
# Words that continue an operand into a larger expression.
_CONTINUES = frozenset(
    [
        "IS",
        "IN",
        "LIKE",
        "GLOB",
        "BETWEEN",
        "NOT",
        "COLLATE",
        "ESCAPE",
        "ISNULL",
        "NOTNULL",
        "MATCH",
        "REGEXP",
    ]
)
_VALUE_WORDS = frozenset({"NULL", "TRUE", "FALSE"})
_COMPARE = frozenset({"=", "==", "<", "<=", ">", ">="})
_RANGE = frozenset({"<", "<=", ">", ">="})
# What may stand in front of an operand of a comparison, so the operand is the
# whole side of it and not the tail of an expression such as ``a + b = ?``.
_BEFORE_OPERAND = frozenset({"AND", "OR", "WHERE", "ON"})


def _is_name(token: Token) -> bool:
    return token.kind == "qid" or (
        token.kind == "word" and token.upper not in _RESERVED
    )


# ---------------------------------------------------------------- statement


def _select_ids(tokens: Sequence[Token]) -> list[int]:
    """For each token, the number of the SELECT it belongs to (0: none yet)."""
    ids: list[int] = []
    current = {0: 0}
    depth = 0
    counter = 0
    for token in tokens:
        if token.kind == "punct" and token.text == "(":
            ids.append(current[depth])
            depth += 1
            current[depth] = current[depth - 1]
            continue
        if token.kind == "punct" and token.text == ")":
            if depth:
                del current[depth]
                depth -= 1
            ids.append(current[depth])
            continue
        if token.upper == "SELECT":
            counter += 1
            current[depth] = counter
        ids.append(current[depth])
    return ids


@dataclass(slots=True)
class _Scope:
    """What the statement's FROM clauses name."""

    # select number -> lowercase names (aliases or tables) its FROM clauses bind
    in_select: dict[int, set[str]] = field(default_factory=dict)
    select_ids: list[int] = field(default_factory=list)

    # lowercase name or alias -> lowercase table, None for a subquery, a CTE or
    # a name that means different things in different parts of the statement
    names: dict[str, str | None] = field(default_factory=dict)

    def add(self, name: str, table: str | None) -> None:
        key = name.lower()
        if key in self.names and self.names[key] != table:
            table = None
        self.names[key] = table


def _skip_group(tokens: Sequence[Token], i: int) -> int:
    """Index after the parenthesis that opens at ``i``."""
    depth = 0
    n = len(tokens)
    while i < n:
        text = tokens[i].text if tokens[i].kind == "punct" else ""
        if text == "(":
            depth += 1
        elif text == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _punct(token: Token | None, text: str) -> bool:
    return token is not None and token.kind == "punct" and token.text == text


def _table_ref(tokens: Sequence[Token], i: int, scope: _Scope) -> int:
    """Read one table reference of a FROM clause; return the next index."""
    n = len(tokens)
    if i >= n:
        return i
    start = i
    table: str | None = None
    if _punct(tokens[i], "("):
        i = _skip_group(tokens, i)
    elif _is_name(tokens[i]):
        table = tokens[i].text
        i += 1
        if i + 1 < n and _punct(tokens[i], ".") and _is_name(tokens[i + 1]):
            table = tokens[i + 1].text  # schema.table
            i += 2
        if i < n and _punct(tokens[i], "("):  # a table-valued function
            table = None
            i = _skip_group(tokens, i)
    else:
        return i
    alias: str | None = None
    if i < n and tokens[i].upper == "AS":
        i += 1
    if i < n and _is_name(tokens[i]):
        alias = tokens[i].text
        i += 1
    bound = alias if alias is not None else table
    if bound is not None:
        scope.in_select.setdefault(scope.select_ids[start], set()).add(bound.lower())
    if alias is not None:
        scope.add(alias, table)
    elif table is not None:
        scope.add(table, table)
    return i


def _read_scope(tokens: Sequence[Token]) -> _Scope:
    scope = _Scope(select_ids=_select_ids(tokens))
    n = len(tokens)
    for i, token in enumerate(tokens):
        word = token.upper
        previous = tokens[i - 1].upper if i else ""
        if word == "FROM" and previous != "DISTINCT":
            j = _table_ref(tokens, i + 1, scope)
            while j < n and _punct(tokens[j], ","):
                j = _table_ref(tokens, j + 1, scope)
        elif word == "JOIN":
            _table_ref(tokens, i + 1, scope)
        elif word == "UPDATE" and not previous.isalpha():
            j = i + 1
            if j + 1 < n and tokens[j].upper == "OR":  # UPDATE OR REPLACE
                j += 2
            _table_ref(tokens, j, scope)
        elif word == "AS" and i >= 1:
            # WITH name AS (...) or WITH name(cols) AS (...): a CTE hides a
            # table of the same name.
            j = i - 1
            if _punct(tokens[j], ")"):
                while j >= 0 and not _punct(tokens[j], "("):
                    j -= 1
                j -= 1
            before = tokens[j - 1] if j >= 1 else None
            if (
                j >= 0
                and _is_name(tokens[j])
                and before is not None
                and (before.upper in ("WITH", "RECURSIVE") or _punct(before, ","))
                and i + 1 < n
                and (
                    _punct(tokens[i + 1], "(")
                    or tokens[i + 1].upper in ("MATERIALIZED", "NOT")
                )
            ):
                scope.names[tokens[j].text.lower()] = None
    return scope


@dataclass(frozen=True, slots=True)
class Atom:
    """A comparison that an index could serve: ``column op value``."""

    key: str | None  # plan name of the table, lowercase; None when unknown
    qualifier: str | None
    column: str
    op: str  # "eq" or "range"
    expression: bool  # the column sits inside a function call
    other: tuple[str | None, str] | None  # the other side, when it is a column
    text: str
    select: int = 0  # the SELECT the comparison belongs to


@dataclass(slots=True)
class _Operand:
    kind: str  # value, ref, func
    start: int  # token indexes: start inclusive, end exclusive
    end: int
    qualifier: str | None = None
    column: str | None = None


def _left_operand(tokens: Sequence[Token], k: int, lo: int) -> _Operand | None:
    """The operand that ends just before the operator at ``k``."""
    j = k - 1
    if j < lo:
        return None
    token = tokens[j]
    operand: _Operand | None = None
    if token.kind in ("param", "num", "str") or token.upper in _VALUE_WORDS:
        operand = _Operand("value", j, k)
    elif _is_name(token):
        qualifier = None
        start = j
        if j - 2 >= lo and _punct(tokens[j - 1], ".") and _is_name(tokens[j - 2]):
            qualifier = tokens[j - 2].text
            start = j - 2
            if j - 4 >= lo and _punct(tokens[j - 3], ".") and _is_name(tokens[j - 4]):
                start = j - 4  # schema.table.column
        operand = _Operand("ref", start, k, qualifier, token.text)
    elif (
        _punct(token, ")")
        and j - 3 >= lo
        and _is_name(tokens[j - 1])
        and _punct(tokens[j - 2], "(")
        and _is_name(tokens[j - 3])
        and not _punct(tokens[j - 4] if j - 4 >= 0 else None, ".")
    ):  # func(column)
        operand = _Operand("func", j - 3, k, None, tokens[j - 1].text)
    if operand is None:
        return None
    before = tokens[operand.start - 1] if operand.start - 1 >= lo else None
    if before is None or before.upper in _BEFORE_OPERAND or _punct(before, "("):
        return operand
    return None


def _right_operand(tokens: Sequence[Token], k: int) -> _Operand | None:
    """The operand that starts just after the operator at ``k``."""
    i = k + 1
    n = len(tokens)
    if i >= n:
        return None
    token = tokens[i]
    if _is_name(token) and token.upper not in _VALUE_WORDS:
        j = i + 1
        qualifier = None
        column = token.text
        if j + 1 < n and _punct(tokens[j], ".") and _is_name(tokens[j + 1]):
            qualifier, column = token.text, tokens[j + 1].text
            j += 2
            if j + 1 < n and _punct(tokens[j], ".") and _is_name(tokens[j + 1]):
                qualifier, column = column, tokens[j + 1].text
                j += 2
        if j < n and _punct(tokens[j], "("):
            return _Operand("value", i, j)  # a function call
        after = tokens[j] if j < n else None
        if after is not None and (after.kind == "op" or after.upper in _CONTINUES):
            return _Operand("value", i, j)  # part of a larger expression
        return _Operand("ref", i, j, qualifier, column)
    return _Operand("value", i, i + 1)


def _atoms(
    sql: str, tokens: Sequence[Token], select_ids: Sequence[int]
) -> tuple[list[Atom], bool]:
    """The comparisons in WHERE and ON clauses, and whether any OR is there."""
    atoms: list[Atom] = []
    has_or = False
    clause: dict[int, bool] = {0: False}
    depth = 0
    n = len(tokens)
    for k, token in enumerate(tokens):
        word = token.upper
        if token.kind == "punct" and token.text == "(":
            depth += 1
            nxt = tokens[k + 1].upper if k + 1 < n else ""
            subquery = nxt in ("SELECT", "WITH", "VALUES")
            clause[depth] = clause[depth - 1] and not subquery
            continue
        if token.kind == "punct" and token.text == ")":
            if depth:
                del clause[depth]
                depth -= 1
            continue
        if word in ("WHERE", "ON"):
            clause[depth] = True
            continue
        if word in (
            "SELECT", "FROM", "GROUP", "ORDER", "LIMIT", "HAVING", "SET", "JOIN",
            "UNION", "INTERSECT", "EXCEPT", "WINDOW", "RETURNING", "VALUES", "USING",
        ):  # fmt: skip
            clause[depth] = False
            continue
        if not clause.get(depth, False):
            continue
        if word == "OR":
            has_or = True
        op = _comparison(tokens, k)
        if op is None:
            continue
        left = _left_operand(tokens, k, 0)
        if token.upper in ("IN", "BETWEEN"):
            right = _list_operand(tokens, k)
        else:
            right = _right_operand(tokens, k)
        atoms.extend(
            replace(atom, select=select_ids[k])
            for atom in _make_atoms(sql, tokens, k, op, left, right)
        )
    return atoms, has_or


def _comparison(tokens: Sequence[Token], k: int) -> str | None:
    """ "eq" or "range" for a comparison operator at ``k`` an index can serve."""
    token = tokens[k]
    if token.kind == "op":
        if token.text in _RANGE:
            return "range"
        return "eq" if token.text in ("=", "==") else None
    word = token.upper
    if word == "IS":
        nxt = tokens[k + 1].upper if k + 1 < len(tokens) else ""
        return None if nxt == "NOT" else "eq"
    if word == "IN":
        return "eq"
    if word == "BETWEEN":
        return "range"
    return None


def _list_operand(tokens: Sequence[Token], k: int) -> _Operand | None:
    """The right side of IN (...) or BETWEEN a AND b, as a value."""
    i = k + 1
    if i >= len(tokens):
        return None
    if tokens[k].upper == "IN":
        if _punct(tokens[i], "("):
            return _Operand("value", i, _skip_group(tokens, i))
        return _Operand("value", i, min(i + 1, len(tokens)))
    # BETWEEN: the bounds are values; the end is after "AND bound".
    j = i
    while j < len(tokens) and tokens[j].upper != "AND":
        j += 1
    return _Operand("value", i, min(j + 2, len(tokens)))


def _make_atoms(
    sql: str,
    tokens: Sequence[Token],
    k: int,
    op: str,
    left: _Operand | None,
    right: _Operand | None,
) -> list[Atom]:
    kind = "range" if op == "range" else "eq"
    if left is None or right is None:
        return []
    text_end = tokens[right.end - 1].end if right.end > right.start else tokens[k].end
    made: list[Atom] = []
    sides = [(left, right, left.start, text_end)]
    if right.kind == "ref" and left.kind in ("value", "ref"):
        sides.append((right, left, left.start, text_end))
    for side, other, start, end in sides:
        if side.kind not in ("ref", "func") or side.column is None:
            continue
        made.append(
            Atom(
                None,
                side.qualifier,
                side.column,
                kind,
                side.kind == "func",
                (other.qualifier, other.column or "")
                if other.kind in ("ref", "func")
                else None,
                sql[tokens[start].start : end],
            )
        )
    return made


# ---------------------------------------------------------------- the plan


class _Access(NamedTuple):
    id: int
    parent: int
    verb: str  # SCAN or SEARCH
    key: str  # the name the plan uses: the table or its alias
    table: str | None  # the table, when the plan names both (older SQLite)
    using: str  # what follows USING, or "" for a plain scan


_ACCESS = re.compile(r"(?P<verb>SCAN|SEARCH)\s+(?:TABLE\s+)?(?P<rest>.+)", re.DOTALL)
_MARKERS = (" USING ", " VIRTUAL TABLE INDEX ")
_INDEX_SCAN = ("INDEX ", "COVERING INDEX ", "INTEGER PRIMARY KEY", "PRIMARY KEY")


def _parse_access(row: Sequence[Any]) -> _Access | None:
    if len(row) < 4 or not isinstance(row[3], str):
        return None
    match = _ACCESS.fullmatch(row[3].strip())
    if match is None:
        return None
    rest = match["rest"]
    cut = len(rest)
    for marker in _MARKERS:
        found = rest.find(marker)
        if 0 <= found < cut:
            cut = found
    name, using = rest[:cut].strip(), rest[cut:].strip()
    table = None
    if " AS " in name:
        table, _, name = name.partition(" AS ")
    try:
        return _Access(int(row[0]), int(row[1]), match["verb"], name, table, using)
    except (TypeError, ValueError):
        return None


def _kind(access: _Access) -> str | None:
    """scan, index-scan or automatic: the accesses worth a look; else None."""
    using = access.using.removeprefix("USING ").strip()
    if access.verb == "SEARCH":
        return "automatic" if using.startswith("AUTOMATIC") else None
    if not access.using:
        return "scan"
    if access.using.startswith("USING ") and using.startswith(_INDEX_SCAN):
        return "index-scan"
    return None  # VIRTUAL TABLE INDEX and shapes this code does not know


def _walked_index(access: _Access) -> str | None:
    """What a ``SCAN t USING ...`` row walks: "index ix_t_a" or "primary key"."""
    using = access.using.removeprefix("USING ").strip()
    for prefix in ("COVERING INDEX ", "INDEX "):
        if using.startswith(prefix):
            name = using.removeprefix(prefix).strip()
            return f"index {name}" if name else None
    if using.endswith("PRIMARY KEY"):
        return "primary key"
    return None


_AUTOMATIC_COLUMNS = re.compile(r"\((?P<terms>[^()]*)\)(?:\s+LEFT-JOIN)?\s*$")
_TERM = re.compile(r"\s*(?P<name>.+?)\s*(?:=|>=|<=|>|<|IS)\s*\?\s*", re.IGNORECASE)


def _automatic_columns(access: _Access) -> list[str] | None:
    match = _AUTOMATIC_COLUMNS.search(access.using)
    if match is None:
        return None
    columns = []
    for term in re.split(r"\bAND\b", match["terms"]):
        found = _TERM.fullmatch(term)
        if found is None:
            return None
        columns.append(found["name"])
    return columns or None


class Schema:
    """What the plan analysis asks the database about its tables."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self._tables: dict[str, str | None] = {}
        self._columns: dict[str, list[str]] = {}
        self._leading: dict[str, frozenset[str]] = {}

    def _query(self, sql: str, *params: str) -> list[tuple[Any, ...]]:
        self._cursor.execute(sql, params)
        return [_row(r) for r in self._cursor.fetchall()]

    def table(self, name: str) -> str | None:
        """The declared name of an ordinary table, or None (view, virtual, internal)."""
        key = name.lower()
        if key not in self._tables:
            found = None
            if not key.startswith("sqlite_"):
                for master in ("sqlite_temp_master", "sqlite_master"):
                    rows = self._query(
                        f"SELECT name, sql FROM {master} "  # fixed names
                        "WHERE type = 'table' AND lower(name) = ?",
                        key,
                    )
                    if rows:
                        sql = rows[0][1] or ""
                        if not sql.lstrip().upper().startswith("CREATE VIRTUAL"):
                            found = str(rows[0][0])
                        break
            self._tables[key] = found
        return self._tables[key]

    def columns(self, table: str) -> list[str]:
        if table not in self._columns:
            rows = self._query(f"PRAGMA table_info({_pragma_name(table)})")
            self._columns[table] = [str(r[1]) for r in rows]
        return self._columns[table]

    def leading(self, table: str) -> frozenset[str]:
        """Lowercase first columns of the table's whole indexes and its rowid alias."""
        if table not in self._leading:
            names: set[str] = set()
            info = self._query(f"PRAGMA table_info({_pragma_name(table)})")
            keys = [r for r in info if r[5]]
            if len(keys) == 1 and str(keys[0][2]).upper() == "INTEGER":
                names.add(str(keys[0][1]).lower())
            for index in self._query(f"PRAGMA index_list({_pragma_name(table)})"):
                if index[4]:  # a partial index serves only some rows
                    continue
                first = self._query(f"PRAGMA index_info({_pragma_name(str(index[1]))})")
                if first and first[0][2] is not None:
                    names.add(str(first[0][2]).lower())
            self._leading[table] = frozenset(names)
        return self._leading[table]


def _pragma_name(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _row(row: Any) -> tuple[Any, ...]:
    """A result row as a tuple, whatever the connection's row_factory made of it."""
    values = tuple(row.values()) if isinstance(row, Mapping) else tuple(row)
    return tuple(
        v.decode("utf-8", "replace") if isinstance(v, bytes | bytearray) else v
        for v in values
    )


def _distinct(names: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for name in names:
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def _advice(
    issue: ScanIssue, columns: Sequence[str] | None, alembic: bool
) -> ScanIssue:
    """The issue with a CREATE INDEX (and an Alembic call) for its columns.

    SQLite needs a name in CREATE INDEX, unlike PostgreSQL, so the suggestion
    is always built here and never left to the PostgreSQL fallback text.
    """
    if not columns:
        return issue
    keys = tuple(IndexKey(quote_ident(c)) for c in columns)
    candidate = advisor.Candidate(keys)
    listed = ", ".join(key.column for key in keys)
    name = advisor.index_name(issue.table, keys)
    advice = Advice(
        keys=keys,
        create_sql=f"CREATE INDEX {name} ON {quote_ident(issue.table)} ({listed})",
        alembic=advisor.alembic_snippet(issue, candidate) if alembic else None,
    )
    return replace(issue, advice=advice)


def analyse(
    rows: Sequence[Sequence[Any]], sql: str, schema: Schema, alembic: bool = True
) -> list[ScanIssue]:
    """The tables the plan reads in full although the statement narrows them.

    One issue per table. Anything this does not understand gives no issue.
    """
    accesses = [a for a in map(_parse_access, rows) if a is not None]
    candidates = [(a, _kind(a)) for a in accesses]
    candidates = [(a, k) for a, k in candidates if k is not None]
    if not candidates:
        return []
    automatic = any(k == "automatic" for _, k in candidates)
    if not automatic and _CONDITION_WORD.search(sql) is None:
        return []  # nothing is filtered, so a scan is the right plan
    tokens = tokenize(sql)
    scope = _read_scope(tokens)

    # The tables behind the plan's names; views, CTEs and subqueries drop out.
    tables: dict[str, str] = {}
    for access in accesses:
        key = access.key.lower()
        if access.table is not None:
            real = schema.table(access.table)
        elif key in scope.names:
            target = scope.names[key]
            real = None if target is None else schema.table(target)
        else:
            real = schema.table(access.key)
        if real is not None:
            tables[key] = real
    columns = {
        key: {c.lower(): c for c in schema.columns(t)} for key, t in tables.items()
    }

    atoms, has_or = _atoms(sql, tokens, scope.select_ids)
    by_key: dict[str, list[Atom]] = {key: [] for key in tables}
    for atom in atoms:
        owner = _resolve(atom.qualifier, atom.column, tables, columns)
        here = scope.in_select.get(atom.select)
        if owner is None or here is None or owner not in here:
            continue  # not a column of a table this part of the statement reads
        other = atom.other
        if other is not None:
            other_key = _resolve(other[0], other[1], tables, columns)
            if other_key == owner:
                continue  # two columns of one table: no index helps
            if other_key is not None and other_key not in here:
                # The other column comes from an enclosing SELECT, so it is
                # a value here, fixed for each run of the subquery.
                atom = replace(atom, other=None)
        by_key[owner].append(atom)

    groups: dict[int, list[int]] = {}
    for access in accesses:
        groups.setdefault(access.parent, []).append(access.id)
    per_key: dict[str, list[_Access]] = {}
    for access in accesses:
        per_key.setdefault(access.key.lower(), []).append(access)

    issues: list[ScanIssue] = []
    seen: set[str] = set()
    for access, kind in candidates:
        key = access.key.lower()
        table = tables.get(key)
        if table is None or table in seen:
            continue
        same = per_key[key]
        if len(same) > 1 and any(a.verb == "SEARCH" and _kind(a) is None for a in same):
            continue  # one name, several uses: the scan may be the unfiltered one
        group = groups[access.parent]
        outermost = len(group) > 1 and access.id == min(group)
        issue = _issue(
            access, kind or "", table, by_key.get(key, []), columns[key],
            schema.leading(table), outermost, has_or, len(same) > 1, alembic,
        )  # fmt: skip
        if issue is not None:
            seen.add(table)
            issues.append(issue)
    return issues


def _resolve(
    qualifier: str | None,
    column: str,
    tables: Mapping[str, str],
    columns: Mapping[str, Mapping[str, str]],
) -> str | None:
    if qualifier is not None:
        key = qualifier.lower()
        return key if key in tables and column.lower() in columns[key] else None
    owners = [key for key in tables if column.lower() in columns[key]]
    return owners[0] if len(owners) == 1 else None


def _issue(
    access: _Access,
    kind: str,
    table: str,
    atoms: Sequence[Atom],
    declared: Mapping[str, str],
    leading: frozenset[str],
    outermost: bool,
    has_or: bool,
    repeated: bool,
    alembic: bool,
) -> ScanIssue | None:
    if kind == "automatic":
        cols = _automatic_columns(access)
        shown = [declared.get(c.lower(), c) for c in cols] if cols else None
        joined = any(a.other is not None for a in atoms)
        terms = _AUTOMATIC_COLUMNS.search(access.using)
        condition = terms["terms"] if cols and terms else ""
        issue = ScanIssue(
            None,
            table,
            f"SEARCH {access.using}".strip(),
            "join condition" if joined else "filter",
            condition,
            ", ".join(shown) if shown else None,
            show_schema=False,
        )
        return _advice(issue, shown, alembic)
    # A join condition is no filter for the table the join starts from.
    own = [a for a in atoms if a.other is None or not outermost]
    if kind == "index-scan":
        own = [a for a in own if a.op == "eq" and not a.expression]
    plain = [a for a in own if not a.expression and a.column.lower() not in leading]
    expression = [a for a in own if a.expression]
    if not plain and not expression:
        # No filter, or every filtered column leads an index already: the
        # planner chose the scan, and a new index would not be missing.
        return None
    chosen = plain or expression
    condition = " AND ".join(_distinct(a.text for a in chosen))
    index_columns: list[str] | None = None
    if plain and not repeated:
        eq = _distinct(a.column for a in plain if a.op == "eq")
        rng = [a.column for a in plain if a.op == "range"]
        names = _distinct([*eq, *rng[:1]])
        if not has_or or len(names) == 1:
            index_columns = names
    shown = (
        [declared.get(c.lower(), c) for c in index_columns] if index_columns else None
    )
    node = "SCAN" + (f" {access.using.removeprefix('USING ')}" if access.using else "")
    issue = ScanIssue(
        None,
        table,
        node,
        "filter" if any(a.other is None for a in chosen) else "join condition",
        condition,
        ", ".join(shown) if shown else None,
        show_schema=False,
        expression=not plain,
        index=_walked_index(access) if kind == "index-scan" else None,
    )
    return _advice(issue, shown, alembic)


# ---------------------------------------------------------------- listener


class _ExplainFailed(Exception):
    pass


def _message(exc: Exception) -> str:
    lines = str(exc).strip().splitlines()
    return lines[0] if lines else type(exc).__name__


def explain(
    dbapi_connection: Any, statement: str, parameters: Any, alembic: bool = True
) -> list[ScanIssue]:
    """Plan ``statement`` on the test's own connection and analyse the plan.

    EXPLAIN QUERY PLAN does not run the statement, so there is no savepoint
    and nothing to roll back. Raises _ExplainFailed for a database error.
    """
    cursor = dbapi_connection.cursor()
    try:
        try:
            cursor.execute(_EXPLAIN + statement, parameters if parameters else ())
            rows = [_row(r) for r in cursor.fetchall()]
            return analyse(rows, statement, Schema(cursor), alembic)
        except Exception as exc:  # any database error from EXPLAIN
            raise _ExplainFailed(_message(exc)) from exc
    finally:
        cursor.close()


def check(
    conn: Any,
    statement: str,
    parameters: Any,
    executemany: bool,
    targets: Sequence[QueryCollector],
) -> None:
    """Explain one executed statement for the collectors that want it."""
    if executemany or not is_checked_statement(statement):
        return
    key = (DIALECT, engine_key(conn.engine), collapse_lists(statement))
    fresh = [target for target in targets if key not in target.explained]
    if not fresh:
        return
    for target in fresh:
        target.explained.add(key)
    try:
        alembic = any(target.index_advisor for target in fresh)
        issues = explain(
            conn.connection.dbapi_connection, statement, parameters, alembic
        )
    except _ExplainFailed as exc:
        error = (str(exc), statement)
        for target in fresh:
            if error not in target.explain_errors:
                target.explain_errors.append(error)
        return
    if not issues:
        return
    site = find_callsite()
    ignores = blocks.current()
    for issue in issues:
        scan = ScanEvent(issue, statement, site, ignores)
        for target in fresh:
            target.scans.append(scan)
