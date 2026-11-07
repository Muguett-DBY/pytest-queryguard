"""Static N+1 check (experimental): lazy relationships read in a loop over a query.

    python -m pytest_queryguard.lint --models app.models [--format text|json|sarif]
        [--output FILE] PATHS...

The pytest plugin finds N+1 queries by watching what a test runs. This module
reads source code instead, without a database and without running anything
but the model modules: it imports the modules named by ``--models`` to learn
each mapped class's relationships and their loader strategy, then parses
PATHS with :mod:`ast` and reports a loop over an ORM query that reads a
``lazy="select"`` relationship of the loaded objects when the query has no
loader option for it.

It is deliberately conservative: anything it cannot follow (a query built in
another function, a variable assigned twice, an option it does not know) is
not reported. It looks at one function at a time.

The exit status is 1 when something is reported, 0 when nothing is, and 2
when a model module cannot be imported, the ``--output`` file cannot be written
or the arguments are wrong.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import io
import json
import os
import re
import sys
import tokenize
import warnings
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import sarif
from .messages import loader_query

RULE_ID = "queryguard/static-relationship-load"
FORMAT_VERSION = 1
DOCS = "https://maciejziel.github.io/pytest-queryguard/static-check.html"

# Loader options that load (or refuse to load) a relationship with the parents.
_COVERING = frozenset(
    ("joinedload", "selectinload", "subqueryload", "immediateload", "raiseload",
     "noload", "contains_eager")
)  # fmt: skip
# Loader options that leave the relationship lazy; they only extend a path.
_LAZY = frozenset(("lazyload", "defaultload"))
# Column options: no effect on relationships.
_COLUMN = frozenset(
    ("load_only", "defer", "undefer", "undefer_group", "with_expression")
)
# Methods of select() and Query that return the same kind of statement.
_SELECT_METHODS = frozenset(
    ("where", "filter", "filter_by", "order_by", "options", "limit", "offset",
     "join", "outerjoin", "join_from", "outerjoin_from", "distinct",
     "execution_options", "group_by", "having", "select_from", "with_for_update",
     "slice", "fetch", "params", "populate_existing", "yield_per",
     "enable_eagerloads", "prefix_with", "suffix_with")
)  # fmt: skip
# Methods that may leave out rows of the class.
_FILTERS = frozenset(
    ("where", "filter", "filter_by", "limit", "offset", "join", "join_from",
     "having", "slice", "fetch", "params")
)  # fmt: skip
# Methods of a Result that return the same rows or objects.
_RESULT_METHODS = frozenset(("all", "unique", "fetchall", "yield_per"))
_MAX_DEPTH = 50
_MAX_GENERATORS = 20  # "for" clauses of one comprehension that are followed
_LIBRARIES = frozenset(("sqlalchemy", "sqlmodel"))
_IGNORE = re.compile(r"#\s*queryguard:\s*ignore\b")
_SKIP_DIRS = frozenset(
    ("__pycache__", "node_modules", "site-packages", "venv", "build", "dist")
)


class LintError(Exception):
    pass


# --- the models ---


@dataclass(frozen=True, slots=True)
class Relationship:
    key: str
    lazy: bool  # lazy="select": one query per parent object on first access
    scalar: bool  # many-to-one or one-to-one
    target: str  # full name of the target class


@dataclass(frozen=True, slots=True)
class MappedClass:
    name: str  # __name__, used in labels
    full: str  # module.qualname
    relationships: dict[str, Relationship]
    attributes: frozenset[str]  # every mapped attribute key


@dataclass(slots=True)
class Models:
    classes: dict[str, MappedClass] = field(default_factory=dict)  # by full name
    by_name: dict[str, list[MappedClass]] = field(default_factory=dict)

    def add(self, cls: MappedClass) -> None:
        if cls.full in self.classes:
            return
        self.classes[cls.full] = cls
        self.by_name.setdefault(cls.name, []).append(cls)

    def unique(self, name: str) -> MappedClass | None:
        found = self.by_name.get(name, [])
        return found[0] if len(found) == 1 else None


def _full_name(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def load_models(modules: Sequence[str]) -> Models:
    """Import the modules and read every class mapped in their registries."""
    from sqlalchemy import inspect
    from sqlalchemy.orm import Mapper, configure_mappers

    registries: dict[int, Any] = {}
    for name in modules:
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001  # the user's code can raise anything
            raise LintError(f"cannot import {name}: {exc!r}") from None
        for value in vars(module).values():
            if not isinstance(value, type):
                continue
            mapper: Any = inspect(value, raiseerr=False)
            if isinstance(mapper, Mapper):
                registries[id(mapper.registry)] = mapper.registry
            registry = getattr(value, "registry", None)
            if registry is not None and hasattr(registry, "mappers"):
                registries[id(registry)] = registry
    try:
        configure_mappers()
    except Exception as exc:  # noqa: BLE001  # the user's mappings can raise anything
        raise LintError(f"the mappers cannot be configured: {exc!r}") from None
    models = Models()
    for registry in registries.values():
        for mapper in registry.mappers:
            relationships = {
                prop.key: Relationship(
                    key=prop.key,
                    lazy=prop.lazy in ("select", True),
                    scalar=not prop.uselist,
                    target=_full_name(prop.mapper.class_),
                )
                for prop in mapper.relationships
            }
            models.add(
                MappedClass(
                    name=mapper.class_.__name__,
                    full=_full_name(mapper.class_),
                    relationships=relationships,
                    attributes=frozenset(mapper.attrs.keys()),
                )
            )
    if not models.classes:
        raise LintError(f"no mapped classes found in {', '.join(modules)}")
    return models


# --- findings ---


@dataclass(frozen=True, slots=True)
class Finding:
    file: str
    line: int
    column: int
    loop_line: int
    query_line: int
    function: str
    root: str  # the class the query loads
    path: tuple[tuple[str, bool], ...]  # (Class.relationship, scalar) per step

    @property
    def relationship(self) -> str:
        return self.path[-1][0]

    @property
    def labels(self) -> list[str]:
        return [label for label, _ in self.path]

    @property
    def fix(self) -> str:
        return loader_query(self.root, self.path)

    def to_json(self) -> dict[str, Any]:
        return {
            "rule": RULE_ID,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "loop_line": self.loop_line,
            "query_line": self.query_line,
            "function": self.function,
            "relationship": self.relationship,
            "path": self.labels,
            "query_class": self.root,
            "fix": self.fix,
        }


# --- reading one file ---


@dataclass(frozen=True, slots=True)
class _Query:
    cls: MappedClass
    options: tuple[ast.expr, ...]
    line: int
    filtered: bool = False  # it may return only some rows of the class

    def at(self, node: ast.expr) -> _Query:
        """The query, with the line of the call that runs it."""
        return replace(self, line=node.lineno)

    def then(self, method: str, args: Sequence[ast.expr]) -> _Query:
        """The query after ``.method(*args)`` of select() or Query."""
        if method == "options":
            return replace(self, options=(*self.options, *args))
        if method in _FILTERS:
            return replace(self, filtered=True)
        return self


@dataclass(frozen=True, slots=True)
class _Binding:
    cls: MappedClass
    path: tuple[tuple[str, bool], ...]
    keys: tuple[str, ...]
    query: _Query
    loop_line: int


@dataclass(frozen=True, slots=True)
class _Loop:
    target: ast.expr
    iter: ast.expr
    body: tuple[ast.AST, ...]
    line: int
    inner: tuple[_Loop, ...] = ()  # later generators of a comprehension


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)


def _children(node: ast.AST) -> Iterator[ast.AST]:
    """The nodes of a scope, without nested functions, lambdas and classes.

    In source order, depth first; without recursion, so that code nested
    deeper than the recursion limit (a long ``a + b + ...``) is no problem.
    """
    stack = [ast.iter_child_nodes(node)]
    while stack:
        for child in stack[-1]:
            if isinstance(child, _SCOPES):
                continue
            yield child
            stack.append(ast.iter_child_nodes(child))
            break
        else:
            stack.pop()


def _comprehension_loops(
    node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
) -> list[_Loop]:
    """One loop per generator; each sees the later generators as loops inside it.

    No loops for more than ``_MAX_GENERATORS``: following them costs the square
    of their number, and real code does not have that many.
    """
    if len(node.generators) > _MAX_GENERATORS:
        return []
    elts: tuple[ast.AST, ...]
    if isinstance(node, ast.DictComp):
        # {**d for d in ...} (Python 3.15, PEP 798) has no value
        elts = (node.key,) if node.value is None else (node.key, node.value)
    else:
        elts = (node.elt,)
    gens = node.generators
    plain = [
        _Loop(gen.target, gen.iter, (*gen.ifs, *gens[i + 1 :], *elts), node.lineno)
        for i, gen in enumerate(gens)
    ]
    return [replace(loop, inner=tuple(plain[i + 1 :])) for i, loop in enumerate(plain)]


def _loops(node: ast.AST) -> list[_Loop]:
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return [_Loop(node.target, node.iter, tuple(node.body), node.lineno)]
    if isinstance(node, _COMPREHENSIONS):
        return _comprehension_loops(node)
    return []


def _name(node: ast.expr) -> str | None:
    """The last name of ``x`` or ``a.b.x``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _unwrap(node: ast.expr) -> ast.expr:
    while isinstance(node, ast.Await):
        node = node.value
    return node


