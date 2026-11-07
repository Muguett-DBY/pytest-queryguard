"""Seeded, property-style tests for the source patch of fixpatch.py.

A generator writes many variants of a module with a parent query line
(``select(X)`` or ``session.query(X)``, ``X`` or ``models.X``, the statement
on that line or in a variable assigned before it, existing options, comments,
line continuations, decorators, nested functions, tabs, CRLF, unicode, no
final newline, import variants) and says for each whether a patch must be made,
must not be made, or may be made. Every patch made must:

- apply with ``git apply`` (skipped without git);
- give a file that parses, with the same line ends;
- change the AST by exactly the loader option (``.options(...)`` around the
  query call, or one more argument of its ``.options``) and, when the loader
  is not imported yet, its import;
- agree with its insertions (what SARIF carries).

``make()`` must never raise. Standard library only: the seed is fixed, so a
failure reproduces exactly; QUERYGUARD_FUZZ=1 runs many more rounds.
"""

from __future__ import annotations

import ast
import os
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pytest_queryguard import fixpatch
from pytest_queryguard.detectors import RelationshipFinding
from pytest_queryguard.events import CallSite
from pytest_queryguard.findings import SourceFix
from pytest_queryguard.messages import loader_steps

pytestmark = [
    pytest.mark.allow_nplusone,
    # mutated modules may have invalid escapes
    pytest.mark.filterwarnings("ignore:invalid escape sequence"),
]

SEED = int(os.environ.get("QUERYGUARD_FUZZ_SEED", "20261106"))
ROUNDS = 3000 if os.environ.get("QUERYGUARD_FUZZ") == "1" else 300
_GIT = shutil.which("git")
_ORM = "sqlalchemy.orm"

# (query class, relationship, scalar, path to it)
RELATIONSHIPS = [
    ("Order", "Order.customer", True, ()),
    ("Order", "Order.items", False, ()),
    ("Order", "Customer.country", True, (("Order.customer", True),)),
    ("Customer", "Customer.orders", False, ()),
]


@dataclass
class Case:
    lines: list[str]  # with relative indentation in steps of four spaces
    line: int  # 1-based number of the parent query line
    entity: str
    relationship: str
    scalar: bool
    path: tuple[tuple[str, bool], ...]
    function: str = "load"
    negative: list[str] = field(default_factory=list)  # reasons for no patch
    unsure: list[str] = field(default_factory=list)  # either outcome is fine
    features: list[str] = field(default_factory=list)
    indent: str = "    "
    newline: str = "\n"
    final_newline: bool = True
    prefix: bytes = b""  # a BOM
    lone_cr: bool = False
    reference: str = ""  # the class through its module: "models.Order"

    @property
    def ref(self) -> str:
        """The code that names the query's class."""
        return self.reference or self.entity

    @property
    def base(self) -> str:
        """The name the class reference starts with, which must stay bound."""
        return self.ref.split(".", 1)[0]

    @property
    def expect(self) -> bool | None:
        if self.negative:
            return False
        return None if self.unsure else True

    def source(self) -> str:
        out: list[str] = []
        for line in self.lines:
            stripped = line.lstrip(" ")
            depth = (len(line) - len(stripped)) // 4
            out.append(self.indent * depth + stripped)
        text = "\n".join(out) + "\n"
        if not self.final_newline:
            text = text.rstrip("\n")
        return text.replace("\n", self.newline)

    def data(self) -> bytes:
        text = self.source()
        if self.lone_cr:
            # a "\r" right before a "\n" would make a "\r\n"
            text = re.sub(r"\r?\n(?!\n)", "\r", text, count=1)
        return self.prefix + text.encode("utf-8")

    def describe(self) -> str:
        return (
            f"line {self.line}, {self.relationship} via {self.ref}, "
            f"features {self.features}, negative {self.negative}, "
            f"unsure {self.unsure}\n{self.source()}"
        )


def _pick(rng: random.Random, chance: float) -> bool:
    return rng.random() < chance


def _block(text: str, depth: int) -> list[str]:
    return ["    " * depth + line for line in text.split("\n")]


