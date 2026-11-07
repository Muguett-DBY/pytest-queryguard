"""A patch that adds the loader option to the query that loaded the parents.

A ``relationship-load`` finding knows the line of the parent query and the
loader option that fixes it (``fix:`` in the report). When that line is
certain, this module turns the hint into a source change: the option goes on
the ``select(Entity)`` or ``session.query(Entity)`` call of that line (or of
the one assignment of the statement that line runs), and the loader is
imported if the module does not import it yet.

A wrong patch costs more trust than no patch, so every doubt means none:

- the file is inside the rootdir, UTF-8 with ``\\n`` or ``\\r\\n`` line ends
  (which the patch keeps), parses with ``ast``, and was not modified after
  the session started;
- the line belongs to the function the query ran in;
- the line is one statement with exactly one ``select(...)`` call, or
  ``.query(...)`` on something named like a session, with the parent class
  (``Order`` or ``models.Order``) as its only argument; the statement runs no
  other query and does not choose between expressions; ``select`` is
  SQLAlchemy's (or SQLModel's);
- that call is the statement the line runs, as it is: a ``select(...)`` with
  only methods that keep its entities is the first argument of a session's
  ``execute``/``scalars``/...; a ``session.query(...)`` with only such methods
  is run by ``.all()``/``.first()``/... or iterated (not inside a union, an
  exists, a subquery, a CTE or an insert, not passed to a helper, not turned
  into other columns or a textual statement);
- or the line runs a statement held in a local variable
  (``session.scalars(statement)``) that the function assigns once, on an
  earlier line, to such a ``select(...)`` and its methods, and reads nowhere
  else;
- the classes, modules and loaders the option names are module-level names
  that the function does not rebind; a loader missing from the module is
  added to its ``from sqlalchemy.orm import`` line, or on a new line after
  its imports;
- an ``.options(...)`` already in the chain gets the loader as one more
  argument, when its arguments are SQLAlchemy loader calls that name each
  relationship as ``Class.attribute`` (not a variable, a string or a helper)
  and none mentions a relationship of the option;
- a ``joinedload`` does not go on a statement with ``with_for_update``,
  ``group_by``, ``having``, ``distinct`` or a prefix or suffix, whose SQL
  the outer join would change.

Everything runs when a test's findings are made, never per statement.
"""

from __future__ import annotations

import ast
import difflib
import functools
import os
import re
import warnings
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from .detectors import RelationshipFinding
from .findings import Insertion, SourceFix
from .messages import loader_steps

