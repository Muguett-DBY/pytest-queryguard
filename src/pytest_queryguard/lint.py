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
when a model module cannot be imported or the arguments are wrong.
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
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import sarif
from .messages import loader_query

RULE_ID = "queryguard/static-relationship-load"
FORMAT_VERSION = 1
DOCS = "https://maciejziel.github.io/pytest-queryguard/static-check.html"

# Loader strategies that keep a relationship from being lazy-loaded row by row.
_NOT_LAZY = frozenset(
    ("joined", "selectin", "subquery", "immediate", "raise", "raise_on_sql",
     "noload", "write_only", "dynamic")
)  # fmt: skip
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
# Methods of a Result that return the same rows or objects.
_RESULT_METHODS = frozenset(("all", "unique", "fetchall", "yield_per"))
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
            "path": [label for label, _ in self.path],
            "query_class": self.root,
            "fix": self.fix,
        }


# --- reading one file ---


@dataclass(frozen=True, slots=True)
class _Query:
    cls: MappedClass
    options: tuple[ast.expr, ...]
    line: int

    def at(self, node: ast.expr) -> _Query:
        """The query, with the line of the call that runs it."""
        return _Query(self.cls, self.options, node.lineno)

    def with_options(self, options: Sequence[ast.expr]) -> _Query:
        return _Query(self.cls, (*self.options, *options), self.line)


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
    """The nodes of a scope, without nested functions, lambdas and classes."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPES):
            continue
        yield child
        yield from _children(child)


def _comprehension_loops(
    node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
) -> list[_Loop]:
    """One loop per generator; each sees the later generators as loops inside it."""
    elts: tuple[ast.AST, ...] = (
        (node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,)
    )
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

    def value(self, name: str, before: int) -> ast.expr | None:
        """The value of a name assigned exactly once, above line ``before``."""
        if name in self.unknown or len(self.assigned.get(name, ())) != 1:
            return None
        found = self.values.get(name)
        if found is None or found[1] >= before:
            return None
        return found[0]


class _File:
    def __init__(self, path: str, source: str, models: Models) -> None:
        self.path = path
        self.models = models
        self.tree = ast.parse(source, filename=path)
        self.ignored = _ignored_lines(source)
        self.parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        self.from_imports: dict[str, str] = {}
        self.module_imports: dict[str, str] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    full = f"{node.module}.{alias.name}"
                    self.from_imports[alias.asname or alias.name] = full
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.module_imports[alias.asname] = alias.name
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

    # -- queries --

    def statement(self, node: ast.expr, scope: _Scope, line: int) -> _Query | None:
        """``select(C)`` with methods that keep it a select of C."""
        node = _unwrap(node)
        if isinstance(node, ast.Name):
            value = scope.value(node.id, line)
            return None if value is None else self.statement(value, scope, line)
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        if (
            _name(func) == "select"
            and not isinstance(func, ast.Attribute)
            or (
                # sa.select(...), sqlalchemy.select(...)
                isinstance(func, ast.Attribute)
                and func.attr == "select"
                and isinstance(func.value, ast.Name)
            )
        ):
            if len(node.args) != 1 or node.keywords:
                return None
            cls = self.mapped(node.args[0])
            return None if cls is None else _Query(cls, (), node.lineno)
        if isinstance(func, ast.Attribute) and func.attr in _SELECT_METHODS:
            base = self.statement(func.value, scope, line)
            if base is None:
                return None
            return base.with_options(node.args) if func.attr == "options" else base
        return None

    def source(
        self, node: ast.expr, scope: _Scope, line: int, depth: int = 0
    ) -> tuple[str, _Query] | None:
        """("entities" | "rows" | "query", query) for an expression, or None."""
        if depth > 20:
            return None
        node = _unwrap(node)
        if isinstance(node, ast.Name):
            value = scope.value(node.id, line)
            return None if value is None else self.source(value, scope, line, depth + 1)
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
            found = self.source(node.args[0], scope, line, depth + 1)
            if found is None or found[0] == "rows":
                return None
            return "entities", found[1]
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
            if method == "options":
                return kind, query.with_options(node.args)
            return kind, query
        return None

    # -- loops --

    def scan(self) -> None:
        scopes: list[ast.AST] = [self.tree]
        scopes += [
            n
            for n in ast.walk(self.tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in scopes:
            scope = _Scope(node)
            function = getattr(node, "name", "<module>")
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
        """The first line at which the scope queries each class."""
        loaded: dict[str, int] = {}
        for child in _children(node):
            if not isinstance(child, ast.Call):
                continue
            query = self.statement(child, scope, child.lineno + 1)
            if query is None:
                found = self.source(child, scope, child.lineno + 1)
                query = None if found is None else found[1]
            if query is not None:
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
        covered = _coverage(binding.query.options)
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
            # a many-to-one whose targets the function loaded before the loop
            # is usually answered from the identity map, without a query
            preloaded = rel.scalar and self.loaded.get(rel.target, 1 << 31) < (
                binding.loop_line
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
        finding = Finding(
            file=self.path,
            line=line,
            column=(getattr(node, "end_col_offset", None) or node.col_offset)
            - len(node.attr)
            + 1,
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
    for node in body:
        for child in [node, *_children(node)]:
            if (
                isinstance(child, ast.Name)
                and child.id == var
                and isinstance(child.ctx, (ast.Store, ast.Del))
            ):
                return True
    return False


def _ends(block: Sequence[ast.stmt], nested_loop: bool) -> bool:
    """Whether a block leaves the loop: it runs at most once per loop."""
    for stmt in block:
        if isinstance(stmt, (ast.Return, ast.Raise)):
            return True
        if isinstance(stmt, ast.Break) and not nested_loop:
            return True
    return False


def _collect(
    node: ast.AST,
    var: str,
    nested_loop: bool,
    ends: bool,
    names: list[ast.Name],
    inner: list[_Loop],
) -> None:
    """Uses of ``var`` that run once per iteration, and the loops inside."""
    if isinstance(node, _SCOPES):
        return
    if isinstance(node, ast.Name):
        if node.id == var and isinstance(node.ctx, ast.Load) and not ends:
            names.append(node)
        return
    if not ends:
        inner.extend(_loops(node))
    is_loop = isinstance(node, (ast.For, ast.AsyncFor, ast.While))
    for name, value in ast.iter_fields(node):
        if isinstance(value, list):
            if value and isinstance(value[0], ast.stmt):
                # a break in a nested loop's body leaves only that loop
                nested = nested_loop or (is_loop and name == "body")
                block_ends = ends or _ends(value, nested)
                for item in value:
                    _collect(item, var, nested, block_ends, names, inner)
            else:
                for item in value:
                    if isinstance(item, ast.AST):
                        _collect(item, var, nested_loop, ends, names, inner)
        elif isinstance(value, ast.AST):
            _collect(value, var, nested_loop, ends, names, inner)


@dataclass(frozen=True, slots=True)
class _Covered:
    everything: bool
    chains: tuple[tuple[tuple[str, str], ...], ...]

    def covers(self, keys: Sequence[str]) -> bool:
        """Whether the options load the last step of ``keys`` with the parents."""
        if self.everything:
            return True
        n = len(keys)
        for chain in self.chains:
            if len(chain) < n:
                continue
            if all(chain[i][0] in (keys[i], "*") for i in range(n)) and (
                chain[n - 1][1] in _COVERING
            ):
                return True
        return False


_Chain = tuple[tuple[str, str], ...]


def _coverage(options: Sequence[ast.expr]) -> _Covered:
    chains: list[_Chain] = []
    for option in options:
        parsed = _option(option)
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


def _option(node: ast.expr) -> tuple[_Chain, list[_Chain]] | None:
    """(the current path, every path an option sets), or None if not understood."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    receiver: ast.expr | None = None
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call):
        receiver = func.value
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
    base = _option(receiver)
    if base is None:
        return None
    current, chains = base
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
            sub = _option(arg)
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
    return found


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
    """The findings in one file's source; raises SyntaxError if it does not parse."""
    file = _File(path, source, models)
    file.scan()
    return sorted(file.results(), key=lambda f: (f.line, f.column, f.path))


def lint(paths: Iterable[Path], models: Models) -> Report:
    report = Report()
    for path in python_files(paths):
        shown = display_path(path)
        report.files += 1
        try:
            source = path.read_text(encoding="utf-8")
            report.findings += lint_source(source, shown, models)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
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
    for f in report.findings:
        path = sarif.repo_path(f.file)
        if path is None:
            continue
        if prefix:
            path = f"{prefix}/{path}"
        digest = sarif.fingerprint((RULE_ID, f.relationship, f.file, f.function))
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
        args.output.write_text(output, encoding="utf-8")
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