def _header(rng: random.Random, case: Case, style: str) -> list[str]:
    lines: list[str] = []
    if _pick(rng, 0.2):
        lines += ['"""Views: select(Order) in a docstring."""', ""]
        case.features.append("docstring")
    if _pick(rng, 0.3):
        lines.append("from __future__ import annotations")
        case.features.append("future")
    if _pick(rng, 0.3):
        lines.append("import os, sys  # select(Order) in a comment")
    if _pick(rng, 0.1):
        lines.append("\x0c")
        case.features.append("form-feed")
    selects = [
        "from sqlalchemy import select",
        "from sqlalchemy import select as select",
        "from sqlalchemy.future import select",
        "from sqlmodel import select",
        "from sqlalchemy import (\n    func,\n    select,\n)",
        "from sqlalchemy import func, \\\n    select",
    ]
    if style == "select" and _pick(rng, 0.1):
        case.negative.append("select-not-from-sqlalchemy")
        lines += _block(
            rng.choice(
                [
                    "from app.db import select",
                    "import sqlalchemy\nselect = sqlalchemy.select",
                    "from sqlalchemy import select as sel\nselect = sel",
                ]
            ),
            0,
        )
    else:
        lines += _block(rng.choice(selects), 0)
    orms = [
        "",
        "from sqlalchemy.orm import Session",
        "from sqlalchemy.orm import Session  # the session",
        "from sqlalchemy.orm import (\n    Session,\n)",
        "from sqlalchemy.orm import (Session,\n    sessionmaker)",
        "from sqlalchemy.orm import Session as S",
        "from sqlalchemy.orm import joinedload, selectinload",
        "from sqlalchemy.orm import Session, \\\n    selectinload",
        "from sqlalchemy.orm import (  # loaders\n    Session,  # here\n)",
    ]
    orm = rng.choice(orms)
    if orm:
        lines += _block(orm, 0)
    models = [
        "from app.models import Customer, Order",
        "from .models import Order, Customer",
        "from app.models import (\n    Customer,\n    Order,\n)",
        "from app import models\nfrom app.models import Customer as Customer, Order",
        "from app.models import Customer, Order  # zażółć 😀",
    ]
    if case.reference:
        if case.base == "app":
            lines.append("import app.models")
        else:
            lines += _block(
                rng.choice(["from app import models", "import app.models as models"]),
                0,
            )
        if _pick(rng, 0.8):
            lines += _block(rng.choice(models), 0)
        elif case.path:
            # the chain names Customer on its own, which is not imported
            case.negative.append("chain-class-not-imported")
        if _pick(rng, 0.05):
            case.negative.append("module-under-another-name")
            lines.append(f"from app import helpers as {case.base}")
    elif _pick(rng, 0.05):
        case.negative.append("class-under-another-name")
        lines.append(f"from app.models import Customer, Order, Item as {case.entity}")
    else:
        lines += _block(rng.choice(models), 0)
    if _pick(rng, 0.05):
        case.negative.append("star-import")
        lines.append("from app.helpers import *")
    if _pick(rng, 0.04):
        case.negative.append("loader-from-elsewhere")
        lines.append(f"from app.loaders import {_loaders(case)[0]}")
    return lines


def _loaders(case: Case) -> list[str]:
    finding = _finding(case, "x.py")
    return [loader for loader, _ in loader_steps(finding)]


def _module_noise(rng: random.Random, case: Case) -> list[str]:
    lines: list[str] = []
    choices = [
        "X = 1",
        'CONSTANT = "select(Order)"',
        "def decorator(function):\n    return function",
        "try:\n    import ujson as json\nexcept ImportError:\n    import json",
        'zażółć = "ąę 😀 select("',
    ]
    if sys.version_info >= (3, 12):
        choices.append("type Alias = list[int]")
    for _ in range(rng.randint(0, 2)):
        lines += [""] + _block(rng.choice(choices), 0)
    if _pick(rng, 0.04):
        case.negative.append("global-rebinding")
        lines += ["", *_block(f"def reset():\n    global {case.base}\n"
                               f"    {case.base} = None", 0)]  # fmt: skip
    if _pick(rng, 0.03):
        case.negative.append("module-level-match-capture")
        lines += ["", *_block(f"match X:\n    case {case.base}:\n        pass", 0)]
    return lines