_ORM = "sqlalchemy.orm"
# Modules whose ``select`` builds a statement that takes loader options.
_SELECT_MODULES = ("sqlalchemy", "sqlmodel")
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_NAMED = (*_SCOPES, ast.ExceptHandler, ast.MatchAs, ast.MatchStar)
# Session methods that run the statement given as their first argument.
# (``exec`` is SQLModel's.)
_EXECUTES = frozenset(
    {"execute", "exec", "scalars", "scalar", "stream", "stream_scalars"}
)
# Session methods that run a statement. On the parent line, one of them with
# an argument that is not the select() call means another query may have run.
_RUNS = _EXECUTES | {"get", "get_one", "merge", "refresh"}
# Select methods that keep the entities it loads and its loader options: a
# statement in a variable is followed only through these (not a union, a
# subquery or with_only_columns).
_KEEPS_ENTITIES = frozenset(
    {
        "where",
        "filter",
        "filter_by",
        "join",
        "outerjoin",
        "join_from",
        "outerjoin_from",
        "select_from",
        "order_by",
        "group_by",
        "having",
        "limit",
        "offset",
        "slice",
        "fetch",
        "distinct",
        "options",
        "execution_options",
        "params",
        "with_for_update",
        "correlate",
        "prefix_with",
        "suffix_with",
    }
)
# Query methods that keep its entities and loader options, and the methods
# that run it: a session.query() on the parent line is followed only through
# these (not with_entities, from_statement, a union, a subquery or .statement).
_QUERY_KEEPS = frozenset(
    {
        "filter",
        "filter_by",
        "where",
        "join",
        "outerjoin",
        "select_from",
        "order_by",
        "group_by",
        "having",
        "limit",
        "offset",
        "slice",
        "distinct",
        "options",
        "execution_options",
        "params",
        "with_for_update",
        "correlate",
        "populate_existing",
        "autoflush",
        "prefix_with",
        "suffix_with",
    }
)
_QUERY_RUNS = frozenset({"all", "first", "one", "one_or_none"})
# Builtins that iterate a Query given as their only argument.
_ITERATES = frozenset({"list", "tuple", "set", "sorted"})
# Methods after which a joined eager load changes the SQL the database gets:
# FOR UPDATE on the outer join's nullable side, GROUP BY and DISTINCT over the
# joined columns (SQLAlchemy 2.0 does not nest the query for a many-to-one).
_NOT_JOINED = frozenset(
    {"with_for_update", "group_by", "having", "distinct", "prefix_with", "suffix_with"}
)
# The origin of a module-level ``import x`` or ``import x.y as z``.
_IMPORTED = "import "
# Expressions that run one of several queries, or none: never certain.
_BRANCHES = (
    ast.IfExp,
    ast.BoolOp,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


class _Module:
    """A parsed source file and what the patch needs to know about it."""

    def __init__(self, source: str, tree: ast.Module) -> None:
        self.source = source
        self.lines = _split(source)
        self.tree = tree
        self.parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        # made on first use, once per parse
        self._names: dict[str, str] | None | bool = False
        self._statements: dict[int, list[ast.stmt]] | None = None

    @property
    def names(self) -> dict[str, str] | None:
        if self._names is False:
            self._names = _module_names(self.tree)
        assert not isinstance(self._names, bool)
        return self._names

    def statements_at(self, line: int) -> list[ast.stmt]:
        """The statements whose header (see ``_header``) covers a line."""
        if self._statements is None:
            self._statements = {}
            for node in ast.walk(self.tree):
                if isinstance(node, ast.stmt):
                    first, last = _header(node)
                    for number in range(first, last + 1):
                        self._statements.setdefault(number, []).append(node)
        return self._statements.get(line, [])

    def column(self, line: int, byte_offset: int) -> int:
        """The character column of an ``ast`` offset (UTF-8 bytes) on a line."""
        text = self.lines[line - 1].encode("utf-8")[:byte_offset]
        return len(text.decode("utf-8"))

    def insertion(self, line: int, column: int, text: str) -> Insertion:
        prefix = self.lines[line - 1][:column]
        utf16 = len(prefix.encode("utf-16-le")) // 2
        return Insertion(line, column, utf16, text)

    def after(self, node: ast.expr | ast.alias, text: str) -> Insertion:
        """Insert ``text`` right after a node."""
        line = node.end_lineno
        offset = node.end_col_offset
        assert line is not None and offset is not None
        return self.insertion(line, self.column(line, offset), text)


def _split(text: str) -> list[str]:
    """Lines with their ``\\n``: only it ends a line, as for Python and git.

    ``str.splitlines`` also splits at form feeds and other separators.
    """
    lines = text.split("\n")
    last = lines.pop()
    return [f"{line}\n" for line in lines] + ([last] if last else [])


@functools.lru_cache(maxsize=16)
def _parse(path: str, mtime_ns: int, size: int) -> _Module | None:
    """The module at ``path``; the stat values make a changed file a new entry."""
    try:
        data = Path(path).read_bytes()
        source = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    # a lone "\r" ends a line for Python but not for git: no patch
    lone_cr = "\r" in source.replace("\r\n", "")
    if len(data) != size or source.startswith("\ufeff") or lone_cr:
        return None
    try:
        # the user's code may warn (an invalid escape): not this plugin's warning
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    return _Module(source, tree)


def _load(path: Path, since: float) -> _Module | None:
    """The module, when its file is not newer than the session (``since``)."""
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_mtime > since:
        return None
    return _parse(str(path), stat.st_mtime_ns, stat.st_size)


def _relative(filename: str, root: Path) -> tuple[Path, str] | None:
    """(the file, its rootdir-relative posix path), or None outside the rootdir."""
    path = Path(filename)
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve()
        relative = inside(str(resolved), str(root.resolve()))
    except (OSError, ValueError):
        return None
    if relative is None or not resolved.is_file():
        return None
    return resolved, relative


def inside(path: str, root: str, flavour: Any = os.path) -> str | None:
    """``path`` relative to ``root`` with forward slashes, or None outside it.

    Compared as the platform compares paths (``flavour`` is ``os.path``, or
    ``ntpath`` in tests): on Windows case-insensitively, with either slash
    and only on the same drive.
    """
    try:
        relative = flavour.relpath(path, root)
    except ValueError:  # another drive
        return None
    parts = relative.replace(flavour.sep, "/").split("/")
    if relative == flavour.curdir or parts[0] == flavour.pardir:
        return None
    if flavour.isabs(relative):
        return None
    return "/".join(parts)


# --- names ---


def _module_names(tree: ast.Module) -> dict[str, str] | None:
    """Module-level names and where they come from, or None after a star import.

    ``from X import name`` maps to X (with its leading dots), ``import X``
    (or ``import X as name``) to "import X", a class to "class", anything else
    (a from-import under another name, an assignment, a function) to "".
    """
    names: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if alias.name == "*":
                    return None
                # an alias names another object: not the one the label means
                origin = "." * stmt.level + (stmt.module or "")
                if alias.asname is not None and alias.asname != alias.name:
                    origin = ""
                names[alias.asname or alias.name] = origin
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                names[bound] = _IMPORTED + alias.name
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names[stmt.name] = "class" if isinstance(stmt, ast.ClassDef) else ""
        else:
            # anything bound under an if, try, with or match: its origin is unknown
            for node in ast.walk(stmt):
                if isinstance(node, ast.ImportFrom) and any(
                    alias.name == "*" for alias in node.names
                ):
                    return None
                names.update(dict.fromkeys(_bound(node), ""))
    # a function that declares a module name global may rebind it
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            names.update(dict.fromkeys(node.names, ""))
    return names


def _bound(node: ast.AST) -> list[str]:
    """The names a node binds in its scope."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return [node.id]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [alias.asname or alias.name.split(".", 1)[0] for alias in node.names]
    if isinstance(node, _NAMED) and node.name:
        # a def or class, except ... as name, a match capture
        return [node.name]
    if isinstance(node, ast.MatchMapping) and node.rest:
        return [node.rest]
    return []


def _walk_scope(node: ast.AST) -> Iterator[ast.AST]:
    """``node`` and the nodes below it, without the bodies of nested scopes.

    A nested def or class still binds its own name.
    """
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if not isinstance(current, (*_SCOPES, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(current))


def _local_names(module: _Module, node: ast.AST) -> set[str]:
    """Names bound in the functions (and lambdas) around ``node``.

    With the type parameters (PEP 695) of the functions and classes around
    it, and the names a class body binds when ``node`` is in that body.
    """
    names: set[str] = set()
    scope: ast.AST | None = module.parents.get(node)
    in_function = False
    while scope is not None:
        for param in getattr(scope, "type_params", None) or ():
            names.add(param.name)
        if isinstance(scope, ast.ClassDef) and not in_function:
            for stmt in scope.body:
                for inner in _walk_scope(stmt):
                    names.update(_bound(inner))
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            in_function = True
            args = scope.args
            for arg in (
                *args.posonlyargs,
                *args.args,
                *args.kwonlyargs,
                *((args.vararg,) if args.vararg else ()),
                *((args.kwarg,) if args.kwarg else ()),
            ):
                names.add(arg.arg)
            for inner in ast.walk(scope):
                if inner is scope:
                    continue
                names.update(_bound(inner))
                if isinstance(inner, (ast.Global, ast.Nonlocal)):
                    names.update(inner.names)
        scope = module.parents.get(scope)
    return names


def _scope_name(module: _Module, node: ast.AST) -> str:
    """The function name a frame in this code reports (``co_name``)."""
    scope: ast.AST | None = module.parents.get(node)
    while scope is not None:
        if isinstance(scope, ast.Lambda):
            return "<lambda>"
        if isinstance(scope, _SCOPES):
            return scope.name
        scope = module.parents.get(scope)
    return "<module>"


# --- the query call ---


def _statement(module: _Module, node: ast.AST) -> ast.stmt | None:
    current: ast.AST | None = node
    while current is not None and not isinstance(current, ast.stmt):
        current = module.parents.get(current)
    return current


def _header(stmt: ast.stmt) -> tuple[int, int]:
    """The lines of a statement without the body of a compound statement."""
    end = stmt.end_lineno or stmt.lineno
    if isinstance(stmt, ast.Match):
        end = stmt.subject.end_lineno or stmt.lineno
    else:
        body = getattr(stmt, "body", None)
        if isinstance(body, list) and body:
            end = max(stmt.lineno, body[0].lineno - 1)
    return stmt.lineno, end


def _terminal(node: ast.expr) -> str:
    """The last name of an expression: ``session`` of ``self.session``."""
    if isinstance(node, ast.Call):
        return _terminal(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_query(call: ast.Call) -> bool:
    """``select(...)``, or ``.query(...)`` on something named as a session."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "select"
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "query"
        and "session" in _terminal(func.value).lower()
    )


