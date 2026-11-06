"""A patch that adds the loader option to the query that loaded the parents.

A ``relationship-load`` finding knows the line of the parent query and the
loader option that fixes it (``fix:`` in the report). When that line is
certain, this module turns the hint into a source change: the option goes on
the ``select(Entity)`` or ``session.query(Entity)`` call of that line, and the
loader is imported if the module does not import it yet.

A wrong patch costs more trust than no patch, so every doubt means none:

- the file is inside the rootdir, UTF-8 with ``\\n`` line ends, parses with
  ``ast``, and was not modified after the session started;
- the line belongs to the function the query ran in;
- the statement on that line has exactly one ``select(...)`` or
  ``.query(...)`` call, with the parent class as its only argument, and
  ``select`` is SQLAlchemy's (or SQLModel's);
- the classes and loaders the option names are module-level names that the
  function does not rebind; a loader missing from the module is added to its
  ``from sqlalchemy.orm import`` line, or on a new line after its imports;
- an ``.options(...)`` already in the chain gets the loader as one more
  argument, unless it mentions a relationship of the option already.

Everything runs when a test's findings are made, never per statement.
"""

from __future__ import annotations

import ast
import difflib
import functools
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .detectors import RelationshipFinding
from .findings import Insertion, SourceFix
from .messages import loader_option, loader_steps

_ORM = "sqlalchemy.orm"
# Modules whose ``select`` builds a statement that takes loader options.
_SELECT_MODULES = ("sqlalchemy", "sqlmodel")
_COMPOUND = (
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.If,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


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
    if len(data) != size or source.startswith("\ufeff") or "\r" in source:
        return None
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
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
        relative = resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved, relative.as_posix()


# --- names ---


def _module_names(tree: ast.Module) -> dict[str, str] | None:
    """Module-level names and where they come from, or None after a star import.

    ``from X import name`` maps to X (with its leading dots), a class to
    "class", anything else (an import under another name, an assignment, a
    function) to "".
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
                names[alias.asname or alias.name.split(".", 1)[0]] = ""
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names[stmt.name] = "class" if isinstance(stmt, ast.ClassDef) else ""
        else:
            # anything bound under an if, try or with: its origin is unknown
            for node in ast.walk(stmt):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    names[node.id] = ""
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        if alias.name == "*":
                            return None
                        names[alias.asname or alias.name.split(".", 1)[0]] = ""
                elif isinstance(node, _SCOPES):
                    names[node.name] = ""
    return names


def _local_names(module: _Module, node: ast.AST) -> set[str]:
    """Names bound in the functions (and lambdas) around ``node``."""
    names: set[str] = set()
    scope: ast.AST | None = module.parents.get(node)
    while scope is not None:
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
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
                if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store):
                    names.add(inner.id)
                elif isinstance(inner, (ast.Import, ast.ImportFrom)):
                    names.update(
                        alias.asname or alias.name.split(".", 1)[0]
                        for alias in inner.names
                    )
                elif isinstance(inner, (ast.Global, ast.Nonlocal)):
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
    if isinstance(stmt, _COMPOUND) and stmt.body:
        end = max(stmt.lineno, stmt.body[0].lineno - 1)
    return stmt.lineno, end


def _is_query(call: ast.Call) -> bool:
    func = call.func
    return (isinstance(func, ast.Name) and func.id == "select") or (
        isinstance(func, ast.Attribute) and func.attr == "query"
    )


def _query_call(module: _Module, line: int) -> ast.Call | None:
    """The only ``select(...)`` or ``.query(...)`` call of the statement at a line."""
    found: list[ast.Call] = []
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Call) and _is_query(node):
            stmt = _statement(module, node)
            if stmt is not None:
                first, last = _header(stmt)
                if first <= line <= last:
                    found.append(node)
    return found[0] if len(found) == 1 else None


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
    end = len(module.lines[line - 1].rstrip("\n"))
    return [
        module.insertion(line, end, f"\nfrom {_ORM} import {loader}")
        for loader in missing
    ]


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _option_edit(
    module: _Module, call: ast.Call, option: str, attributes: set[str]
) -> Insertion | None:
    """The insertion that adds ``option`` to the query; None when not certain."""
    chain = _chain(module, call)
    existing = [outer for name, outer in chain if name == "options"]
    if len(existing) > 1:
        return None
    if not existing:
        return module.after(call, f".options({option})")
    options = existing[0]
    if options.keywords or not options.args:
        return None
    for node in ast.walk(options):
        # an option for the same relationship already: adding one would conflict
        if isinstance(node, ast.Attribute) and node.attr in attributes:
            return None
    return module.after(options.args[-1], f", {option}")


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
    argument = call.args[0]
    if not (isinstance(argument, ast.Name) and argument.id == entity):
        return None
    if _scope_name(module, call) != site.function:
        return None
    names = _module_names(module.tree)
    if names is None:
        return None
    steps = loader_steps(finding)
    classes: set[str] = {entity}
    attributes: set[str] = set()
    for _, label in steps:
        owner, _, attribute = label.partition(".")
        if not (owner.isidentifier() and attribute.isidentifier()):
            return None
        classes.add(owner)
        attributes.add(attribute)
    loaders = {loader for loader, _ in steps}
    local = _local_names(module, call)
    needed = classes | loaders
    if isinstance(call.func, ast.Name):
        needed.add("select")
        origin = names.get("select", "")
        if origin.split(".", 1)[0] not in _SELECT_MODULES:
            return None
    if needed & local:
        return None
    for name in classes:
        if not names.get(name):  # not imported under its own name, nor a class
            return None
    for loader in loaders:
        if loader in names and names[loader] != _ORM:
            return None
    option = loader_option(finding)
    edit = _option_edit(module, call, option, attributes)
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


def combine(fixes: Iterable[SourceFix], root: Path, since: float) -> tuple[str, int]:
    """(one patch with every fix, number of files in it), file by file.

    Fixes of one file are merged; a file changed since the session started is
    left out.
    """
    by_file: dict[str, set[Insertion]] = {}
    for fix in fixes:
        by_file.setdefault(fix.file, set()).update(fix.insertions)
    parts: list[str] = []
    for file in sorted(by_file):
        module = _load(root / file, since)
        if module is None:
            continue
        parts.append(_diff(file, module, by_file[file]))
    return "".join(parts), len(parts)


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
