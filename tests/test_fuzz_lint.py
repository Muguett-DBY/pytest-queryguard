"""Seeded, property-style tests for the static check (lint.py).

A generator writes random but valid modules that mix loops, comprehensions,
queries, loader options and attribute reads with the rest of the language
(match, except*, PEP 695 generics and type aliases, t-strings, guarded by the
running Python). For each module:

- the check never raises and finishes quickly;
- every finding points at its relationship's name in the source;
- with options that load every relationship to any depth on every query,
  nothing is reported; nor when the reads are on a name no loop binds;
- blank lines above, CRLF line ends or tabs change only the lines and
  columns of the findings.

Plus pathological modules (deep nesting, huge files, long chains, name
cycles) and handwritten code that must not be reported. Standard library
only: the seed is fixed; QUERYGUARD_FUZZ=1 runs many more rounds.
"""

from __future__ import annotations

import ast
import os
import random
import re
import sys
import textwrap
import time
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from pytest_queryguard import lint
from tests.test_lint import HEADER, MODELS, MODULE

SEED = int(os.environ.get("QUERYGUARD_FUZZ_SEED", "20261106"))
ROUNDS = 2000 if os.environ.get("QUERYGUARD_FUZZ") == "1" else 150
SECONDS_PER_FILE = 20.0  # generous: loaded runners, coverage

RELATIONSHIPS = {
    "Order": ["customer", "items", "notes", "audits", "events", "payer"],
    "Customer": ["address", "orders"],
    "Item": ["product"],
}
READS = [
    "customer",
    "items",
    "address",
    "orders",
    "product",
    "country",
    "notes",
    "payer",
    "id",
    "name",
    "customer.address",
    "customer.address.country",
    "customer.orders",
    "awaitable_attrs.customer",
    "items",
]
# every relationship of every query, at any depth the generator reads
COVER = (
    '.options(raiseload("*"), defaultload("*").raiseload("*"), '
    'defaultload("*").defaultload("*").raiseload("*"), '
    'defaultload("*").defaultload("*").defaultload("*").raiseload("*"), '
    'defaultload("*").defaultload("*").defaultload("*").defaultload("*")'
    '.raiseload("*"))'
)


@pytest.fixture(scope="module")
def models(tmp_path_factory: pytest.TempPathFactory) -> Iterator[lint.Models]:
    root = tmp_path_factory.mktemp("models")
    (root / f"{MODULE}.py").write_text(MODELS, encoding="utf-8")
    sys.path.insert(0, str(root))
    try:
        yield lint.load_models([MODULE])
    finally:
        sys.path.remove(str(root))


@dataclass(frozen=True)
class Context:
    function: bool = False
    asynchronous: bool = False
    generator: bool = False  # yield is allowed
    loop: bool = False  # break and continue are allowed
    jumps: bool = True  # not in except*, where break, continue, return are not
    variables: tuple[str, ...] = ()  # loop variables in scope