def _query_call(module: _Module, line: int) -> ast.Call | None:
    """The only query call of the statement at a line, when it is the one that ran.

    None when the statement has no such call or several, when it chooses
    between expressions (``a if c else b``, ``or``, a lambda or a
    comprehension), or when it runs a statement that does not contain the call
    (``session.scalars(other)``) or another query call. A statement without
    a query call that runs a variable is looked up by ``_assigned_query``.
    """
    statements = module.statements_at(line)
    if len(statements) != 1:
        return None  # several statements on one line
    stmt = statements[0]
    # the statement's own expressions, not those of a compound statement's body
    nodes = [node for node in ast.walk(stmt) if _statement(module, node) is stmt]
    if any(isinstance(node, _BRANCHES) for node in nodes):
        return None
    calls = [node for node in nodes if isinstance(node, ast.Call)]
    found = [call for call in calls if _is_query(call)]
    if not found:
        return _assigned_query(module, stmt, calls)
    if len(found) != 1:
        return None
    query = found[0]
    for call in calls:
        if _runs(call) and not any(query in ast.walk(arg) for arg in call.args):
            return None
    return query if _executed(module, query) else None


def _executed(module: _Module, query: ast.Call) -> bool:
    """Whether the query call is the statement the line runs, as it is.

    A ``select(...)``, with only methods of ``_KEEPS_ENTITIES`` chained to it,
    is the first argument of a ``_EXECUTES`` method of something named like a
    session. A ``session.query(...)`` has only methods of ``_QUERY_KEEPS``
    before one of ``_QUERY_RUNS``, or, without one, is iterated by the
    statement (``for``) or a builtin (``list(...)``). Not a select inside a
    union, an exists, a subquery, a CTE, an insert or another statement, nor
    one passed to a helper or turned into other columns.
    """
    chain = _chain(module, query)
    if isinstance(query.func, ast.Name):
        if any(method not in _KEEPS_ENTITIES for method, _ in chain):
            return False
        last = chain[-1][1] if chain else query
        run = module.parents.get(last)
        return (
            isinstance(run, ast.Call)
            and isinstance(run.func, ast.Attribute)
            and run.func.attr in _EXECUTES
            and "session" in _terminal(run.func.value).lower()
            and bool(run.args)
            and run.args[0] is last
        )
    end: ast.expr = query
    for method, call in chain:
        if method in _QUERY_RUNS:
            return not call.args and not call.keywords
        if method not in _QUERY_KEEPS:
            return False
        end = call
    outer = module.parents.get(end)
    if isinstance(outer, (ast.For, ast.AsyncFor)):
        return outer.iter is end
    if not (
        isinstance(outer, ast.Call)
        and isinstance(outer.func, ast.Name)
        and outer.func.id in _ITERATES
        and len(outer.args) == 1
        and outer.args[0] is end
    ):
        return False
    names = module.names
    builtin = outer.func.id
    return (
        names is not None
        and builtin not in names
        and builtin not in _local_names(module, outer)
    )


