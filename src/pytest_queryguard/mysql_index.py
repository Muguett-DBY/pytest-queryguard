"""Missing-index check for MySQL and MariaDB: read the plan of EXPLAIN.

See docs/index-check-mysql.md for the rules and their limits. The check runs
the tabular ``EXPLAIN`` (the same columns on MySQL 5.7 and later and on
MariaDB) with the statement's real bound parameters and reads one row per
table of the plan: ``type`` ALL is a table read in full, ``index`` a whole
index read. A scan is reported when the statement filters or joins on a
column that does not lead any index of the table. Everything that is not
clearly that is left alone.

EXPLAIN plans SELECT, UPDATE and DELETE without running them, so the check
changes neither data nor the transaction of the connection. ``ANALYZE`` is
never used: it runs the statement.

The statement text is read with the small reader of the SQLite check
(sqlite_index.py): which WHERE and ON comparisons belong to a table is not in
the tabular plan.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from . import advisor, blocks
from . import sqlite_index as reader
from .callsite import find_callsite
from .collector import QueryCollector, note_index_first_run, note_index_reruns
from .detectors import collapse_lists
from .events import Advice, IndexKey, ScanEvent, ScanIssue
from .indexes import engine_key, quote_ident

DIALECTS = ("mysql", "mariadb")
DIALECT = "mysql"  # the dialect part of the key of an explained statement
_EXPLAIN = "EXPLAIN "
UNVERIFIED_NOTICE = (
    "index check: the MySQL and MariaDB index suggestions are derived from the "
    "statement and the existing indexes; neither server can try an index "
    "before it exists, so they are not verified"
)

# ---------------------------------------------------------------- tokens

_TOKEN = re.compile(
    r"""
      (?P<space>\s+)
    | (?P<comment>--\s[^\n]*|--$|\#[^\n]*|/\*.*?(?:\*/|\Z))
    | (?P<str>'(?:[^'\\]|\\.|'')*'?|"(?:[^"\\]|\\.|"")*"?)
    | (?P<qid>`(?:[^`]|``)*`?)
    | (?P<param>\?|%s|%\([^)]*\)s|@@?[\w.$]+)
    | (?P<num>\d[\w.]*|\.\d\w*)
    | (?P<word>[^\W\d]\w*)
    | (?P<op><=>|<=|>=|==|<>|!=|\|\||&&|<<|>>|:=|[=<>+\-*/%&|~^!])
    | (?P<punct>[(),.;])
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)
_PLACEHOLDER = re.compile(r"%s|%\([^)]*\)s")


def tokenize(sql: str) -> list[reader.Token]:
    tokens: list[reader.Token] = []
    for match in _TOKEN.finditer(sql):
        kind = match.lastgroup
        if kind in ("space", "comment") or kind is None:
            continue
        text = match.group()
        if kind == "qid":
            text = text[1:-1] if text.endswith("`") and len(text) > 1 else text[1:]
            text = text.replace("``", "`")
        tokens.append(reader.Token(kind, text, match.start(), match.end()))
    return tokens


def _strip_hints(tokens: list[reader.Token]) -> list[reader.Token]:
    """Without index hints (``FORCE INDEX (...)``); STRAIGHT_JOIN read as JOIN."""
    out: list[reader.Token] = []
    i, n = 0, len(tokens)
    while i < n:
        token = tokens[i]
        if (
            token.upper in ("USE", "FORCE", "IGNORE")
            and i + 1 < n
            and tokens[i + 1].upper in ("INDEX", "KEY")
        ):
            j = i + 2
            while j < n and not reader._punct(tokens[j], "("):
                j += 1  # FOR JOIN, FOR ORDER BY, FOR GROUP BY
            i = reader._skip_group(tokens, j) if j < n else n
            continue
        if token.upper == "STRAIGHT_JOIN":
            token = token._replace(kind="word", text="JOIN")
        out.append(token)
        i += 1
    return out


def display_sql(sql: str) -> str:
    """The statement with ``%s`` placeholders shown as ``?``."""
    return _PLACEHOLDER.sub("?", sql).replace("%%", "%")


_BARE = re.compile(r"[a-z_][a-z0-9_]*")
# Reserved in MySQL and not in PostgreSQL's list that quote_ident knows.
_MYSQL_RESERVED = frozenset(
    ["key", "keys", "index", "range", "rank", "row", "rows", "read", "write", "lock"]
)


def quote_name(name: str) -> str:
    """An identifier for MySQL: bare when plain, in backticks otherwise."""
    if (
        _BARE.fullmatch(name)
        and quote_ident(name) == name
        and name not in _MYSQL_RESERVED
    ):
        return name
    return "`" + name.replace("`", "``") + "`"