def _body_noise(rng: random.Random, case: Case) -> list[str]:
    choices = [
        "n = 1",
        'text = "select(Order) # not a query"',
        'zażółć = "ąę 😀 select("',
        "if (m := n) > 0:\n    pass",
        "match n:\n    case 1:\n        pass\n    case _:\n        pass",
        'doc = """\nselect(Order)\n  # inside a string\n"""',
        "values = [i for i in range(3)]",
        "key = lambda row: row",
        "def helper(x):\n    return select(x)",
        "with open(os.devnull) as handle:\n    pass",
        "t: int = 3",
        "total = (1 +\n         2)",
    ]
    if sys.version_info >= (3, 11):
        choices.append("try:\n    pass\nexcept* ValueError:\n    pass")
    lines: list[str] = []
    for _ in range(rng.randint(0, 3)):
        lines += _block(rng.choice(choices), 0)
    if _pick(rng, 0.04):
        case.negative.append("entity-rebound-in-the-function")
        lines.append(f"{case.base} = None")
    if _pick(rng, 0.04):
        case.negative.append("loader-rebound-in-the-function")
        lines.append(f"{_loaders(case)[-1]} = None")
    return lines


def _chain(rng: random.Random, case: Case, style: str, multiline: bool) -> list[str]:
    """Methods after the query call, one string each."""
    entity = case.entity
    filters = [
        f".where({entity}.id > 0)"
        if style == "select"
        else f".filter({entity}.id > 0)",
        ".limit(3)",
        f".order_by({entity}.id)",
    ]
    parts = [rng.choice(filters) for _ in range(rng.randint(0, 2))]
    if _pick(rng, 0.08):
        # FOR UPDATE, GROUP BY and DISTINCT take a selectinload, not a joinedload
        parts.append(
            rng.choice([".with_for_update()", f".group_by({entity}.id)", ".distinct()"])
        )
        if "joinedload" in _loaders(case):
            case.negative.append("joined-load-of-a-locked-or-grouped-query")
    first = case.path[0][0] if case.path else case.relationship
    options: list[tuple[str, str | None]] = [
        ("", None),
        ("", None),
        (f".options(selectinload({entity}.extra))", None),
        (f".options(selectinload({entity}.extra), )", None),
        (f".options(selectinload({entity}.extra), raiseload('*'))", None),
        (f".options(Load({entity}).raiseload('*'))", None),
        (f".options((selectinload({entity}.extra)))", "unsure:parenthesized-option"),
        (f".options(selectinload({entity}.extra), (raiseload('*')))", "unsure:parenthesized-option"),
        (".options(*OPTIONS)", "negative:starred-options"),
        (".options(OPTIONS)", "negative:options-from-a-name"),
        (f".options(lazyload({first}))", "negative:option-for-the-relationship"),
        (".options(a()).options(b())", "negative:two-options-calls"),
    ]  # fmt: skip
    # a loader whose relationship the code does not name: it may be this one
    owner, _, attribute = first.partition(".")
    unnamed = [
        (".options(noload(REL))", "negative:loader-of-a-variable"),
        (f".options(noload({owner}.RELATIONSHIP))", "negative:loader-of-a-constant"),
        (f'.options(noload("{attribute}"))', "negative:loader-of-a-string"),
        (f".options(selectinload(getattr({entity}, name)))", "negative:computed-attribute"),
    ]  # fmt: skip
    if multiline:
        options += [
            (f".options(\n    selectinload({entity}.extra),\n)", None),
            (f".options(selectinload({entity}.extra)  # keep\n)", None),
            (f".options(\n    (selectinload({entity}.extra))\n)", "unsure:parenthesized-option"),
        ]  # fmt: skip
    text, mark = rng.choice(unnamed if _pick(rng, 0.06) else options)
    if mark is not None:
        kind, _, reason = mark.partition(":")
        (case.negative if kind == "negative" else case.unsure).append(reason)
    if text:
        parts.insert(rng.randint(0, len(parts)), text)
    return parts