def _runs(call: ast.Call) -> bool:
    """A call that may run a query: a method of ``_RUNS``.

    Except one without arguments on what such a call returned, which is a
    method of the result (``session.execute(...).scalars()``): every session
    method that runs a query takes the statement or instance. A ``.scalar()``
    on anything else (a ``Query``) may run one.
    """
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr in _RUNS):
        return False
    if call.args or call.keywords:
        return True
    receiver = func.value
    if isinstance(receiver, ast.Await):
        receiver = receiver.value
    return not (isinstance(receiver, ast.Call) and _runs(receiver))


def _function(module: _Module, node: ast.AST) -> ast.AST | None:
    """The function whose body holds ``node``; None in a class body or the module."""
    scope = module.parents.get(node)
    while scope is not None:
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return scope
        if isinstance(scope, (ast.ClassDef, ast.Lambda)):
            return None
        scope = module.parents.get(scope)
    return None


def _assigned_query(
    module: _Module, stmt: ast.stmt, calls: list[ast.Call]
) -> ast.Call | None:
    """The ``select(...)`` call of the variable that the statement runs.

    ``statement = select(Order).where(...)`` and, on a later line of the same
    function, ``session.scalars(statement)``: the call is certain when the
    statement runs exactly one query, on something named like a session, with
    the variable as its first argument; the function binds the variable once, in that assignment (no
    other assignment, parameter, ``global`` or ``del``), and reads it only
    there; and the assigned value is the ``select(...)`` call, alone or with
    methods of ``_KEEPS_ENTITIES`` chained to it, with no other query and no choice between
    expressions. None otherwise.
    """
    runs = [call for call in calls if _runs(call)]
    if len(runs) != 1:
        return None
    run = runs[0]
    assert isinstance(run.func, ast.Attribute)
    if run.func.attr not in _EXECUTES or not run.args:
        return None
    # a helper that takes the statement (repo.execute(stmt)) may change it
    if "session" not in _terminal(run.func.value).lower():
        return None
    variable = run.args[0]
    function = _function(module, stmt)
    if not isinstance(variable, ast.Name) or function is None:
        return None
    name = variable.id
    if any(param.name == name for param in getattr(function, "type_params", ())):
        return None
    target: ast.Name | None = None
    reads = 0
    # nested scopes too: a closure that reads or rebinds it makes it uncertain
    for node in ast.walk(function):
        if node is function:
            continue
        if isinstance(node, ast.Name) and node.id == name:
            if isinstance(node.ctx, ast.Load):
                reads += 1
            elif target is None:
                target = node
            else:
                return None  # bound twice, or deleted
        elif _binds(node, name):
            return None
    if target is None or reads != 1:
        return None
    assign = module.parents.get(target)
    if isinstance(assign, ast.Assign):
        if len(assign.targets) != 1 or assign.targets[0] is not target:
            return None
    elif not (
        isinstance(assign, ast.AnnAssign)
        and assign.target is target
        and assign.value is not None
    ):
        return None
    value = assign.value
    assert value is not None
    end = assign.end_lineno
    if _function(module, assign) is not function or end is None or end >= stmt.lineno:
        return None
    nodes = list(ast.walk(value))
    if any(isinstance(node, _BRANCHES) for node in nodes):
        return None
    inner = [node for node in nodes if isinstance(node, ast.Call)]
    found = [call for call in inner if _is_query(call)]
    # select() only: a session.query() is not a statement for execute()
    if len(found) != 1 or not isinstance(found[0].func, ast.Name):
        return None
    if any(_runs(call) for call in inner):
        return None
    query = found[0]
    chain = _chain(module, query)
    if any(method not in _KEEPS_ENTITIES for method, _ in chain):
        return None
    if (chain[-1][1] if chain else query) is not value:
        return None
    return query