class _Scope:
    """Names bound in one function (or the module) and how often."""

    def __init__(self, node: ast.AST) -> None:
        self.assigned: dict[str, list[ast.AST]] = {}
        self.unknown: set[str] = set()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                self.unknown.add(arg.arg)
            for extra in (args.vararg, args.kwarg):
                if extra is not None:
                    self.unknown.add(extra.arg)
        for child in _children(node):
            if isinstance(child, ast.Name) and isinstance(
                child.ctx, (ast.Store, ast.Del)
            ):
                self.assigned.setdefault(child.id, []).append(child)
            elif isinstance(child, (ast.Global, ast.Nonlocal)):
                self.unknown.update(child.names)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    self.unknown.add((alias.asname or alias.name).split(".")[0])
            elif (
                isinstance(child, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar))
                and child.name
            ):
                self.unknown.add(child.name)
            elif isinstance(child, ast.MatchMapping) and child.rest:
                self.unknown.add(child.rest)
        self.values: dict[str, tuple[ast.expr, int]] = {}
        for child in _children(node):
            if isinstance(child, ast.Assign) and len(child.targets) == 1:
                target, value = child.targets[0], child.value
            elif isinstance(child, ast.AnnAssign) and child.value is not None:
                target, value = child.target, child.value
            else:
                continue
            if isinstance(target, ast.Name):
                self.values[target.id] = (value, child.lineno)

    def value(self, name: str, before: int) -> tuple[ast.expr, int] | None:
        """(value, line) of a name assigned exactly once, above line ``before``.

        Names in the value are looked up above that line in turn, so a lookup
        always moves up the file and cannot go round in a circle.
        """
        if name in self.unknown or len(self.assigned.get(name, ())) != 1:
            return None
        found = self.values.get(name)
        if found is None or found[1] >= before:
            return None
        return found