def _statement(
    rng: random.Random, case: Case, style: str, asynchronous: bool, method: bool
) -> tuple[list[str], list[int]]:
    """The parent query statement and the indexes of the lines it may report."""
    entity = case.entity
    ref = case.ref
    if style == "select":
        core = rng.choice([f"select({ref})", f"select( {ref} )", f"(select({ref}))"])
    else:
        sessions = ["session", "db_session"] + (["self.session"] if method else [])
        core = f"{rng.choice(sessions)}.query({ref})"
    shape = rng.choice(
        ["assign", "return", "for", "parens", "backslash", "walrus", "same-line",
         "await", "fstring", "variable", "negative"]
    )  # fmt: skip
    if shape == "await" and not asynchronous:
        shape = "assign"
    case.features.append(f"shape:{shape}")
    multiline = shape == "parens"
    chain = _chain(rng, case, style, multiline)

    def run(statement: str) -> str:
        if style == "select":
            return f"session.scalars({statement})"
        return statement

    one_line = core + "".join(part.replace("\n", " ") for part in chain)
    if shape == "assign":
        return [f"rows = {run(one_line)}.all()"], [0]
    if shape == "return":
        return [f"return list({run(one_line)})"], [0]
    if shape == "for":
        return [f"for row in {run(one_line)}:", "    print(row)"], [0]
    if shape == "walrus":
        return [f"if (rows := {run(one_line)}.all()):", "    pass"], [0]
    if shape == "same-line":
        return [f'label, rows = "😀ż", {run(one_line)}.all()  # select( again'], [0]
    if shape == "fstring":
        # 3.12 parses f-strings into exact positions; earlier versions too
        return [f'print(f"{{ {run(one_line)}.all() }}")'], [0]
    if shape == "await":
        if style != "select":
            # a Query is not awaitable: this is not what ran
            case.negative.append("awaited-query")
        return [f"rows = (await {run(one_line)}).all()"], [0]
    if shape == "variable":
        if style != "select":
            # a Query object runs through its own methods: never followed
            case.negative.append("query-object-in-a-variable")
            return [f"query = {one_line}", "rows = query.all()"], [1]
        between = []
        if _pick(rng, 0.15):
            reason, between = rng.choice(
                [
                    ("statement-reassigned", ["statement = statement.limit(3)"]),
                    ("statement-read-twice", ["print(statement)"]),
                    ("statement-deleted", ["rows = session.scalars(statement)",
                                           "del statement"]),
                ]
            )  # fmt: skip
            case.negative.append(reason)
        run_line = rng.choice(
            [
                f"rows = {run('statement')}.all()",
                "rows = session.execute(statement).scalars().all()",
                "for row in session.scalars(statement):",
            ]
        )
        lines = [f"statement = {one_line}", *between, run_line]
        if run_line.endswith(":"):
            lines.append("    print(row)")
        return lines, [len(between) + 1]
    if shape == "backslash":
        return ["rows = \\", f"    {run(one_line)}.all()"], [0, 1]
    if shape == "parens":
        inner = [core, *("\n".join(chain).split("\n") if chain else [])]
        if style == "select":
            lines = ["result = session.execute(", *("    " + p for p in inner), ")"]
        else:
            lines = ["rows = (", *("    " + p for p in inner), "    .all()", ")"]
        return lines, list(range(len(lines)))
    # a line that runs another query or chooses between expressions
    other = (
        "select(Customer.id)" if style == "select" else "db_session.query(Customer.id)"
    )
    if style == "select":
        # the select is not the statement that runs, or not as it is
        shapes = [
            (
                "in-a-union",
                f"rows = session.execute(union_all({one_line}, other)).all()",
            ),
            ("in-an-exists", f"found = session.scalar(exists({one_line}).select())"),
            (
                "a-subquery",
                f"rows = session.execute({one_line}.subquery().select()).all()",
            ),
            ("a-cte", f"rows = session.execute({one_line}.cte().select()).all()"),
            ("a-helper", f"rows = session.scalars(paginate({one_line})).all()"),
            (
                "other-columns",
                f"rows = session.execute({one_line}.with_only_columns({entity}.id)).all()",
            ),
            (
                "insert-from-select",
                f'session.execute(insert(Archive).from_select(["id"], {one_line}))',
            ),
            ("not-run-by-a-session", f"rows = repository.scalars({one_line}).all()"),
            ("not-run", f"subquery = {one_line}.scalar_subquery()"),
        ]
    else:
        shapes = [
            ("other-entities", f"rows = {one_line}.with_entities({entity}.id).all()"),
            ("its-statement", f"rows = session.execute({one_line}.statement).all()"),
            ("a-helper", f"rows = paginate({one_line})"),
            ("a-union", f"rows = {one_line}.union_all(others).all()"),
            ("a-subquery", f"subquery = {one_line}.subquery()"),
            ("not-run", f"return {one_line}"),
        ]
    if _pick(rng, 0.5):
        reason, line = rng.choice(shapes)
        case.negative.append(reason)
        return [line], [0]
    reason, line = rng.choice(
        [
            ("two-statements", f"n = 0; rows = {run(one_line)}.all()"),
            ("conditional", f"rows = {run(one_line + ' if n else other')}.all()"),
            ("comprehension", f"rows = [{run(one_line)}.all() for _ in range(2)]"),
            ("second-query", f"rows = {run(one_line + f'.where({entity}.id.in_({other}))')}.all()"),
            ("another-statement-runs", f"rows = {run(one_line)}.all(), session.get({entity}, 1)"),
        ]
    )  # fmt: skip
    case.negative.append(reason)
    return [line], [0]