class Generator:
    """A random module. ``mode`` changes the text, never the random choices.

    "plain", "covered" (every query loads everything) or "other" (reads are
    on a name that no loop binds).
    """

    def __init__(self, rng: random.Random, mode: str = "plain") -> None:
        self.rng = rng
        self.mode = mode
        self.lines: list[str] = []
        self.counter = 0
        self.budget = 0

    def module(self) -> str:
        self.lines = HEADER.rstrip("\n").split("\n")
        self.lines += ["opts = []", "G = 0", ""]
        self.budget = self.rng.randint(5, 60)
        context = Context()
        while self.budget > 0:
            self.statement(0, context)
        return "\n".join(self.lines) + "\n"

    # -- helpers --

    def pick(self, chance: float) -> bool:
        return self.rng.random() < chance

    def name(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def emit(self, depth: int, text: str) -> None:
        for line in text.split("\n"):
            self.lines.append("    " * depth + line)

    def variable(self, context: Context) -> str:
        if context.variables and self.pick(0.8):
            var = self.rng.choice(context.variables)
        else:
            var = self.rng.choice(["row", "o", "other", "item"])
        return "other" if self.mode == "other" else var

    def read(self, context: Context) -> str:
        return f"{self.variable(context)}.{self.rng.choice(READS)}"

    def options(self, entity: str) -> str:
        rel = self.rng.choice(RELATIONSHIPS[entity])
        option = self.rng.choice(
            [
                "",
                "",
                f".options(selectinload({entity}.{rel}))",
                f".options(joinedload({entity}.{rel}))",
                f".options(lazyload({entity}.{rel}))",
                f".options(defaultload({entity}.{rel}).selectinload(Customer.orders))",
                ".options(*opts)",
                f".options(Load({entity}).lazyload('*'))",
                f".options(orm.selectinload({entity}.{rel}))",
                f".options(sa.orm.joinedload({entity}.{rel}))",
                f".options(load_only({entity}.id))",
                f".options(contains_eager({entity}.{rel}))",
                ".options(raiseload('*'))",
                f".options(selectinload({entity}.{rel}.of_type(Item)))",
            ]
        )
        if self.mode == "covered" and "lazyload(" in option and "Load(" not in option:
            # a named lazyload overrides a "*" option: drop it
            option = ""
        filters = ["", ".where(Order.id > 1)", ".limit(3)", ".order_by(Order.id)"]
        chain = self.rng.choice(filters) + option + self.rng.choice(filters)
        return chain + (COVER if self.mode == "covered" else "")

    def query(self, depth: int, context: Context) -> str:
        """An expression that iterates over a query (or does not, for rows)."""
        entity = self.rng.choice(list(RELATIONSHIPS))
        select = self.rng.choice(["select", "sa.select"])
        statement = f"{select}({entity}){self.options(entity)}"
        query = f"session.query({entity}){self.options(entity)}"
        shapes = [
            f"session.scalars({statement})",
            f"session.scalars({statement}).all()",
            f"session.execute({statement}).scalars()",
            f"session.execute({statement})",  # rows: not followed
            query,
            f"{query}.all()",
            f"list(session.scalars({statement}))",
            f"sorted({query}, key=lambda r: r.customer)",
            "stmt",
        ]
        if context.asynchronous:
            shapes += [
                f"await session.scalars({statement})",
                f"(await session.execute({statement})).scalars().all()",
            ]
        shape = self.rng.choice(shapes)
        if shape == "stmt":
            name = self.name("stmt")
            self.emit(depth, f"{name} = {statement}")
            if self.pick(0.2):
                self.emit(depth, f"{name} = {name}.limit(1)")  # assigned twice
            shape = f"session.scalars({name})"
        return shape

    def block(self, depth: int, context: Context) -> None:
        start = len(self.lines)
        for _ in range(self.rng.randint(1, 4)):
            if self.budget <= 0:
                break
            self.statement(depth, context)
        if len(self.lines) == start:
            self.emit(depth, "pass")

    # -- statements --

    def statement(self, depth: int, context: Context) -> None:
        self.budget -= 1
        kinds = ["read", "read", "assign", "for", "for", "comprehension", "if"]
        if depth < 8:
            kinds += ["while", "try", "with", "match", "def", "class", "walrus"]
        kinds += ["lambda", "fstring", "star", "del", "annotated"]
        if context.loop and context.jumps:
            kinds += ["break", "continue"]
        if context.function and context.jumps:
            kinds.append("return")
        if context.generator:
            kinds.append("yield")
        if sys.version_info >= (3, 11) and depth < 8:
            kinds.append("except-star")
        if sys.version_info >= (3, 12):
            kinds += ["type-alias", "fstring-nested"]
        if sys.version_info >= (3, 14):
            kinds += ["tstring", "except-no-parens"]
        getattr(self, "s_" + self.rng.choice(kinds).replace("-", "_"))(depth, context)

    def s_read(self, depth: int, context: Context) -> None:
        read = self.read(context)
        self.emit(
            depth,
            self.rng.choice(
                [f"print({read})", read, f"total = len({read})", f"{read}.id"]
            ),
        )

    def s_assign(self, depth: int, context: Context) -> None:
        name = self.name("x")
        self.emit(depth, f"{name} = {self.read(context)}")

    def s_for(self, depth: int, context: Context) -> None:
        var = self.rng.choice(["row", "o", "order", "c", self.name("v")])
        shape = self.rng.choice(["query", "query", "enumerate", "range", "nested"])
        if shape == "nested" and not context.variables:
            shape = "query"
        if shape == "range":
            header = f"for {var} in range(3):"
        elif shape == "nested":
            owner = self.variable(context)
            header = f"for {var} in {owner}.{self.rng.choice(['items', 'orders'])}:"
        else:
            iterable = self.query(depth, context)
            if shape == "enumerate":
                header = f"for i, {var} in enumerate({iterable}):"
            else:
                header = f"for {var} in {iterable}:"
        if context.asynchronous and self.pick(0.2):
            entity = self.rng.choice(list(RELATIONSHIPS))
            iterable = f"session.stream_scalars(select({entity}){self.options(entity)})"
            header = f"async for {var} in await {iterable}:"
        self.emit(depth, header)
        inner = replace(context, loop=True, variables=(*context.variables, var))
        if self.pick(0.15):
            self.emit(depth + 1, f"{var} = None")  # the loop variable rebound
        self.block(depth + 1, inner)
        if self.pick(0.15):
            self.emit(depth, "else:")
            self.block(depth + 1, context)

    def s_comprehension(self, depth: int, context: Context) -> None:
        var = self.rng.choice(["o", "row", self.name("v")])
        iterable = self.query(depth, context)
        inner = replace(context, variables=(*context.variables, var))
        read = self.read(inner)
        shape = self.rng.randrange(6)
        if shape == 0:
            text = f"[{read} for {var} in {iterable}]"
        elif shape == 1:
            text = f"{{{read} for {var} in {iterable} if {var}.id}}"
        elif shape == 2:
            text = f"{{{var}.id: {read} for {var} in {iterable}}}"
        elif shape == 3:
            text = f"any({read} for {var} in {iterable})"
        elif shape == 4:
            item = self.name("i")
            items = "other" if self.mode == "other" else var
            text = (
                f"[{item}.product for {var} in {iterable} for {item} in {items}.items]"
            )
        else:
            text = f"[[{read} for _ in range(2)] for {var} in {iterable}]"
        self.emit(depth, f"{self.name('values')} = {text}")

    def s_if(self, depth: int, context: Context) -> None:
        self.emit(depth, f"if {self.read(context)}:")
        self.block(depth + 1, context)
        for _ in range(self.rng.randint(0, 2)):
            self.emit(depth, f"elif {self.read(context)} is None:")
            self.block(depth + 1, context)
        if self.pick(0.4):
            self.emit(depth, "else:")
            self.block(depth + 1, context)

    def s_while(self, depth: int, context: Context) -> None:
        self.emit(depth, "while G < 3:")
        self.block(depth + 1, replace(context, loop=True))
        self.emit(depth + 1, "break")

    def s_try(self, depth: int, context: Context) -> None:
        self.emit(depth, "try:")
        self.block(depth + 1, context)
        self.emit(depth, f"except (ValueError, KeyError) as {self.name('error')}:")
        self.block(depth + 1, context)
        if self.pick(0.3):
            self.emit(depth, "else:")
            self.block(depth + 1, context)
        if self.pick(0.3):
            self.emit(depth, "finally:")
            self.block(depth + 1, context)

    def s_except_star(self, depth: int, context: Context) -> None:
        self.emit(depth, "try:")
        self.block(depth + 1, context)
        self.emit(depth, "except* ValueError:")
        self.block(depth + 1, replace(context, jumps=False))

    def s_except_no_parens(self, depth: int, context: Context) -> None:
        self.emit(depth, "try:\n    pass\nexcept ValueError, KeyError:")
        self.block(depth + 1, context)

    def s_with(self, depth: int, context: Context) -> None:
        prefix = "async " if context.asynchronous and self.pick(0.5) else ""
        self.emit(depth, f"{prefix}with session.begin() as {self.name('tx')}:")
        self.block(depth + 1, context)

    def s_match(self, depth: int, context: Context) -> None:
        self.emit(depth, f"match {self.read(context)}:")
        cases = [
            f"case Order(customer={self.name('c')}):",
            f'case {{"key": {self.name("c")}, **{self.name("rest")}}}:',
            f"case [{self.name('c')}, *{self.name('rest')}]:",
            "case 1 | 2:",
            f"case str() as {self.name('c')} if {self.read(context)}:",
        ]
        for case in self.rng.sample(cases, self.rng.randint(1, 3)):
            self.emit(depth + 1, case)
            self.block(depth + 2, context)
        if self.pick(0.5):
            self.emit(depth + 1, "case _:")
            self.block(depth + 2, context)

    def s_def(self, depth: int, context: Context) -> None:
        asynchronous = self.pick(0.3)
        generator = not asynchronous and self.pick(0.2)
        type_params = ""
        if sys.version_info >= (3, 12) and self.pick(0.2):
            type_params = "[T, *Ts, **P]"
            if sys.version_info >= (3, 13) and self.pick(0.5):
                type_params = "[T = int]"
        if self.pick(0.2):
            self.emit(depth, "@staticmethod")
        prefix = "async def" if asynchronous else "def"
        params = self.rng.choice(
            ["session", "session, /, *args, other=None", "session, **kw"]
        )
        self.emit(depth, f"{prefix} {self.name('load')}{type_params}({params}):")
        if self.pick(0.2):
            self.emit(depth + 1, "global G")
        if context.function and self.pick(0.3):
            self.emit(depth + 1, "nonlocal nl")
        self.emit(depth + 1, "nl = 0")
        inner = Context(function=True, asynchronous=asynchronous, generator=generator)
        self.block(depth + 1, inner)

    def s_class(self, depth: int, context: Context) -> None:
        generic = "[T]" if sys.version_info >= (3, 12) and self.pick(0.2) else ""
        self.emit(depth, f"class {self.name('Repository')}{generic}:")
        self.emit(depth + 1, "session = None")
        self.s_def(depth + 1, context)

    def s_walrus(self, depth: int, context: Context) -> None:
        self.emit(depth, f"if ({self.name('w')} := {self.read(context)}):")
        self.block(depth + 1, context)

    def s_lambda(self, depth: int, context: Context) -> None:
        var = self.variable(context)
        self.emit(depth, f"{self.name('f')} = lambda {var}: {var}.customer")

    def s_fstring(self, depth: int, context: Context) -> None:
        self.emit(depth, f"print(f\"{{{self.read(context)}!r:>10}} {{'x'}}\")")

    def s_fstring_nested(self, depth: int, context: Context) -> None:
        self.emit(depth, f'print(f"{{f"{{{self.read(context)}}}"}}")')

    def s_tstring(self, depth: int, context: Context) -> None:
        self.emit(depth, f'{self.name("t")} = t"{{{self.read(context)}}}"')

    def s_star(self, depth: int, context: Context) -> None:
        first, rest = self.name("first"), self.name("rest")
        self.emit(depth, f"{first}, *{rest} = {self.query(depth, context)}")

    def s_del(self, depth: int, context: Context) -> None:
        name = self.name("d")
        self.emit(depth, f"{name} = 1\ndel {name}")

    def s_annotated(self, depth: int, context: Context) -> None:
        self.emit(depth, f"{self.name('a')}: int = {self.read(context)}")

    def s_type_alias(self, depth: int, context: Context) -> None:
        self.emit(depth, f"type {self.name('Alias')}[X] = list[X]")

    def s_break(self, depth: int, context: Context) -> None:
        self.emit(depth, "break")

    def s_continue(self, depth: int, context: Context) -> None:
        self.emit(depth, "continue")

    def s_return(self, depth: int, context: Context) -> None:
        value = "" if context.generator else f" {self.read(context)}"
        self.emit(depth, f"return{value}")

    def s_yield(self, depth: int, context: Context) -> None:
        self.emit(depth, f"yield {self.read(context)}")


def generate(seed: int, mode: str = "plain") -> str:
    source = Generator(random.Random(seed), mode).module()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compile(
            source, f"generated{seed}.py", "exec"
        )  # the generator writes valid code
    return source


def check(models: lint.Models, source: str, name: str = "gen.py") -> list[lint.Finding]:
    started = time.perf_counter()
    findings = lint.lint_source(source, name, models)
    elapsed = time.perf_counter() - started
    assert elapsed < SECONDS_PER_FILE, f"{elapsed:.1f} s for {name}"
    lines = re.split(r"\r\n|\r|\n", source)  # as ast counts lines
    for finding in findings:
        assert 0 < finding.line <= len(lines)
        key = finding.relationship.split(".", 1)[1]
        text = lines[finding.line - 1]
        assert text[finding.column - 1 :].startswith(key), (finding, text)
    return findings


def _identity(finding: lint.Finding, shift: int = 0) -> tuple[object, ...]:
    return (
        finding.line - shift,
        finding.loop_line - shift,
        finding.query_line - shift,
        finding.function,
        finding.path,
    )


# --- generated modules ---


def test_generated_modules(models: lint.Models) -> None:
    reported = 0
    started = time.perf_counter()
    for round_ in range(ROUNDS):
        seed = SEED + round_
        source = generate(seed)
        findings = check(models, source, f"generated{seed}.py")
        reported += bool(findings)
        # loaded with the parents, to any depth: nothing to report
        covered = check(models, generate(seed, "covered"))
        assert covered == [], (seed, covered)
        # reads on a name no loop binds: nothing to report
        assert check(models, generate(seed, "other")) == [], seed
        # blank lines and comments above, CRLF, tabs: the same findings
        extra = "# queryguard lint fuzz\n\n# select(Order)\n"
        head, _, body = source.partition("opts = []\n")
        shifted = check(models, f"{head}{extra}opts = []\n{body}".replace("\n", "\r\n"))
        assert [_identity(f, 3) for f in shifted] == [_identity(f) for f in findings]
        tabbed = "\n".join(
            "\t" * ((len(line) - len(line.lstrip(" "))) // 4) + line.lstrip(" ")
            for line in source.split("\n")
        )
        assert [_identity(f) for f in check(models, tabbed)] == [
            _identity(f) for f in findings
        ]
    # the generator must make code the check reports
    assert reported > ROUNDS // 5, reported
    assert time.perf_counter() - started < 60 * (20 if ROUNDS > 150 else 1)


def test_mutated_modules_never_raise(models: lint.Models) -> None:
    """Random edits: a SyntaxError or a result, nothing else."""
    rng = random.Random(SEED)
    soup = "()[]{}.,:;=#'\"\\\n\r\t xé😀\x0c"
    for round_ in range(ROUNDS):
        source = list(generate(SEED + 10_000 + round_))
        for _ in range(rng.randint(1, 4)):
            at = rng.randrange(len(source) + 1)
            if rng.random() < 0.5:
                del source[at : at + rng.randint(1, 3)]
            else:
                source[at:at] = rng.choice(soup)
        try:
            check(models, "".join(source))
        except SyntaxError:
            pass


def test_the_repository_sources(models: lint.Models) -> None:
    """This repository's source, tests and examples: no exception, no error."""
    root = Path(__file__).resolve().parent.parent
    started = time.perf_counter()
    report = lint.lint([root / "src", root / "tests", root / "examples"], models)
    assert report.files > 100
    assert report.errors == []
    assert time.perf_counter() - started < 30


# --- pathological modules ---

LOOP = """\
def load(session):
    for o in session.scalars(select(Order)):
        print(o.customer)
"""


def _deep_ifs(depth: int) -> str:
    lines = ["def load(session):", "    for o in session.scalars(select(Order)):"]
    lines += ["    " * (level + 2) + "if o:" for level in range(depth)]
    lines.append("    " * (depth + 2) + "print(o.customer)")
    return "\n".join(lines) + "\n"


def _loop_reading(expression: str) -> str:
    return (
        "def load(session):\n"
        "    for o in session.scalars(select(Order)):\n"
        f"        x = {expression}\n"
    )


PATHOLOGICAL = {
    "long-string-sum": "x = " + " + ".join(["'a'"] * 3000) + "\n" + LOOP,
    "long-sum-in-the-loop": _loop_reading(" + ".join(["o.customer"] * 1500)),
    "long-attribute-chain": _loop_reading("o" + ".customer" * 3000),
    "long-conditional-chain": _loop_reading(" if o else ".join(["o.customer"] * 1500)),
    "long-method-chain": (
        "def load(session):\n"
        "    for o in session.scalars(select(Order)"
        + ".where(Order.id > 1)" * 3000
        + "):\n        x = o.customer\n"
    ),
    "long-loader-chain": (
        "def load(session):\n"
        "    for o in session.scalars(select(Order).options(joinedload(Order.customer)"
        + ".joinedload(Customer.orders)" * 2000
        + ")):\n        x = o.customer\n"
    ),
    "long-elif-chain": (
        "def load(session):\n"
        "    for o in session.scalars(select(Order)):\n"
        "        if o.id == 0:\n            pass\n"
        + "".join(
            f"        elif o.id == {i}:\n            print(o.customer)\n"
            for i in range(1, 2000)
        )
    ),
    "deep-ifs": _deep_ifs(95),
    "deep-parentheses": _loop_reading("(" * 150 + "o.customer" + ")" * 150),
    "deep-lambdas": _loop_reading("lambda: " * 300 + "o.customer"),
    "many-generators": (
        "def load(session):\n    x = [o.customer "
        + " ".join(f"for o{i} in session.scalars(select(Order))" for i in range(1500))
        + " for o in session.scalars(select(Order))]\n"
    ),
    "many-nested-generators": (
        "def load(session):\n    x = [o.customer for o in session.scalars(select(Order)) "
        + " ".join("for o in o.items" for _ in range(1500))
        + "]\n"
    ),
    "huge-file": "".join(LOOP.replace("def load", f"def load{i}") for i in range(4000)),
    "assignment-chain": (
        "def load(session):\n    s0 = select(Order)\n"
        + "".join(f"    s{i} = s{i - 1}\n" for i in range(1, 1000))
        + "    for o in session.scalars(s999):\n        o.customer\n"
    ),
    "name-cycle": (
        "def load(session):\n    a = b\n    b = c\n    c = a\n"
        "    for o in session.scalars(a):\n        o.customer\n"
    ),
    "self-reference": (
        "def load(session):\n    stmt = stmt\n"
        "    for o in session.scalars(stmt):\n        o.customer\n"
    ),
    "deeply-nested-functions": "".join(
        "    " * level + f"def f{level}(session):\n" for level in range(90)
    )
    + textwrap.indent(LOOP, "    " * 90),
    "deeply-nested-classes": "".join(
        "    " * level + f"class C{level}:\n" for level in range(90)
    )
    + textwrap.indent(LOOP, "    " * 90),
}


@pytest.mark.parametrize("name", PATHOLOGICAL)
def test_pathological_modules(models: lint.Models, name: str) -> None:
    source = HEADER + PATHOLOGICAL[name]
    try:
        compile(source, name, "exec", ast.PyCF_ONLY_AST)
    except (RecursionError, MemoryError):  # pragma: no cover - too deep for ast here
        pytest.skip("too deep for this Python's parser")
    check(models, source, name)


def test_code_nested_deeper_than_ast_parses_is_skipped_with_an_error(
    models: lint.Models, tmp_path: Path
) -> None:
    path = tmp_path / "deep.py"
    path.write_text(HEADER + "x = " + " + ".join(["1"] * 100_000) + "\n" + LOOP)
    report = lint.lint([path], models)
    assert report.files == 1
    found = [f.relationship for f in report.findings]
    # a RecursionError of ast.parse, unless this Python parses it
    assert (len(report.errors) == 1 and found == []) or found == ["Order.customer"]


# --- what must not be reported ---

NOT_REPORTED = {
    "read-after-the-loop": """
        def load(session):
            for o in session.scalars(select(Order)):
                pass
            print(o.customer)
        """,
    "read-in-the-else-of-the-loop": """
        def load(session):
            for o in session.scalars(select(Order)):
                pass
            else:
                print(o.customer)
        """,
    "read-in-a-lambda": """
        def load(session):
            for o in session.scalars(select(Order)):
                session.run_sync(lambda s: o.customer)
        """,
    "read-in-a-nested-function": """
        def load(session):
            for o in session.scalars(select(Order)):
                def show():
                    return o.customer
        """,
    "read-in-a-nested-class": """
        def load(session):
            for o in session.scalars(select(Order)):
                class Show:
                    value = o.customer
        """,
    "shadowed-by-a-comprehension": """
        def load(session, others):
            for o in session.scalars(select(Order)):
                names = [o.customer for o in others]
        """,
    "rebound-by-a-walrus": """
        def load(session, others):
            for o in session.scalars(select(Order)):
                if (o := others[0]):
                    print(o.customer)
        """,
    "rebound-by-a-match-capture": """
        def load(session, value):
            for o in session.scalars(select(Order)):
                match value:
                    case [o]:
                        print(o.customer)
        """,
    "rebound-by-with": """
        def load(session, ctx):
            for o in session.scalars(select(Order)):
                with ctx as o:
                    print(o.customer)
        """,
    "rebound-by-an-inner-loop": """
        def load(session, others):
            for o in session.scalars(select(Order)):
                for o in others:
                    print(o.customer)
        """,
    "rebound-by-except-as": """
        def load(session):
            for o in session.scalars(select(Order)):
                try:
                    pass
                except ValueError as o:
                    print(o.customer)
        """,
    "rebound-by-an-import": """
        def load(session):
            for o in session.scalars(select(Order)):
                import json as o
                print(o.customer)
        """,
    "rebound-by-a-def": """
        def load(session):
            for o in session.scalars(select(Order)):
                def o():
                    pass
                print(o.customer)
        """,
    "rebound-through-nonlocal": """
        def load(session):
            for o in session.scalars(select(Order)):
                def change():
                    nonlocal o
                    o = None
                print(o.customer)
        """,
    "rows-not-entities": """
        def load(session):
            for o in session.execute(select(Order)):
                print(o.customer)
        """,
    "statement-from-a-parameter": """
        def load(session, stmt):
            for o in session.scalars(stmt):
                print(o.customer)
        """,
    "statement-assigned-in-a-branch-and-after": """
        def load(session, flag):
            if flag:
                stmt = select(Order)
            else:
                stmt = select(Order).options(joinedload(Order.customer))
            for o in session.scalars(stmt):
                print(o.customer)
        """,
    "statement-made-by-another-function": """
        def load(session):
            for o in session.scalars(build()):
                print(o.customer)
        """,
    "an-option-from-a-variable": """
        def load(session, option):
            for o in session.scalars(select(Order).options(option)):
                print(o.customer)
        """,
    "a-wildcard-loader": """
        def load(session):
            for o in session.scalars(select(Order).options(selectinload("*"))):
                print(o.customer, o.items)
        """,
    "a-loader-under-load": """
        def load(session):
            for o in session.scalars(select(Order).options(Load(Order).joinedload(Order.customer))):
                print(o.customer)
        """,
    "an-unknown-attribute": """
        def load(session):
            for o in session.scalars(select(Order)):
                print(o.total, o.customer_id, o.id)
        """,
    "a-write-not-a-read": """
        def load(session, customer):
            for o in session.scalars(select(Order)):
                o.customer = customer
                del o.items
        """,
    "a-select-of-columns": """
        def load(session):
            for o in session.scalars(select(Order.id)):
                print(o.customer)
        """,
    "a-select-of-two-entities": """
        def load(session):
            for o in session.scalars(select(Order, Customer)):
                print(o.customer)
        """,
    "a-return-in-the-loop": """
        def load(session):
            for o in session.scalars(select(Order)):
                return o.customer
        """,
    "a-raise-in-the-loop": """
        def load(session):
            for o in session.scalars(select(Order)):
                raise ValueError(o.customer)
        """,
    "a-break-after-each-branch": """
        def load(session):
            for o in session.scalars(select(Order)):
                if o.id:
                    print(o.customer)
                    break
                elif o.id is None:
                    print(o.items)
                    break
                else:
                    break
        """,
    "a-string-and-a-comment": """
        def load(session):
            for o in session.scalars(select(Order)):
                print("o.customer")  # o.customer
        """,
    "a-local-select": """
        def load(session):
            from app.queries import select
            for o in session.scalars(select(Order)):
                print(o.customer)
        """,
}


@pytest.mark.parametrize("name", NOT_REPORTED)
def test_not_reported(models: lint.Models, name: str) -> None:
    source = HEADER + textwrap.dedent(NOT_REPORTED[name])
    assert check(models, source, name) == []