class _File:
    def __init__(self, path: str, source: str, models: Models) -> None:
        self.path = path
        self.models = models
        # the code's own warnings (an invalid escape) are not the check's
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.tree = ast.parse(source, filename=path)
        self.ignored = _ignored_lines(source)
        self.parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        self.from_imports: dict[str, str] = {}  # name -> module.name
        self.module_imports: dict[str, str] = {}  # name -> module
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    full = f"{node.module}.{alias.name}"
                    self.from_imports[alias.asname or alias.name] = full
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.module_imports[alias.asname] = alias.name
                    else:
                        top = alias.name.split(".")[0]
                        self.module_imports[top] = top
        # names under which SQLAlchemy's functions are reached: sa, orm, ...
        self.namespaces = frozenset(
            name
            for name, full in [*self.module_imports.items(), *self.from_imports.items()]
            if full.split(".")[0] in _LIBRARIES
        ) | frozenset(self._flask_sqlalchemy())
        # the lines as ast numbers them: not str.splitlines, which also splits
        # at a form feed or a line separator inside a string or a comment
        self.lines = re.split(r"\r\n|\r|\n", source)
        self.findings: dict[tuple[int, tuple[str, ...]], Finding] = {}
        self.loaded: dict[str, int] = {}
        self.suppressed: set[tuple[int, tuple[str, ...]]] = set()

    # -- names --

    def mapped(self, node: ast.expr) -> MappedClass | None:
        """The mapped class an expression names, when it is unambiguous."""
        models = self.models
        if isinstance(node, ast.Name):
            full = self.from_imports.get(node.id)
            if full is not None:
                found = models.classes.get(full)
                return found or models.unique(full.rsplit(".", 1)[1])
            return models.unique(node.id)
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                module = self.module_imports.get(node.value.id, node.value.id)
                found = models.classes.get(f"{module}.{node.attr}")
                if found is not None:
                    return found
            return models.unique(node.attr)
        return None

    def _flask_sqlalchemy(self) -> Iterator[str]:
        """Module-level ``db = SQLAlchemy(...)``: ``db.select`` is SQLAlchemy's."""
        for node in self.tree.body:
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
                continue
            func = node.value.func
            if isinstance(func, ast.Name):
                full = self.from_imports.get(func.id, "")
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                module = self.module_imports.get(func.value.id, "")
                full = f"{module}.{func.attr}"
            else:
                continue
            if full == "flask_sqlalchemy.SQLAlchemy":
                yield from (t.id for t in node.targets if isinstance(t, ast.Name))

    def is_sqlalchemy(self, func: ast.expr, name: str) -> bool:
        """Whether ``func`` is SQLAlchemy's (or SQLModel's) ``name``, by the imports."""
        if isinstance(func, ast.Name):
            full = self.from_imports.get(func.id, "")
            return full.split(".")[0] in _LIBRARIES and full.endswith(f".{name}")
        if isinstance(func, ast.Attribute) and func.attr == name:
            base: ast.expr = func.value
            while isinstance(base, ast.Attribute):
                base = base.value
            return isinstance(base, ast.Name) and base.id in self.namespaces
        return False

    # -- queries --

    def statement(
        self, node: ast.expr, scope: _Scope, line: int, depth: int = 0
    ) -> _Query | None:
        """``select(C)`` with methods that keep it a select of C."""
        if depth > _MAX_DEPTH:
            return None
        node = _unwrap(node)
        if isinstance(node, ast.Name):
            found = scope.value(node.id, line)
            if found is None:
                return None
            return self.statement(found[0], scope, found[1], depth + 1)
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        if self.is_sqlalchemy(func, "select"):
            if len(node.args) != 1 or node.keywords:
                return None
            cls = self.mapped(node.args[0])
            return None if cls is None else _Query(cls, (), node.lineno)
        if isinstance(func, ast.Attribute) and func.attr in _SELECT_METHODS:
            base = self.statement(func.value, scope, line, depth + 1)
            if base is None:
                return None
            return base.then(func.attr, node.args)
        return None

    def source(
        self, node: ast.expr, scope: _Scope, line: int, depth: int = 0
    ) -> tuple[str, _Query] | None:
        """("entities" | "rows" | "query", query) for an expression, or None."""
        if depth > _MAX_DEPTH:
            return None
        node = _unwrap(node)
        if isinstance(node, ast.Name):
            found = scope.value(node.id, line)
            if found is None:
                return None
            return self.source(found[0], scope, found[1], depth + 1)
        if isinstance(node, ast.Attribute) and node.attr == "query":
            # Flask-SQLAlchemy's Model.query
            cls = self.mapped(node.value)
            if cls is not None and "query" not in cls.attributes:
                return "query", _Query(cls, (), node.lineno)
            return None
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        if isinstance(func, ast.Name) and func.id in ("list", "tuple", "sorted"):
            if not node.args:
                return None
            inner = self.source(node.args[0], scope, line, depth + 1)
            if inner is None or inner[0] == "rows":
                return None
            return "entities", inner[1]
        if not isinstance(func, ast.Attribute):
            return None
        method = func.attr
        if method in ("scalars", "stream_scalars") and node.args:
            query = self.statement(node.args[0], scope, line)
            return None if query is None else ("entities", query.at(node))
        if method in ("execute", "stream") and node.args:
            query = self.statement(node.args[0], scope, line)
            return None if query is None else ("rows", query.at(node))
        if method == "query":
            if len(node.args) != 1 or node.keywords:
                return None
            cls = self.mapped(node.args[0])
            return None if cls is None else ("query", _Query(cls, (), node.lineno))
        base = self.source(func.value, scope, line, depth + 1)
        if base is None:
            return None
        kind, query = base
        if method == "scalars" and not node.args and kind == "rows":
            return "entities", query
        if method in _RESULT_METHODS:
            return ("entities" if kind == "query" else kind), query
        if kind == "query" and method in _SELECT_METHODS:
            return kind, query.then(method, node.args)
        return None

    # -- loops --

    def scan(self) -> None:
        scopes = [(self.tree, "<module>"), *_functions(self.tree, "")]
        for node, function in scopes:
            scope = _Scope(node)
            self.loaded = self.queried(node, scope)
            for child in _children(node):
                for loop in _loops(child):
                    var, iterable = _target(loop)
                    if var is None or iterable is None:
                        continue
                    found = self.source(iterable, scope, loop.line)
                    if found is None or found[0] == "rows":
                        continue
                    query = found[1]
                    binding = _Binding(query.cls, (), (), query, loop.line)
                    self.loop(loop, var, binding, function)

    def queried(self, node: ast.AST, scope: _Scope) -> dict[str, int]:
        """The first line at which the scope queries all rows of each class."""
        loaded: dict[str, int] = {}
        for child in _children(node):
            if not isinstance(child, ast.Call):
                continue
            parent = self.parents.get(child)
            if isinstance(parent, ast.Attribute) and parent.value is child:
                continue  # part of a longer chain, which is looked at as a whole
            query = self.statement(child, scope, child.lineno + 1)
            if query is None:
                found = self.source(child, scope, child.lineno + 1)
                query = None if found is None else found[1]
            if query is not None and not query.filtered:
                old = loaded.get(query.cls.full, child.lineno)
                loaded[query.cls.full] = min(old, child.lineno)
        return loaded

    def loop(self, loop: _Loop, var: str, binding: _Binding, function: str) -> None:
        if _reassigned(loop.body, var):
            return
        names: list[ast.Name] = []
        inner: list[_Loop] = list(loop.inner)
        statements = [n for n in loop.body if isinstance(n, ast.stmt)]
        ends = _ends(statements, nested_loop=False)
        for node in loop.body:
            _collect(node, var, False, ends, names, inner)
        covered = _coverage(binding.query.options, self.namespaces)
        for name in names:
            self.access(name, binding, function, covered)
        for nested in inner:
            nested_var, iterable = _target(nested)
            if nested_var is None or iterable is None:
                continue
            bound = self.follow(iterable, var, binding)
            if bound is not None and not bound[1].scalar:
                cls = self.models.classes.get(bound[1].target)
                if cls is None:
                    continue
                label = f"{bound[0].name}.{bound[1].key}"
                child = _Binding(
                    cls,
                    (*bound[2].path, (label, False)),
                    (*bound[2].keys, bound[1].key),
                    binding.query,
                    nested.line,
                )
                self.loop(nested, nested_var, child, function)

    def follow(
        self, node: ast.expr, var: str, binding: _Binding
    ) -> tuple[MappedClass, Relationship, _Binding] | None:
        """For ``var.a.b`` (relationships), the last class, relationship and path."""
        node = _unwrap(node)
        attrs: list[str] = []
        while isinstance(node, ast.Attribute):
            attrs.insert(0, node.attr)
            node = node.value
        if not (isinstance(node, ast.Name) and node.id == var) or not attrs:
            return None
        cls, path, keys = binding.cls, binding.path, binding.keys
        if attrs[0] == "awaitable_attrs":
            attrs = attrs[1:]
        last: tuple[MappedClass, Relationship, _Binding] | None = None
        for i, attr in enumerate(attrs):
            rel = cls.relationships.get(attr)
            if rel is None:
                return None
            last = (cls, rel, _Binding(cls, path, keys, binding.query, 0))
            if i < len(attrs) - 1:
                if not rel.scalar:
                    return None
                path = (*path, (f"{cls.name}.{attr}", True))
                keys = (*keys, attr)
                target = self.models.classes.get(rel.target)
                if target is None:
                    return None
                cls = target
        return last

    def access(
        self, name: ast.Name, binding: _Binding, function: str, covered: _Covered
    ) -> None:
        cls, path, keys = binding.cls, binding.path, binding.keys
        node: ast.AST = name
        parent = self.parents.get(node)
        if (
            isinstance(parent, ast.Attribute)
            and parent.value is node
            and parent.attr == "awaitable_attrs"
        ):
            node, parent = parent, self.parents.get(parent)
        while isinstance(parent, ast.Attribute) and parent.value is node:
            rel = cls.relationships.get(parent.attr)
            if rel is None or not isinstance(parent.ctx, ast.Load):
                return
            label = f"{cls.name}.{rel.key}"
            path = (*path, (label, rel.scalar))
            keys = (*keys, rel.key)
            # a many-to-one whose targets the function loaded (all of them,
            # without a filter) before the loop is usually answered from the
            # identity map, without a query (not
            # counted when the target is the class of the loop's own query,
            # which may have been built before the loop)
            preloaded = (
                rel.scalar
                and rel.target != binding.query.cls.full
                and self.loaded.get(rel.target, 1 << 31) < binding.loop_line
            )
            if rel.lazy and not preloaded and not covered.covers(keys):
                self.report(parent, binding, function, path, keys)
            if not rel.scalar:
                return
            target = self.models.classes.get(rel.target)
            if target is None:
                return
            cls, node, parent = target, parent, self.parents.get(parent)

    def report(
        self,
        node: ast.Attribute,
        binding: _Binding,
        function: str,
        path: tuple[tuple[str, bool], ...],
        keys: tuple[str, ...],
    ) -> None:
        key = (binding.loop_line, tuple(label for label, _ in path))
        line = getattr(node, "end_lineno", None) or node.lineno
        if (
            binding.loop_line in self.ignored
            or line in self.ignored
            or node.lineno in self.ignored
        ):
            self.suppressed.add(key)
            return
        end = getattr(node, "end_col_offset", None) or node.col_offset
        # the offsets of ast are in UTF-8 bytes; the columns are in characters
        start = end - len(node.attr.encode("utf-8"))
        text = self.lines[line - 1] if 0 < line <= len(self.lines) else ""
        column = len(text.encode("utf-8")[:start].decode("utf-8", "replace")) + 1
        finding = Finding(
            file=self.path,
            line=line,
            column=column,
            loop_line=binding.loop_line,
            query_line=binding.query.line,
            function=function,
            root=binding.query.cls.name,
            path=path,
        )
        old = self.findings.get(key)
        if old is None or (finding.line, finding.column) < (old.line, old.column):
            self.findings[key] = finding

    def results(self) -> list[Finding]:
        return [f for k, f in self.findings.items() if k not in self.suppressed]