def generate(rng: random.Random) -> Case:
    entity, relationship, scalar, path = rng.choice(RELATIONSHIPS)
    case = Case([], 0, entity, relationship, scalar, path)
    style = rng.choice(["select", "query"])
    case.features.append(style)
    if _pick(rng, 0.3):
        case.reference = rng.choice([f"models.{entity}", f"app.models.{entity}"])
        case.features.append("through-a-module")
    lines = _header(rng, case, style)
    lines += _module_noise(rng, case)
    wrapper = rng.choice(["def", "async", "method", "static", "nested", "decorated"])
    case.features.append(f"wrapper:{wrapper}")
    asynchronous = wrapper == "async"
    method = wrapper == "method"
    params = rng.choice(["session", "session: 'Session'", "session, *args, **kwargs"])
    if _pick(rng, 0.04):
        case.negative.append("entity-is-a-parameter")
        params = f"{case.base}, {params}"
    type_params = ""
    if sys.version_info >= (3, 12) and _pick(rng, 0.1):
        type_params = "[T]"
        case.features.append("pep695")
    signature = f"def load{type_params}({params}):"
    if asynchronous:
        signature = "async " + signature
    depth = 1
    lines.append("")
    if wrapper in ("method", "static"):
        lines.append("class Repository:")
        if wrapper == "static":
            lines.append("    @staticmethod")
        else:
            signature = signature.replace("(", "(self, ", 1)
        lines.append("    " + signature)
        depth = 2
    elif wrapper == "nested":
        lines += ["def outer():", "    " + signature]
        depth = 2
    elif wrapper == "decorated":
        lines += ["@decorator", "@decorator  # select(Order)", signature]
    else:
        lines.append(signature)
    lines += ["    " * depth + line for line in _body_noise(rng, case)]
    statement, reported = _statement(rng, case, style, asynchronous, method)
    start = len(lines)
    lines += ["    " * depth + line for line in statement]
    case.line = start + rng.choice(reported) + 1
    if wrapper == "nested":
        lines.append("    return load")
    if _pick(rng, 0.05):
        case.function = "other"
        case.negative.append("another-function")
    case.lines = lines
    case.indent = rng.choice(["    ", "    ", "  ", "\t"])
    if _pick(rng, 0.3):
        case.newline = "\r\n"
    if _pick(rng, 0.2):
        case.final_newline = False
    if _pick(rng, 0.2):
        case.lines = [
            line + rng.choice([" ", "\t", "  "])
            if line.strip() and not line.endswith("\\") and _pick(rng, 0.3)
            else line
            for line in case.lines
        ]
        case.features.append("trailing-whitespace")
    if _pick(rng, 0.02):
        case.prefix = b"\xef\xbb\xbf"
        case.negative.append("bom")
    if _pick(rng, 0.02):
        case.lone_cr = True
        case.negative.append("lone-carriage-return")
    return case


def _finding(case: Case, filename: str) -> RelationshipFinding:
    site = CallSite(filename=filename, lineno=case.line, function=case.function)
    return RelationshipFinding(
        case.relationship,
        3,
        None,
        None,
        case.scalar,
        parent_site=site,
        other_parent_sites=0,
        parent_root=case.entity,
        parent_path=case.path,
    )


# --- the checks ---


class _Undo(ast.NodeTransformer):
    """Removes one option expression from every ``.options(...)`` call."""

    def __init__(self, option: str) -> None:
        self.option = option
        self.count = 0

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "options":
            kept = [arg for arg in node.args if ast.dump(arg) != self.option]
            removed = len(node.args) - len(kept)
            if removed:
                self.count += removed
                if not kept and not node.keywords:
                    return func.value
                node.args = kept
        return node