def _binds(node: ast.AST, name: str) -> bool:
    """Whether ``node`` binds ``name`` other than as a plain variable.

    As a parameter, ``global`` or ``nonlocal``, an import, def or class, an
    ``except ... as`` or a match capture.
    """
    if isinstance(node, ast.arg):
        return node.arg == name
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return name in node.names
    return name in _bound(node)


def _reference(argument: ast.expr, entity: str) -> tuple[str, str] | None:
    """(the code that names the parent class, the name it starts with).

    ``Order`` or a module attribute such as ``models.Order``; None for
    anything else.
    """
    if isinstance(argument, ast.Name):
        return (entity, entity) if argument.id == entity else None
    if not (isinstance(argument, ast.Attribute) and argument.attr == entity):
        return None
    base: ast.expr = argument.value
    while isinstance(base, ast.Attribute):
        base = base.value
    if not isinstance(base, ast.Name):
        return None
    return ast.unparse(argument), base.id


def _chain(module: _Module, call: ast.Call) -> list[tuple[str, ast.Call]]:
    """The methods called on the result of ``call``: ``.where(...).options(...)``."""
    methods: list[tuple[str, ast.Call]] = []
    current: ast.AST = call
    while True:
        attribute = module.parents.get(current)
        if not (isinstance(attribute, ast.Attribute) and attribute.value is current):
            return methods
        outer = module.parents.get(attribute)
        if not (isinstance(outer, ast.Call) and outer.func is attribute):
            return methods
        methods.append((attribute.attr, outer))
        current = outer