def _functions(node: ast.AST, prefix: str) -> Iterator[tuple[ast.AST, str]]:
    """Every function below ``node`` with its qualified name, in source order."""
    stack = [(ast.iter_child_nodes(node), prefix)]
    while stack:
        children, prefix = stack[-1]
        for child in children:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                yield child, name
                stack.append((ast.iter_child_nodes(child), f"{name}.<locals>."))
            elif isinstance(child, ast.ClassDef):
                stack.append((ast.iter_child_nodes(child), f"{prefix}{child.name}."))
            else:
                stack.append((ast.iter_child_nodes(child), prefix))
            break
        else:
            stack.pop()


def _target(loop: _Loop) -> tuple[str | None, ast.expr | None]:
    """(loop variable, iterable), looking through ``enumerate()``."""
    target, iterable = loop.target, loop.iter
    if isinstance(target, ast.Name):
        return target.id, iterable
    call = _unwrap(iterable)
    if (
        isinstance(target, ast.Tuple)
        and len(target.elts) == 2
        and isinstance(target.elts[1], ast.Name)
        and isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "enumerate"
        and call.args
    ):
        return target.elts[1].id, call.args[0]
    return None, None


def _reassigned(body: Iterable[ast.AST], var: str) -> bool:
    """Whether the loop body binds ``var`` again, in any way.

    A nested def or class binds its own name here, and rebinds ``var`` from
    its body only through ``nonlocal``; other bindings in its body are its
    own. A comprehension's target still counts (conservative).
    """
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, _SCOPES):
            if _binds(node, var) or any(
                isinstance(inner, ast.Nonlocal) and var in inner.names
                for inner in ast.walk(node)
            ):
                return True
            continue
        if _binds(node, var):
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def _binds(node: ast.AST, var: str) -> bool:
    """Whether a node binds (or deletes) the name ``var``."""
    if isinstance(node, ast.Name):
        return node.id == var and isinstance(node.ctx, (ast.Store, ast.Del))
    if isinstance(
        node,
        (ast.MatchAs, ast.MatchStar, ast.ExceptHandler, ast.FunctionDef,
         ast.AsyncFunctionDef, ast.ClassDef),
    ):  # fmt: skip
        return node.name == var
    if isinstance(node, ast.MatchMapping):
        return node.rest == var
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return any(
            (alias.asname or alias.name.split(".")[0]) == var for alias in node.names
        )
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return var in node.names
    return False