def _undo(tree: ast.Module, option: str, loaders: set[str]) -> tuple[str, int, int]:
    """(the dump without the option and loader imports, options, imports).

    Changes ``tree``.
    """
    undo = _Undo(option)
    tree = undo.visit(tree)
    imports = 0
    body: list[ast.stmt] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == _ORM and not stmt.level:
            names = [
                a for a in stmt.names if not (a.name in loaders and a.asname is None)
            ]
            imports += len(stmt.names) - len(names)
            if not names:
                continue
            stmt.names = names
        body.append(stmt)
    tree.body = body
    return ast.dump(tree), undo.count, imports


def _option(finding: RelationshipFinding, reference: str) -> str:
    """The loader option, with the query's class named as the query names it."""
    parts = []
    for loader, label in loader_steps(finding):
        owner, _, attribute = label.partition(".")
        if owner == finding.parent_root:
            owner = reference
        parts.append(f"{loader}({owner}.{attribute})")
    return ".".join(parts)


def _is_query_call(node: ast.AST, reference: str) -> bool:
    if not (isinstance(node, ast.Call) and len(node.args) == 1):
        return False
    arg = node.args[0]
    if not (
        isinstance(arg, (ast.Name, ast.Attribute)) and ast.unparse(arg) == reference
    ):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "select"
    return isinstance(func, ast.Attribute) and func.attr == "query"


def _option_receiver(tree: ast.Module, option: str, reference: str) -> list[int]:
    """The lines of the query calls that the new option is chained to."""
    found: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "options"
            and any(ast.dump(arg) == option for arg in node.args)
        ):
            continue
        current: ast.AST = node.func.value
        while not _is_query_call(current, reference):
            if not (
                isinstance(current, ast.Call)
                and isinstance(current.func, ast.Attribute)
            ):
                break
            current = current.func.value
        else:
            found.append(current.lineno)  # type: ignore[attr-defined]
    return found


def _apply_insertions(source: str, fix: SourceFix) -> str:
    lines = source.split("\n")
    for edit in sorted(fix.insertions, key=lambda e: (e.line, e.column), reverse=True):
        text = lines[edit.line - 1]
        prefix = text[: edit.column]
        assert len(prefix.encode("utf-16-le")) // 2 == edit.utf16_column
        lines[edit.line - 1] = prefix + edit.text + text[edit.column :]
    return "\n".join(lines)


def _git_apply(root: Path, fix: SourceFix, index: int) -> str:
    """The file after ``git apply --check`` and ``git apply`` of the patch.

    Applied to a copy, in another directory: the file itself stays as it is.
    """
    assert _GIT is not None
    copy = root / ".git-copy"
    target = copy / fix.file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((root / fix.file).read_bytes())
    patch = copy / f"fix{index}.patch"
    patch.write_bytes(fix.diff.encode("utf-8"))
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(root)}
    # Windows runners set core.autocrlf, which would rewrite the line ends
    plain = ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false"]
    for args in (["apply", "--check", str(patch)], ["apply", str(patch)]):
        done = subprocess.run(
            [_GIT, *plain, *args], cwd=copy, env=env, capture_output=True, check=False
        )
        assert done.returncode == 0, done.stderr.decode(errors="replace")
    return target.read_bytes().decode("utf-8")


def check(case: Case, root: Path, index: int, use_git: bool) -> SourceFix | None:
    """Write the case, make the patch and check it; returns the patch."""
    file = root / "app" / f"views{index}.py"
    file.parent.mkdir(exist_ok=True)
    file.write_bytes(case.data())
    source = case.source()
    finding = _finding(case, str(file))
    fix = fixpatch.make(finding, root, time.time() + 1)
    if fix is None:
        assert case.expect is not True, "no patch\n" + case.describe()
        return None
    assert case.expect is not False, f"a patch\n{fix.diff}\n" + case.describe()
    assert fix.file == f"app/views{index}.py"
    query_line = _query_line(ast.parse(source), case)  # the generator writes valid code
    verify(
        root,
        source,
        finding,
        fix,
        query_line,
        index,
        use_git,
        case.describe(),
        case.ref,
    )
    return fix