# --- the patch ---


def _import_edits(
    module: _Module, names: dict[str, str], loaders: Iterable[str]
) -> list[Insertion] | None:
    """The insertions that import the missing loaders; None when not possible."""
    missing = sorted(loader for loader in set(loaders) if loader not in names)
    if not missing:
        return []
    for stmt in module.tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == _ORM and not stmt.level:
            last = stmt.names[-1]
            return [module.after(last, f", {loader}") for loader in missing]
    # a new line after the imports at the top of the module
    anchor: ast.stmt | None = None
    for index, stmt in enumerate(module.tree.body):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            anchor = stmt
        elif not (index == 0 and _is_docstring(stmt)):
            break
    if anchor is None or anchor.end_lineno is None:
        return None
    line = anchor.end_lineno
    text = module.lines[line - 1]
    # the new line ends like the line before it: "\n" or "\r\n"
    newline = "\r\n" if text.endswith("\r\n") else "\n"
    end = len(text.removesuffix("\n").removesuffix("\r"))
    return [
        module.insertion(line, end, f"{newline}from {_ORM} import {loader}")
        for loader in missing
    ]


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _option_edit(
    module: _Module,
    call: ast.Call,
    option: str,
    attributes: set[str],
    names: dict[str, str],
) -> Insertion | None:
    """The insertion that adds ``option`` to the query; None when not certain."""
    chain = _chain(module, call)
    existing = [outer for name, outer in chain if name == "options"]
    if len(existing) > 1:
        return None
    if not existing:
        return module.after(call, f".options({option})")
    options = existing[0]
    # only plain loader calls: a starred or named list may hide an option
    if options.keywords or not options.args:
        return None
    if not all(isinstance(argument, ast.Call) for argument in options.args):
        return None
    local = _local_names(module, options)
    if not all(_plain_loader(argument, names, local) for argument in options.args):
        return None
    for node in ast.walk(options):
        # an option for the same relationship already: adding one would conflict
        if isinstance(node, ast.Attribute) and node.attr in attributes:
            return None
    last = options.args[-1]
    # ", option" after "(a)" in ".options((a))" would make a tuple of them
    if _between(module, last, options) not in (")", ",)"):
        return None
    return module.after(last, f", {option}")


def _plain_loader(option: ast.expr, names: dict[str, str], local: set[str]) -> bool:
    """Whether an existing option names every relationship it loads.

    A chain of SQLAlchemy loader calls (``selectinload(Order.items)``,
    ``.joinedload(Item.product)``, ``Load(Order).raiseload("*")``) whose
    arguments are ``Class.relationship`` (or ``models.Class.relationship``),
    ``"*"``, or for ``Load`` a class; keyword values are constants. Not a
    relationship held in a variable or a constant (``noload(REL)``), named by
    a string or computed, nor an option a helper returns: any of them may be
    the relationship of the patch, and two loaders of it conflict.
    """
    current: ast.expr = option
    while isinstance(current, ast.Call):
        func = current.func
        # a constant: not **kwargs, which may hold anything
        if any(not isinstance(k.value, ast.Constant) for k in current.keywords):
            return False
        loader = _terminal(func)
        for argument in current.args:
            if isinstance(argument, ast.Constant):
                if argument.value != "*":
                    return False
            elif not _class_attribute(argument, local, loader == "Load"):
                return False
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call):
            current = func.value  # .joinedload(...) on the loader before it
            continue
        if isinstance(func, ast.Name):
            origin = names.get(func.id)
            # unbound in the module: the code that ran found it elsewhere
            return func.id not in local and (
                origin is None or origin.split(".", 1)[0] in _SELECT_MODULES
            )
        # orm.selectinload(...) of "import sqlalchemy.orm as orm"
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            origin = names.get(func.value.id, "")
            return func.value.id not in local and origin in (
                f"{_IMPORTED}sqlalchemy",
                f"{_IMPORTED}sqlalchemy.orm",
            )
        return False
    return False