def _ends(block: Sequence[ast.stmt], nested_loop: bool) -> bool:
    """Whether a block leaves the loop: it runs at most once per loop."""
    for stmt in block:
        if isinstance(stmt, (ast.Return, ast.Raise)):
            return True
        if isinstance(stmt, ast.Break) and not nested_loop:
            return True
        # the else of a nested loop runs in this loop: its break leaves this one
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)) and _ends(
            stmt.orelse, nested_loop
        ):
            return True
        if isinstance(stmt, ast.If) and _if_ends(stmt, nested_loop):
            return True
    return False


def _if_ends(stmt: ast.If, nested_loop: bool) -> bool:
    """Whether every branch of an if (and its elif chain) leaves the loop."""
    while _ends(stmt.body, nested_loop):
        orelse = stmt.orelse
        if len(orelse) == 1 and isinstance(orelse[0], ast.If):
            stmt = orelse[0]  # an elif: a loop, not one call per branch
            continue
        return _ends(orelse, nested_loop)
    return False


def _collect(
    node: ast.AST,
    var: str,
    nested_loop: bool,
    ends: bool,
    names: list[ast.Name],
    inner: list[_Loop],
) -> None:
    """Uses of ``var`` that run once per iteration, and the loops inside.

    In source order, without recursion (see ``_children``).
    """
    stack = [(node, nested_loop, ends)]
    while stack:
        node, nested_loop, ends = stack.pop()
        if isinstance(node, _SCOPES):
            continue
        if isinstance(node, ast.Name):
            if node.id == var and isinstance(node.ctx, ast.Load) and not ends:
                names.append(node)
            continue
        if not ends:
            inner.extend(_loops(node))
        is_loop = isinstance(node, (ast.For, ast.AsyncFor, ast.While))
        later: list[tuple[ast.AST, bool, bool]] = []
        for name, value in ast.iter_fields(node):
            if isinstance(value, list):
                if value and isinstance(value[0], ast.stmt):
                    # a break in a nested loop's body leaves only that loop
                    nested = nested_loop or (is_loop and name == "body")
                    block_ends = ends or _ends(value, nested)
                    later += [(item, nested, block_ends) for item in value]
                else:
                    later += [
                        (item, nested_loop, ends)
                        for item in value
                        if isinstance(item, ast.AST)
                    ]
            elif isinstance(value, ast.AST):
                later.append((value, nested_loop, ends))
        stack.extend(reversed(later))