def verify(
    root: Path,
    source: str,
    finding: RelationshipFinding,
    fix: SourceFix,
    query_line: int,
    index: int,
    use_git: bool,
    context: str,
    reference: str | None = None,
) -> None:
    """The invariants of a patch made for ``source``.

    ``reference`` names the query's class as the query does (``models.Order``);
    by default its name.
    """
    original = ast.parse(source)
    by_insertions = _apply_insertions(source, fix)
    # (a) git applies it, to the same text as its insertions
    if use_git:
        assert _git_apply(root, fix, index) == by_insertions, context
    # (b) it parses, with the same line ends
    patched = ast.parse(by_insertions)
    if "\r\n" in source:
        assert "\n" not in by_insertions.replace("\r\n", "")
    else:
        assert "\r" not in by_insertions
    # (c) the AST changed by exactly the option, on the query, and the imports
    entity = finding.parent_root
    assert entity is not None
    reference = reference or entity
    option = _option(finding, reference)
    option_dump = ast.dump(ast.parse(option, mode="eval").body)
    loaders = {loader for loader, _ in loader_steps(finding)}
    # the new import lines all go above the query
    added = by_insertions.count("\n") - source.count("\n")
    receivers = _option_receiver(patched, option_dump, reference)
    # the module may have the same option on another query already
    assert query_line + added in receivers, f"{fix.diff}\n{context}"
    missing = loaders - _imported(original)
    before, options_before, imports_before = _undo(original, option_dump, loaders)
    after, options_after, imports_after = _undo(patched, option_dump, loaders)
    assert before == after, f"{fix.diff}\n{context}"
    assert options_after == options_before + 1
    assert imports_after - imports_before == len(missing)


def _imported(tree: ast.Module) -> set[str]:
    return {
        alias.name
        for stmt in tree.body
        if isinstance(stmt, ast.ImportFrom) and stmt.module == _ORM and not stmt.level
        for alias in stmt.names
        if alias.asname is None
    }


def _query_line(tree: ast.Module, case: Case) -> int:
    """The line of the module's one query call of the case's class."""
    lines = [node.lineno for node in ast.walk(tree) if _is_query_call(node, case.ref)]
    assert len(lines) == 1, case.describe()
    return lines[0]


# --- the tests ---


def test_generated_modules_give_a_correct_patch_or_none(tmp_path: Path) -> None:
    rng = random.Random(SEED)
    use_git = _GIT is not None
    outcomes = {True: 0, False: 0}
    started = time.perf_counter()
    for index in range(ROUNDS):
        case = generate(rng)
        fix = check(case, tmp_path, index, use_git)
        outcomes[fix is not None] += 1
    # the generator must exercise both outcomes
    assert outcomes[True] > ROUNDS // 5, outcomes
    assert outcomes[False] > ROUNDS // 10, outcomes
    assert time.perf_counter() - started < 60 * (10 if ROUNDS > 300 else 1)


def test_make_never_raises_on_mutated_source(tmp_path: Path) -> None:
    """Random edits of generated modules: no exception, and a patch still applies."""
    rng = random.Random(SEED + 1)
    soup = "()[]{}.,:;=#'\"\\\n\r\t xé😀\x0c\x00"
    for index in range(ROUNDS):
        case = generate(rng)
        data = bytearray(case.data())
        for _ in range(rng.randint(1, 4)):
            at = rng.randrange(len(data) + 1)
            if _pick(rng, 0.5) and data:
                del data[at : at + rng.randint(1, 3)]
            else:
                data[at:at] = rng.choice(soup).encode("utf-8")
        file = tmp_path / "app" / f"mutated{index}.py"
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(bytes(data))
        finding = _finding(case, str(file))
        fix = fixpatch.make(finding, tmp_path, time.time() + 1)
        if fix is None:
            continue
        # whatever was mutated, a patch made must still be a valid edit
        source = bytes(data).decode("utf-8")
        patched = _apply_insertions(source, fix)
        ast.parse(patched)
        if _GIT is not None:
            assert _git_apply(tmp_path, fix, index) == patched