# ---------------------------------------------------------------- catalog


def _row(row: Any) -> tuple[Any, ...]:
    return reader._row(row)


class Schema:
    """What the plan analysis asks the database about its tables."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self._database: str | None = None
        self._tables: dict[str, str | None] = {}
        self._columns: dict[str, list[str]] = {}
        self._leading: dict[str, frozenset[str]] = {}

    def _query(self, sql: str, *params: str) -> list[dict[str, Any]]:
        """Rows as dictionaries with lowercase column names."""
        self._cursor.execute(sql, params or None)
        names = [str(d[0]).lower() for d in self._cursor.description or ()]
        return [
            dict(zip(names, _row(r), strict=False)) for r in self._cursor.fetchall()
        ]

    def database(self) -> str | None:
        if self._database is None:
            self._cursor.execute("SELECT DATABASE()")
            rows = self._cursor.fetchall()
            self._database = str(_row(rows[0])[0]) if rows and _row(rows[0])[0] else ""
        return self._database or None

    def table(self, name: str) -> str | None:
        """The declared name of a base table of the current database, or None.

        Views, temporary tables and tables of other databases are not judged.
        """
        key = name.lower()
        if key not in self._tables:
            rows = self._query(
                "SELECT table_name AS table_name, table_type AS table_type "
                "FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = %s",
                name,
            )
            found = None
            for row in rows:
                if str(row.get("table_type", "")).upper() == "BASE TABLE":
                    found = str(row["table_name"])
                    break
            self._tables[key] = found
        return self._tables[key]

    def columns(self, table: str) -> list[str]:
        if table not in self._columns:
            rows = self._query(
                "SELECT column_name AS column_name "
                "FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s "
                "ORDER BY ordinal_position",
                table,
            )
            self._columns[table] = [str(r["column_name"]) for r in rows]
        return self._columns[table]

    def leading(self, table: str) -> frozenset[str]:
        """Lowercase first columns of the table's usable indexes, primary key included.

        FULLTEXT and SPATIAL indexes do not serve comparisons, an invisible
        (MySQL) or ignored (MariaDB) index is not used, and a functional
        index has no column.
        """
        if table not in self._leading:
            quoted = "`" + table.replace("`", "``") + "`"
            names: set[str] = set()
            for row in self._query(f"SHOW INDEX FROM {quoted}"):
                if (
                    str(row.get("seq_in_index")) != "1"
                    or row.get("column_name") is None
                ):
                    continue
                if str(row.get("index_type", "")).upper() in ("FULLTEXT", "SPATIAL"):
                    continue
                if str(row.get("visible", "YES")).upper() == "NO":
                    continue
                if str(row.get("ignored", "NO")).upper() == "YES":
                    continue
                names.add(str(row["column_name"]).lower())
            self._leading[table] = frozenset(names)
        return self._leading[table]


# ---------------------------------------------------------------- the plan


@dataclass(frozen=True, slots=True)
class Access:
    """One row of the tabular EXPLAIN."""

    id: int | None
    select_type: str
    key: str  # the name the plan uses: the table or its alias
    type: str  # lowercase: all, index, ref, const, ...
    index: str | None  # the index in use (the ``key`` column)
    extra: str


def parse_rows(rows: Sequence[Mapping[str, Any]]) -> list[Access]:
    """The table rows of the plan; rows this code cannot read are dropped."""
    accesses: list[Access] = []
    for row in rows:
        lowered = {str(k).lower(): v for k, v in row.items()}
        table, kind = lowered.get("table"), lowered.get("type")
        if not isinstance(table, str) or not isinstance(kind, str):
            continue  # "Impossible WHERE" and the like have no table
        if table.startswith("<"):
            continue  # <derivedN>, <subqueryN>, <unionN,M>
        number = lowered.get("id")
        index = lowered.get("key")
        extra = lowered.get("extra")
        accesses.append(
            Access(
                number if isinstance(number, int) else None,
                str(lowered.get("select_type") or ""),
                table,
                kind.lower(),
                index if isinstance(index, str) else None,
                extra if isinstance(extra, str) else "",
            )
        )
    return accesses


def _kind(access: Access) -> str | None:
    """scan or index-scan: the accesses worth a look; else None."""
    if access.select_type.upper() == "UNION RESULT":
        return None
    if access.type == "all":
        return "scan"
    if access.type == "index":
        return "index-scan"
    return None  # ref, eq_ref, const, range, fulltext and shapes not known here


def _joined(access: Access) -> str:
    """ ", in a join buffer" when the plan joins this table without an index."""
    return ", in a join buffer" if "join buffer" in access.extra.lower() else ""


def _advice(
    issue: ScanIssue, columns: Sequence[str] | None, alembic: bool
) -> ScanIssue:
    """The issue with a CREATE INDEX (and an Alembic call) for its columns."""
    if not columns:
        return issue
    keys = tuple(IndexKey(quote_ident(c)) for c in columns)
    candidate = advisor.Candidate(keys)
    listed = ", ".join(quote_name(c) for c in columns)
    name = advisor.index_name(issue.table, keys)
    advice = Advice(
        keys=keys,
        create_sql=f"CREATE INDEX {name} ON {quote_name(issue.table)} ({listed})",
        alembic=advisor.alembic_snippet(issue, candidate) if alembic else None,
    )
    return replace(issue, advice=advice)


def analyse(
    rows: Sequence[Mapping[str, Any]],
    sql: str,
    schema: Schema,
    alembic: bool = True,
) -> list[ScanIssue]:
    """The tables the plan reads in full although the statement narrows them.

    One issue per table. Anything this does not understand gives no issue.
    """
    accesses = parse_rows(rows)
    candidates = [(a, _kind(a)) for a in accesses]
    candidates = [(a, k) for a, k in candidates if k is not None]
    if not candidates:
        return []
    text = display_sql(sql)
    tokens = _strip_hints(tokenize(text))
    if not any(t.upper in ("WHERE", "ON") for t in tokens):
        return []  # nothing is filtered, so a scan is the right plan
    scope = reader._read_scope(tokens)
    _drop_other_databases(tokens, scope, schema)

    # The tables behind the plan's names; views, CTEs and subqueries drop out.
    tables: dict[str, str] = {}
    for access in accesses:
        key = access.key.lower()
        target = scope.names.get(key)
        real = schema.table(target) if target is not None else None
        if real is not None:
            tables[key] = real
    if not any(a.key.lower() in tables for a, _ in candidates):
        return []
    columns = {
        key: {c.lower(): c for c in schema.columns(t)} for key, t in tables.items()
    }

    atoms, has_or = reader._atoms(text, tokens, scope.select_ids)
    by_key: dict[str, list[reader.Atom]] = {key: [] for key in tables}
    for atom in atoms:
        owner = reader._resolve(atom.qualifier, atom.column, tables, columns)
        here = scope.in_select.get(atom.select)
        if owner is None or here is None or owner not in here:
            continue  # not a column of a table this part of the statement reads
        other = atom.other
        if other is not None:
            other_key = reader._resolve(other[0], other[1], tables, columns)
            if other_key == owner:
                continue  # two columns of one table: no index helps
            if other_key is not None and other_key not in here:
                # The other column comes from an enclosing SELECT, so it is
                # a value here, fixed for each run of the subquery.
                atom = replace(atom, other=None)
        by_key[owner].append(atom)

    first: dict[tuple[int | None, str], int] = {}
    for position, access in enumerate(accesses):
        first.setdefault((access.id, access.select_type), position)
    per_key: dict[str, list[Access]] = {}
    for access in accesses:
        if access.select_type.upper() != "UNION RESULT":
            per_key.setdefault(access.key.lower(), []).append(access)

    issues: list[ScanIssue] = []
    seen: set[str] = set()
    for position, access in enumerate(accesses):
        kind = _kind(access)
        key = access.key.lower()
        table = tables.get(key)
        if kind is None or table is None or table in seen:
            continue
        same = per_key[key]
        if len(same) > 1 and any(_kind(a) is None for a in same):
            continue  # one name, several uses: the scan may be the unfiltered one
        siblings = [a for a in accesses if a.id == access.id and a.id is not None]
        outermost = (
            len(siblings) > 1 and first[(access.id, access.select_type)] == position
        )
        issue = _issue(
            access, kind, table, by_key.get(key, []), columns[key],
            schema.leading(table), outermost, has_or, len(same) > 1, alembic,
        )  # fmt: skip
        if issue is not None:
            seen.add(table)
            issues.append(issue)
    return issues


def _drop_other_databases(
    tokens: Sequence[reader.Token], scope: reader._Scope, schema: Schema
) -> None:
    """Forget tables named as ``other_db.table``: the catalog is the current one."""
    n = len(tokens)
    for i, token in enumerate(tokens):
        if token.upper not in ("FROM", "JOIN", "UPDATE", "INTO"):
            continue
        j = i + 1
        if j + 2 < n and tokens[j + 1].text == "." and tokens[j + 1].kind == "punct":
            database, table = tokens[j].text, tokens[j + 2].text
            if database.lower() == (schema.database() or "").lower():
                continue
            bound = table
            k = j + 3
            if k < n and tokens[k].upper == "AS":
                k += 1
            if k < n and reader._is_name(tokens[k]):
                bound = tokens[k].text
            scope.names[bound.lower()] = None


def _issue(
    access: Access,
    kind: str,
    table: str,
    atoms: Sequence[reader.Atom],
    declared: Mapping[str, str],
    leading: frozenset[str],
    outermost: bool,
    has_or: bool,
    repeated: bool,
    alembic: bool,
) -> ScanIssue | None:
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
    condition = (" OR " if has_or else " AND ").join(
        reader._distinct(a.text for a in chosen)
    )
    index_columns: list[str] | None = None
    if plain and not repeated:
        eq = reader._distinct(a.column for a in plain if a.op == "eq")
        rng = [a.column for a in plain if a.op == "range"]
        names = reader._distinct([*eq, *rng[:1]])
        if not has_or or len(names) == 1:
            index_columns = names
    shown = (
        [declared.get(c.lower(), c) for c in index_columns] if index_columns else None
    )
    if kind == "scan":
        node = f"full table scan (type ALL{_joined(access)})"
    else:
        node = f"full index scan (type index, key {access.index or '?'})"
    issue = ScanIssue(
        None,
        table,
        node,
        "filter" if any(a.other is None for a in chosen) else "join condition",
        condition,
        ", ".join(shown) if shown else None,
        show_schema=False,
        expression=not plain,
    )
    return _advice(issue, shown, alembic)


# ---------------------------------------------------------------- listener


class _ExplainFailed(Exception):
    pass


def _message(exc: Exception) -> str:
    lines = str(exc).strip().splitlines()
    return lines[0] if lines else type(exc).__name__


# FOUND_ROWS() reports on the previous statement, and the catalog queries of
# the check would replace it, so such a statement is not explained.
_CALC_FOUND_ROWS = "SQL_CALC_FOUND_ROWS"


def _unbuffered(cursor: Any) -> bool:
    """Whether the user's own cursor still has rows to fetch from the server.

    Running another statement on the connection would discard them, so the
    check leaves such a statement (SSCursor) alone.
    """
    inner = getattr(cursor, "_cursor", cursor)
    result = getattr(inner, "_result", None)
    return bool(getattr(result, "unbuffered_active", False))


def explain(
    dbapi_connection: Any,
    statement: str,
    parameters: Any,
    alembic: bool = True,
    no_parameters: bool = False,
) -> list[ScanIssue]:
    """Plan ``statement`` on the test's own connection and analyse the plan.

    EXPLAIN does not run the statement, so there is no savepoint and nothing to
    roll back. Raises _ExplainFailed for a database error.

    PyMySQL formats the text with ``query % args`` unless ``args`` is None. The
    EXPLAIN gets the formatting the application's execution got: with the
    ``no_parameters`` execution option and no parameters the text went to the
    server as it is, and so does the EXPLAIN.
    """
    cursor = dbapi_connection.cursor()
    try:
        try:
            # SQLAlchemy honours no_parameters only when there are none
            raw = no_parameters and not parameters
            cursor.execute(_EXPLAIN + statement, None if raw else parameters)
            names = [str(d[0]) for d in cursor.description or ()]
            rows = [dict(zip(names, _row(r), strict=False)) for r in cursor.fetchall()]
            return analyse(rows, statement, Schema(cursor), alembic)
        except Exception as exc:  # any database error from EXPLAIN
            raise _ExplainFailed(_message(exc)) from exc
    finally:
        cursor.close()


def check(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    executemany: bool,
    targets: Sequence[QueryCollector],
    no_parameters: bool = False,
) -> None:
    """Explain one executed statement for the collectors that want it."""
    if executemany or not reader.is_checked_statement(statement):
        return
    if _unbuffered(cursor) or _CALC_FOUND_ROWS in statement.upper():
        return
    key = (DIALECT, engine_key(conn.engine), collapse_lists(statement))
    fresh = [target for target in targets if key not in target.explained]
    note_index_reruns(targets, fresh, key)
    if not fresh:
        return
    for target in fresh:
        target.explained.add(key)
    try:
        alembic = any(target.index_advisor for target in fresh)
        issues = explain(
            conn.connection.dbapi_connection,
            statement,
            parameters,
            alembic,
            no_parameters,
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
    note_index_first_run(fresh, key, ignores)
    for issue in issues:
        scan = ScanEvent(issue, display_sql(statement), site, ignores, key=key)
        for target in fresh:
            target.scans.append(scan)
            if issue.advice is not None:
                target.index_notices[UNVERIFIED_NOTICE] = None