@dataclass(frozen=True, slots=True)
class _Covered:
    everything: bool
    chains: tuple[tuple[tuple[str, str], ...], ...]

    def covers(self, keys: Sequence[str]) -> bool:
        """Whether the options load the last step of ``keys`` with the parents."""
        if self.everything:
            return True
        n = len(keys)
        named: list[str] = []  # strategies set for this path by name
        wildcard: list[str] = []  # ... and through a "*"
        for chain in self.chains:
            if len(chain) < n or chain[n - 1][1] == "defaultload":
                continue
            if all(chain[i][0] == keys[i] for i in range(n)):
                named.append(chain[n - 1][1])
            elif all(chain[i][0] in (keys[i], "*") for i in range(n)):
                wildcard.append(chain[n - 1][1])
        # lazyload(C.rel) overrides selectinload("*") for that relationship
        return any(s in _COVERING for s in (named or wildcard))


_Chain = tuple[tuple[str, str], ...]


def _coverage(options: Sequence[ast.expr], namespaces: frozenset[str]) -> _Covered:
    chains: list[_Chain] = []
    for option in options:
        parsed = _option(option, namespaces)
        if parsed is None:
            return _Covered(True, ())
        chains.extend(parsed[1])
    return _Covered(False, tuple(chains))


def _key(node: ast.expr) -> str | None:
    """The relationship key of ``C.rel``, ``C.rel.of_type(X)`` or ``"rel"``."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("of_type", "and_")
    ):
        node = node.func.value
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _option(
    node: ast.expr, namespaces: frozenset[str], depth: int = 0
) -> tuple[_Chain, list[_Chain]] | None:
    """(the current path, every path an option sets), or None if not understood.

    ``namespaces``: the names a function may be reached through (``orm`` in
    ``orm.selectinload``); a method of anything else (``opt.joinedload``, with
    ``opt`` a variable) is not understood.
    """
    if not isinstance(node, ast.Call) or depth > _MAX_DEPTH:
        return None
    func = node.func
    receiver: ast.expr | None = None
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Call):
            receiver = func.value
        else:
            base: ast.expr = func.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if not (isinstance(base, ast.Name) and base.id in namespaces):
                return None
    name = _name(func)
    if name is None:
        return None
    if receiver is None:
        if name == "Load":
            return (), []
        if name in _COLUMN:
            return (), []
        if name in _COVERING or name in _LAZY:
            if len(node.args) != 1:
                return None
            key = _key(node.args[0])
            if key is None:
                return None
            first: _Chain = ((key, name),)
            return first, [first]
        return None
    parsed = _option(receiver, namespaces, depth + 1)
    if parsed is None:
        return None
    current, chains = parsed
    if name in _COLUMN:
        return current, chains
    if name in _COVERING or name in _LAZY:
        if len(node.args) != 1:
            return None
        key = _key(node.args[0])
        if key is None:
            return None
        path = (*current, (key, name))
        return path, [*chains, path]
    if name == "options":
        found = list(chains)
        for arg in node.args:
            sub = _option(arg, namespaces, depth + 1)
            if sub is None:
                return None
            found += [(*current, *chain) for chain in sub[1]]
        return current, found
    return None


def _ignored_lines(source: str) -> set[int]:
    lines: set[int] = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT and _IGNORE.search(token.string):
                lines.add(token.start[0])
    except (tokenize.TokenError, SyntaxError):
        pass
    return lines


# --- files ---


def python_files(paths: Iterable[Path]) -> list[Path]:
    """The files to check, each once (``app app/models.py`` names one twice)."""
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(
                    d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS
                )
                found += [Path(root, f) for f in sorted(files) if f.endswith(".py")]
        elif path.exists():
            found.append(path)
        else:
            raise LintError(f"{path} does not exist")
    seen: set[Path] = set()
    unique = []
    for path in found:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


@dataclass(slots=True)
class Report:
    findings: list[Finding] = field(default_factory=list)
    files: int = 0
    errors: list[str] = field(default_factory=list)


def lint_source(source: str, path: str, models: Models) -> list[Finding]:
    """The findings in one file's source.

    Raises SyntaxError if it does not parse, and RecursionError when it is
    nested too deeply for :func:`ast.parse`.
    """
    file = _File(path, source, models)
    file.scan()
    return sorted(file.results(), key=lambda f: (f.line, f.column, f.path))


def lint(paths: Iterable[Path], models: Models) -> Report:
    report = Report()
    for path in python_files(paths):
        shown = display_path(path)
        report.files += 1
        try:
            # as Python reads it: a BOM or a PEP 263 coding declaration
            with tokenize.open(path) as handle:
                source = handle.read()
            report.findings += lint_source(source, shown, models)
        except (
            OSError,
            UnicodeDecodeError,
            SyntaxError,
            ValueError,
            RecursionError,  # code nested deeper than the recursion limit
        ) as exc:
            report.errors.append(f"{shown}: {exc}")
    report.findings.sort(key=lambda f: (f.file, f.line, f.column, f.path))
    return report


# --- output ---


def text(report: Report) -> str:
    lines: list[str] = []
    for f in report.findings:
        lines += [
            (
                f"{f.file}:{f.line}:{f.column}: {f.relationship} is lazy-loaded "
                "once per row (N+1)"
            ),
            (
                f"  in the loop at line {f.loop_line} over the query at line "
                f"{f.query_line} in {f.function}"
            ),
            f"  rule: {RULE_ID}  label: {f.relationship}",
            "  fix: load it with the parent query, e.g.",
            f"       {f.fix}",
            "",
        ]
    count = len(report.findings)
    lines.append(
        f"queryguard lint (experimental): {count} finding{'s' * (count != 1)} "
        f"in {report.files} file{'s' * (report.files != 1)}"
    )
    return "\n".join(lines) + "\n"


def to_json(report: Report) -> dict[str, Any]:
    return {
        "format": FORMAT_VERSION,
        "tool": {"name": "pytest-queryguard", "version": _version()},
        "files": report.files,
        "errors": report.errors,
        "findings": [f.to_json() for f in report.findings],
    }


_SARIF_RULE = {
    "id": RULE_ID,
    "name": "StaticRelationshipLoad",
    "shortDescription": {
        "text": "A lazy relationship is read in a loop over a query (experimental)"
    },
    "fullDescription": {
        "text": "Static check: a loop over the objects of an ORM query reads a "
        'lazy="select" relationship, and the query has no loader option for it, '
        "so each object may run one more query."
    },
    "help": {
        "text": "Load the relationship with the parent query: joinedload() for a "
        "many-to-one or one-to-one relationship, selectinload() for a collection.",
    },
    "helpUri": DOCS,
    "defaultConfiguration": {"level": sarif.LEVEL_WARNING},
    "properties": {"tags": ["performance", "database", "static"]},
}


def to_sarif(report: Report, prefix: str = "") -> dict[str, Any]:
    results = []
    seen: dict[tuple[str, str, str | None, str | None], int] = {}
    for f in report.findings:
        path = sarif.repo_path(f.file)
        if path is None:
            continue
        if prefix:
            path = f"{prefix}/{path}"
        # not the line, which moves with every edit above it: the path, the
        # function and, for a repeat in the same function, its position
        identity = (RULE_ID, " -> ".join(f.labels), f.file, f.function)
        nth = seen[identity] = seen.get(identity, 0) + 1
        if nth > 1:
            identity = (*identity[:3], f"{f.function}#{nth}")
        digest = sarif.fingerprint(identity)
        results.append(
            {
                "ruleId": RULE_ID,
                "ruleIndex": 0,
                "level": sarif.LEVEL_WARNING,
                "message": {
                    "text": f"{f.relationship} is lazy-loaded once per row in the "
                    f"loop at line {f.loop_line}.\nFix: {f.fix}"
                },
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": path},
                            "region": {"startLine": f.line, "startColumn": f.column},
                        },
                        "logicalLocations": [{"name": f.function, "kind": "function"}],
                    }
                ],
                "partialFingerprints": {
                    "primaryLocationLineHash": f"{digest[:32]}:1",
                    sarif.FINGERPRINT_KEY: digest,
                },
                "properties": {"label": f.relationship, "loopLine": f.loop_line},
            }
        )
    return {
        "$schema": sarif.SCHEMA_URI,
        "version": sarif.SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "pytest-queryguard",
                        "version": _version(),
                        "informationUri": sarif.INFORMATION_URI,
                        "rules": [_SARIF_RULE],
                    }
                },
                "columnKind": "unicodeCodePoints",
                "results": results,
            }
        ],
    }


def _version() -> str:
    from . import __version__

    return __version__


def render(report: Report, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(to_json(report), indent=2, ensure_ascii=False) + "\n"
    if fmt == "sarif":
        prefix = sarif.workspace_prefix(Path.cwd(), os.environ.get("GITHUB_WORKSPACE"))
        return sarif.dumps(to_sarif(report, prefix))
    return text(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pytest_queryguard.lint",
        description="Experimental static check: lazy relationships read in a loop "
        "over an ORM query (N+1). Imports the --models modules.",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="files or directories")
    parser.add_argument(
        "--models",
        action="append",
        required=True,
        metavar="MODULE",
        help="module that defines (or imports) the mapped classes; repeatable",
    )
    parser.add_argument(
        "--format", choices=("text", "json", "sarif"), default="text", dest="fmt"
    )
    parser.add_argument("--output", type=Path, help="write the report here too")
    args = parser.parse_args(argv)
    # python -m puts the working directory on sys.path; a console script would not
    if "" not in sys.path and os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    try:
        models = load_models(args.models)
        report = lint(args.paths, models)
    except LintError as exc:
        print(f"pytest-queryguard lint: {exc}", file=sys.stderr)
        return 2
    for error in report.errors:
        print(f"pytest-queryguard lint: skipped {error}", file=sys.stderr)
    output = render(report, args.fmt)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")
    sys.stdout.write(output)
    if args.output is not None:
        try:
            args.output.write_text(output, encoding="utf-8")
        except OSError as exc:
            print(
                f"pytest-queryguard lint: cannot write {args.output}: {exc}",
                file=sys.stderr,
            )
            return 2
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