def _class_attribute(node: ast.expr, local: set[str], load: bool) -> bool:
    """``Order.items``, ``models.Order.items`` (or ``Order`` for ``Load``).

    The class part starts with a capital and the attribute does not, so
    ``rels.CUSTOMER`` (a relationship in a constant) is not one.
    """
    current = node
    if not load:
        if not isinstance(node, ast.Attribute) or not node.attr[:1].islower():
            return False
        current = node.value
    # the class: Order, or models.Order
    if isinstance(current, ast.Name):
        name = current.id
    elif isinstance(current, ast.Attribute):
        name = current.attr
        while isinstance(current, ast.Attribute):
            current = current.value
    else:
        return False
    return (
        name[:1].isupper() and isinstance(current, ast.Name) and current.id not in local
    )


def _between(module: _Module, first: ast.expr, outer: ast.expr) -> str:
    """The code from the end of ``first`` to the end of ``outer``, which contains it.

    Without whitespace, comments and line continuations: between two nodes
    there is no string, so a "#" starts a comment.
    """
    assert first.end_lineno is not None and first.end_col_offset is not None
    assert outer.end_lineno is not None and outer.end_col_offset is not None
    start = module.column(first.end_lineno, first.end_col_offset)
    end = module.column(outer.end_lineno, outer.end_col_offset)
    lines = module.lines[first.end_lineno - 1 : outer.end_lineno]
    if len(lines) == 1:
        text = lines[0][start:end]
    else:
        text = lines[0][start:] + "".join(lines[1:-1]) + lines[-1][:end]
    return re.sub(r"#[^\n]*|\\\r?\n|\s", "", text)


def _ordered(insertions: Iterable[Insertion]) -> list[Insertion]:
    """Deduplicated and from the end of the file to its start, for ``_diff``.

    Inserting from the end keeps the positions of the others valid; several
    insertions at one place end up in the order of their text.
    """
    return sorted(
        set(insertions), key=lambda e: (e.line, e.column, e.text), reverse=True
    )


def _diff(file: str, module: _Module, insertions: Iterable[Insertion]) -> str:
    out = list(module.lines)
    for edit in _ordered(insertions):
        text = out[edit.line - 1]
        out[edit.line - 1] = text[: edit.column] + edit.text + text[edit.column :]
    after = _split("".join(out))
    lines = difflib.unified_diff(module.lines, after, f"a/{file}", f"b/{file}")
    # the last line of a file without a final newline, marked as diff does
    return "".join(
        line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n"
        for line in lines
    )