def test_make_never_raises_for_odd_sites(tmp_path: Path) -> None:
    rng = random.Random(SEED + 2)
    case = generate(rng)
    file = tmp_path / "app" / "views.py"
    file.parent.mkdir()
    file.write_bytes(case.data())
    sites = [
        (str(file), 0, "load"),
        (str(file), -5, "load"),
        (str(file), 10**9, "load"),
        (str(file) + "-missing", 1, "load"),
        ("relative/views.py", 1, "load"),
        ("", 1, "load"),
        ("<string>", 1, "<module>"),
        (str(file) + "\x00", 1, "load"),
        (str(tmp_path), 1, "load"),
    ]
    for filename, line, function in sites:
        finding = RelationshipFinding(
            case.relationship,
            3,
            None,
            None,
            case.scalar,
            parent_site=CallSite(filename=filename, lineno=line, function=function),
            other_parent_sites=0,
            parent_root=case.entity,
            parent_path=case.path,
        )
        assert fixpatch.make(finding, tmp_path, time.time() + 1) is None
    for relationship, root in [
        ("Order", "Order"),
        ("Order.customer.extra", "Order"),
        ("Order.", "Order"),
        ("1.customer", "Order"),
        ("Order.customer", "Order.x"),
        ("Order.customer", ""),
    ]:
        for line in range(1, len(case.lines) + 1):
            finding = RelationshipFinding(
                relationship,
                3,
                None,
                None,
                True,
                parent_site=CallSite(filename=str(file), lineno=line, function="load"),
                other_parent_sites=0,
                parent_root=root,
                parent_path=(),
            )
            fixpatch.make(finding, tmp_path, time.time() + 1)


REPOSITORY = Path(__file__).resolve().parent.parent


def _co_name(parents: dict[ast.AST, ast.AST], node: ast.AST) -> str:
    scope = parents.get(node)
    while scope is not None:
        if isinstance(scope, ast.Lambda):
            return "<lambda>"
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return scope.name
        scope = parents.get(scope)
    return "<module>"


def test_the_repository_sources(tmp_path: Path) -> None:
    """A finding for every query call of this repository's own code.

    Its source, tests and examples: no exception, and every patch is correct.
    """
    files = sorted(
        path
        for folder in ("src", "tests", "examples")
        for path in (REPOSITORY / folder).rglob("*.py")
    )
    assert files
    calls = made = 0
    for index, path in enumerate(files):
        data = path.read_bytes()
        source = data.decode("utf-8")
        tree = ast.parse(source)
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        target = tmp_path / "app" / f"source{index}.py"
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Name)
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == "select")
                    or (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "query"
                    )
                )
            ):
                continue
            calls += 1
            entity = node.args[0].id
            for relationship, scalar, steps in [
                (f"{entity}.items", False, ()),
                ("Customer.country", True, ((f"{entity}.customer", True),)),
            ]:
                site = CallSite(
                    filename=str(target),
                    lineno=node.lineno,
                    function=_co_name(parents, node),
                )
                finding = RelationshipFinding(
                    relationship,
                    3,
                    None,
                    None,
                    scalar,
                    parent_site=site,
                    other_parent_sites=0,
                    parent_root=entity,
                    parent_path=steps,
                )
                fix = fixpatch.make(finding, tmp_path, time.time() + 1)
                if fix is not None:
                    made += 1
                    context = f"{path}:{node.lineno}"
                    verify(
                        tmp_path,
                        source,
                        finding,
                        fix,
                        node.lineno,
                        index,
                        _GIT is not None,
                        context,
                    )
    # the repository has query calls, and some of them get a patch
    assert calls > 50, calls
    assert made > 10, made


def test_every_line_of_a_generated_module_is_safe(tmp_path: Path) -> None:
    """A finding pointing at any line: a patch, if any, is still a correct one."""
    rng = random.Random(SEED + 3)
    rounds = max(ROUNDS // 10, 10)
    for index in range(rounds):
        case = generate(rng)
        file = tmp_path / "app" / f"lines{index}.py"
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(case.data())
        source = case.source()
        for line in range(1, source.count("\n") + 2):
            finding = _finding(
                Case(
                    case.lines,
                    line,
                    case.entity,
                    case.relationship,
                    case.scalar,
                    case.path,
                    reference=case.reference,
                ),
                str(file),
            )
            fix = fixpatch.make(finding, tmp_path, time.time() + 1)
            if fix is None:
                continue
            patched = _apply_insertions(source, fix)
            option = ast.dump(ast.parse(_option(finding, case.ref), mode="eval").body)
            loaders = {loader for loader, _ in loader_steps(finding)}
            before = _undo(ast.parse(source), option, loaders)
            after = _undo(ast.parse(patched), option, loaders)
            assert before[0] == after[0], f"line {line}\n{fix.diff}\n{source}"
            assert after[1] == before[1] + 1