def make(finding: RelationshipFinding, root: Path, since: float) -> SourceFix | None:
    """The patch that adds the finding's loader option, or None when not certain.

    ``root`` is pytest's rootdir and ``since`` the time the session started:
    a file modified after it may not be the code that ran.
    """
    site = finding.parent_site
    entity = finding.parent_root
    if site is None or entity is None or finding.other_parent_sites:
        return None
    located = _relative(site.filename, root)
    if located is None:
        return None
    path, file = located
    module = _load(path, since)
    if module is None or not 0 < site.lineno <= len(module.lines):
        return None
    call = _query_call(module, site.lineno)
    if call is None or call.keywords or len(call.args) != 1:
        return None
    reference = _reference(call.args[0], entity)
    if reference is None or _scope_name(module, call) != site.function:
        return None
    text, base = reference
    names = module.names
    if names is None:
        return None
    steps = loader_steps(finding)
    # the classes named on their own; the parent class as the query names it
    classes: set[str] = {entity} if text == entity else set()
    attributes: set[str] = set()
    parts: list[str] = []
    for loader, label in steps:
        owner, _, attribute = label.partition(".")
        if not (owner.isidentifier() and attribute.isidentifier()):
            return None
        if owner == entity:
            owner = text
        else:
            classes.add(owner)
        parts.append(f"{loader}({owner}.{attribute})")
        attributes.add(attribute)
    loaders = {loader for loader, _ in steps}
    local = _local_names(module, call)
    needed = classes | loaders | {base}
    if isinstance(call.func, ast.Name):
        needed.add("select")
        origin = names.get("select", "")
        if origin.split(".", 1)[0] not in _SELECT_MODULES:
            return None
    if needed & local:
        return None
    for name in classes:
        origin = names.get(name, "")
        # not from-imported under its own name, nor a class
        if not origin or origin.startswith(_IMPORTED):
            return None
    # the module of models.Order: imported, not assigned or a class
    if base not in classes and names.get(base, "") in ("", "class"):
        return None
    for loader in loaders:
        if loader in names and names[loader] != _ORM:
            return None
    # a joined load of the parent query (not one under a selectinload, which
    # runs its own query) would change the SQL of FOR UPDATE, GROUP BY, DISTINCT
    if steps[0][0] == "joinedload" and any(
        method in _NOT_JOINED for method, _ in _chain(module, call)
    ):
        return None
    option = ".".join(parts)
    edit = _option_edit(module, call, option, attributes, names)
    imports = _import_edits(module, names, loaders)
    if edit is None or imports is None:
        return None
    insertions = tuple(_ordered([edit, *imports]))
    return SourceFix(
        file=file,
        insertions=insertions,
        description=f"Load {finding.relationship} with the parent query: {option}",
        diff=_diff(file, module, insertions),
    )


def combine(
    fixes: Iterable[SourceFix], root: Path, since: float
) -> tuple[str, int, list[str]]:
    """(one patch with every fix, number of files in it, files left out).

    Fixes of one file are merged; a file changed since the session started is
    left out.
    """
    by_file: dict[str, set[Insertion]] = {}
    for fix in fixes:
        by_file.setdefault(fix.file, set()).update(fix.insertions)
    parts: list[str] = []
    changed: list[str] = []
    for file in sorted(by_file):
        module = _load(root / file, since)
        if module is None:
            changed.append(file)
            continue
        parts.append(_diff(file, module, _without_prefixes(by_file[file])))
    return "".join(parts), len(parts), changed


def _option_text(text: str) -> str | None:
    """The loader option an insertion adds to a query, or None for an import."""
    if text.startswith(".options(") and text.endswith(")"):
        return text[len(".options(") : -1]
    if text.startswith(", ") and text.endswith(")"):
        return text[2:]
    return None


def _without_prefixes(insertions: set[Insertion]) -> set[Insertion]:
    """The insertions minus an option that another one at its place extends.

    Two findings of one query, ``Playlist.entries`` and, through it,
    ``PlaylistEntry.track``, each add their option at the same place:
    ``selectinload(Playlist.entries)`` is then a prefix of
    ``selectinload(Playlist.entries).joinedload(PlaylistEntry.track)``, which
    loads it the same way, so only the longer chain is kept.
    """
    options = {
        edit: option
        for edit in insertions
        if (option := _option_text(edit.text)) is not None
    }
    return {
        edit
        for edit in insertions
        if edit not in options
        or not any(
            other.line == edit.line
            and other.column == edit.column
            and longer.startswith(options[edit] + ".")
            for other, longer in options.items()
        )
    }


# --- for pytest-xdist and the SARIF store ---


def to_wire(fix: SourceFix | None) -> dict[str, Any] | None:
    if fix is None:
        return None
    return {
        "file": fix.file,
        "insertions": [
            [e.line, e.column, e.utf16_column, e.text] for e in fix.insertions
        ],
        "description": fix.description,
        "diff": fix.diff,
    }


def from_wire(raw: Mapping[str, Any] | None) -> SourceFix | None:
    if raw is None:
        return None
    return SourceFix(
        file=str(raw["file"]),
        insertions=tuple(
            Insertion(int(line), int(column), int(utf16), str(text))
            for line, column, utf16, text in raw["insertions"]
        ),
        description=str(raw["description"]),
        diff=str(raw["diff"]),
    )
