"""Seeded fuzzing of the detection path: random SQLAlchemy programs run under the plugin.

A generator writes small but valid SQLAlchemy programs: a mapped schema of one
to four classes (many-to-one, one-to-many, many-to-many and self-referential
relationships, sometimes joined or single-table inheritance, random ``lazy=``
strategies), random rows, and a few query steps (``select()`` or
``session.query()`` with chains of loader options, ``where``/``limit``/
``order_by``, ``session.get``, Core and ``text()`` statements,
``populate_existing``, ``commit``/``expire``/``refresh`` in between), each
followed by an access pattern over its result (loops over columns and
relationships, nested loops, comprehensions, early ``break``). Some programs
run on an async engine (aiosqlite) through ``AsyncSession.run_sync``.

Each batch of programs runs as one pytest session per configuration, in a
subprocess, with random plugin settings (thresholds, scope, the opt-in
kinds, the event cap, the stack depth). Oracles:

1. The plugin never fails on its own: no ``QueryGuardInternalWarning``, no
   ``INTERNALERROR``, no "measurement stopped", no warning that the run
   without the plugin does not have, and a test fails only with a queryguard
   finding or ``NPlusOneDetected``.
2. Metamorphic: every program's results (the values it read) are the same
   with the plugin as with ``-p no:queryguard``; a program wrapped in
   ``ignore("*")`` has no finding; adding ``selectinload`` for each
   relationship the program reads on the objects of its own queries removes
   those relationship-load findings.
3. Strict mode raises ``NPlusOneDetected`` in a test if and only if the report
   of the same run has a relationship-load finding for it.
4. Two runs give the same findings and statement counts.

Documented behaviour the oracles leave out: a test that went over
``queryguard_max_events`` (the report's counts are lower bounds and
``ignore()`` blocks leave nothing out); a relationship whose parents include
one of unknown origin (an object that two queries or load paths read: the
report counts it with every query line, strict mode never); for the
selectinload oracle, programs that commit, expire, refresh or repopulate
(their relationships reload lazily by design), relationships read only
through another relationship, and ``session.get()`` of a class whose objects
may already be in the identity map (it returns them without a query, so its
options do nothing); and the SAWarning in ``DOCUMENTED_WARNINGS``. Oracles
3 and 4 skip a test whose SQL differs between the runs: SQLAlchemy may run
the eager loaders of a self-referential schema in another order from one
process to the next (with or without the plugin), and then loads more or
fewer relationships lazily.

Standard library only: the seed is fixed (``QUERYGUARD_FUZZ_SEED``), so a
failure reproduces exactly; QUERYGUARD_FUZZ=1 runs many more batches, and
``QUERYGUARD_FUZZ_KEEP=DIR`` keeps the generated projects there.
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import re
import shutil
import textwrap
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest

SEED = int(os.environ.get("QUERYGUARD_FUZZ_SEED", "20261107"))
LONG = os.environ.get("QUERYGUARD_FUZZ") == "1"
BATCHES = 40 if LONG else 2
PROGRAMS = 8
HAS_AIOSQLITE = importlib.util.find_spec("aiosqlite") is not None

# Without a relationship option, the strategies a relationship() may declare.
LAZY = ["select"] * 6 + [
    "joined",
    "selectin",
    "subquery",
    "immediate",
    "raise",
    "noload",
]
LOADERS = [
    "selectinload",
    "joinedload",
    "subqueryload",
    "lazyload",
    "raiseload",
    "noload",
    "immediateload",
    "defaultload",
]


# ------------------------------------------------------------------- schema


@dataclass
class Cls:
    name: str
    table: str | None  # None: single-table subclass
    base: str | None = None  # the mapped superclass
    polymorphic: bool = False  # the base of a hierarchy (has a "kind" column)
    extra: int = 0  # c0..cN integer columns
    fks: dict[str, str] = field(default_factory=dict)  # column -> target class


@dataclass
class Rel:
    name: str
    owner: str
    target: str
    kind: str  # m2o, o2m, m2m
    lazy: str
    fk: tuple[str, str] | None = None  # (class holding the column, column)
    secondary: str | None = None
    back: str | None = None
    self_ref: bool = False


@dataclass
class Schema:
    classes: dict[str, Cls]
    rels: list[Rel]
    m2m: list[tuple[str, str, str]]  # (table, left class, right class)

    def lineage(self, name: str) -> list[str]:
        out = [name]
        while self.classes[out[-1]].base is not None:
            out.append(self.classes[out[-1]].base)  # type: ignore[arg-type]
        return out

    def root(self, name: str) -> str:
        return self.lineage(name)[-1]

    def table_of(self, name: str) -> str:
        for c in self.lineage(name):
            table = self.classes[c].table
            if table is not None:
                return table
        raise AssertionError(name)

    def rels_of(self, name: str) -> list[Rel]:
        owners = set(self.lineage(name))
        return [r for r in self.rels if r.owner in owners]

    def rel(self, name: str) -> Rel:
        return next(r for r in self.rels if r.name == name)

    def same_tree(self, a: str, b: str) -> bool:
        return self.root(a) == self.root(b)


def _schema(rng: random.Random) -> Schema:
    count = rng.randint(1, 4)
    names = ["A", "B", "C", "D"][:count]
    classes: dict[str, Cls] = {
        n: Cls(n, f"t_{n.lower()}", extra=rng.choice([0, 0, 1, 3, 8])) for n in names
    }
    if count >= 2 and rng.random() < 0.35:
        base, sub = names[0], names[1]
        classes[base].polymorphic = True
        classes[sub].base = base
        if rng.random() < 0.5:
            classes[sub].table = None
            classes[sub].extra = 0
    rels: list[Rel] = []
    m2m: list[tuple[str, str, str]] = []
    counter = iter(range(100))

    def fk_target_ok(holder: str, target: str) -> bool:
        if classes[holder].table is None:
            return False  # single-table subclass: no columns of its own
        if classes[holder].base is not None or classes[holder].polymorphic:
            # a hierarchy's own tables point only outside it: no ambiguous
            # inheritance join
            hierarchy = {
                n for n in names if _root(classes, n) == _root(classes, holder)
            }
            return target not in hierarchy
        return True

    for _ in range(rng.randint(1, 4)):
        shape = rng.random()
        if shape < 0.15:
            # self-referential adjacency list
            plain = [
                n
                for n in names
                if classes[n].base is None and not classes[n].polymorphic
            ]
            if not plain:
                continue
            c = rng.choice(plain)
            col = f"f{len(classes[c].fks)}_{next(counter)}"
            classes[c].fks[col] = c
            want = rng.choice(["m2o", "o2m", "both"])
            up = Rel(
                f"r{next(counter)}",
                c,
                c,
                "m2o",
                rng.choice(LAZY),
                (c, col),
                self_ref=True,
            )
            down = Rel(
                f"r{next(counter)}",
                c,
                c,
                "o2m",
                rng.choice(LAZY),
                (c, col),
                self_ref=True,
            )
            if want == "both":
                up.back, down.back = down.name, up.name
            rels += [up] if want == "m2o" else [down] if want == "o2m" else [up, down]
        elif shape < 0.75:
            holder, target = rng.choice(names), rng.choice(names)
            if holder == target or not fk_target_ok(holder, target):
                continue
            col = f"f{len(classes[holder].fks)}_{next(counter)}"
            classes[holder].fks[col] = target
            want = rng.choice(["m2o", "o2m", "both", "m2o"])
            up = Rel(
                f"r{next(counter)}",
                holder,
                target,
                "m2o",
                rng.choice(LAZY),
                (holder, col),
            )
            down = Rel(
                f"r{next(counter)}",
                target,
                holder,
                "o2m",
                rng.choice(LAZY),
                (holder, col),
            )
            if want == "both":
                up.back, down.back = down.name, up.name
            rels += [up] if want == "m2o" else [down] if want == "o2m" else [up, down]
        else:
            left, right = rng.choice(names), rng.choice(names)
            if _root(classes, left) == _root(classes, right):
                continue
            table = f"m_{next(counter)}"
            m2m.append((table, left, right))
            there = Rel(
                f"r{next(counter)}",
                left,
                right,
                "m2m",
                rng.choice(LAZY),
                secondary=table,
            )
            rels.append(there)
            if rng.random() < 0.5:
                back = Rel(
                    f"r{next(counter)}",
                    right,
                    left,
                    "m2m",
                    rng.choice(LAZY),
                    secondary=table,
                )
                there.back, back.back = back.name, there.name
                rels.append(back)
    return Schema(classes, rels, m2m)


def _root(classes: dict[str, Cls], name: str) -> str:
    while classes[name].base is not None:
        name = classes[name].base  # type: ignore[assignment]
    return name


# ------------------------------------------------------------------ program


@dataclass
class Query:
    var: str
    cls: str
    style: str  # scalars, execute, legacy, get, iterate
    chains: list[list[tuple[str, str]]]  # [(loader, rel name), ...] from cls
    columns: str | None  # load_only/defer option source, without the class
    where: str | None
    order: str
    limit: int | None
    populate: bool
    eager_join: str | None = None  # rel name of a contains_eager join
    yield_per: bool = False


@dataclass
class Step:
    kind: str  # query, rows, op, access
    query: Query | None = None
    code: list[str] = field(default_factory=list)


@dataclass
class Program:
    index: int
    seed: int
    schema: Schema
    rows: dict[str, list[dict[str, object]]]
    links: dict[str, list[tuple[int, int]]]
    steps: list[Step]
    is_async: bool
    expire_on_commit: bool
    root_reads: set[str] = field(default_factory=set)  # rels read on query results
    # read where run_fix's selectinload may not reach (nested, identity map)
    unchecked_reads: set[str] = field(default_factory=set)
    invalidates: bool = False  # commit/expire/refresh/populate_existing used


def _data(
    rng: random.Random, schema: Schema
) -> tuple[dict, dict, dict[str, list[int]]]:
    rows: dict[str, list[dict[str, object]]] = {n: [] for n in schema.classes}
    ids: dict[str, list[int]] = {n: [] for n in schema.classes}
    next_id: dict[str, int] = {}
    for name in schema.classes:
        root = schema.root(name)
        for _ in range(rng.choice([0, 1, 2, 3, 3, 4, 5, 6])):
            next_id[root] = next_id.get(root, 0) + 1
            ids[name].append(next_id[root])
    # an instance of a subclass is an instance of its bases
    members = {
        n: [i for c in schema.classes if n in schema.lineage(c) for i in ids[c]]
        for n in schema.classes
    }
    for name in schema.classes:
        for i in ids[name]:
            row: dict[str, object] = {
                "id": i,
                "name": f"{name.lower()}{i}",
                "n": rng.randint(0, 5),
            }
            for c in schema.lineage(name):
                spec = schema.classes[c]
                for k in range(spec.extra):
                    row[f"{c.lower()}c{k}"] = rng.randint(0, 9)
                for col, target in spec.fks.items():
                    pool = members[target]
                    row[col] = rng.choice(pool) if pool and rng.random() < 0.8 else None
            rows[name].append(row)
    links: dict[str, list[tuple[int, int]]] = {}
    for table, left, right in schema.m2m:
        pairs = {
            (a, b) for a in members[left] for b in members[right] if rng.random() < 0.4
        }
        links[table] = sorted(pairs)
    return rows, links, members


def _chain(rng: random.Random, schema: Schema, cls: str) -> list[tuple[str, str]]:
    chain: list[tuple[str, str]] = []
    current = cls
    back = None
    for _depth in range(rng.choice([1, 1, 2])):
        # an option on the way back (A.b then B.a) conflicts with the loader
        # SQLAlchemy gives the back reference (InvalidRequestError)
        options = [r for r in schema.rels_of(current) if r.name != back]
        if not options:
            break
        rel = rng.choice(options)
        chain.append((rng.choice(LOADERS), rel.name))
        current, back = rel.target, rel.back
    return chain


def _query(
    rng: random.Random, schema: Schema, var: str, cls: str, members: dict
) -> Query:
    style = rng.choice(
        ["scalars", "scalars", "execute", "legacy", "legacy", "get", "iterate"]
    )
    chains: list[list[tuple[str, str]]] = []
    seen: set[tuple[str, ...]] = set()
    for _ in range(rng.choice([0, 0, 1, 1, 2, 3])):
        chain = _chain(rng, schema, cls)
        # one loader per path: a prefix of a path already given keeps its loader
        key = tuple(r for _, r in chain)
        if not chain or any(key[: k + 1] in seen for k in range(len(key))):
            continue
        for k in range(len(key)):
            seen.add(key[: k + 1])
        chains.append(chain)
    if rng.random() < 0.08 and style != "get":
        chains.append([(rng.choice(["raiseload", "lazyload", "selectinload"]), "*")])
    columns = None
    # A subquery loader of a many-to-one needs the foreign key column, which
    # load_only()/defer() may leave out (NoSuchColumnError, in SQLAlchemy).
    subquery = any(loader == "subqueryload" for chain in chains for loader, _ in chain)
    subquery |= any(r.lazy == "subquery" for r in schema.rels_of(cls))
    if not subquery and rng.random() < 0.25:
        columns = rng.choice(
            [
                "load_only({c}.name)",
                "load_only({c}.name, {c}.n)",
                "defer({c}.n)",
                "defer({c}.name)",
            ]
        )
    eager_join = None
    if style in ("scalars", "legacy", "iterate") and rng.random() < 0.15:
        candidates = [
            r
            for r in schema.rels_of(cls)
            if r.kind in ("m2o", "o2m")
            and not schema.same_tree(r.target, cls)
            and (r.name,) not in seen
        ]
        if candidates:
            eager_join = rng.choice(candidates).name
    where = None
    pick = rng.random()
    if pick < 0.25:
        where = f"{{c}}.n >= {rng.randint(0, 4)}"
    elif pick < 0.4 and members[cls]:
        chosen = sorted(rng.sample(members[cls], rng.randint(1, len(members[cls]))))
        where = f"{{c}}.id.in_({chosen!r})"
    limit = rng.choice([None, None, None, 1, 2, 3]) if eager_join is None else None
    order = rng.choice(["{c}.id", "{c}.id", "{c}.id.desc()"])
    # yield_per cannot run joined or subquery eager loads (nor unique())
    yield_per = (
        style == "iterate"
        and eager_join is None
        and not any(r.lazy in ("joined", "subquery") for r in schema.rels)
        and not any(
            loader in ("joinedload", "subqueryload")
            for chain in chains
            for loader, _ in chain
        )
        and rng.random() < 0.5
    )
    return Query(
        var,
        cls,
        style,
        chains,
        columns,
        where,
        order,
        limit,
        rng.random() < 0.15,
        eager_join,
        yield_per,
    )


def _value(expr: str) -> str:
    return f"val(lambda: v({expr}))"


def _access(
    rng: random.Random, program: Program, var: str, cls: str, unchecked: bool
) -> list[str]:
    """Code reading ``var``'s objects; ``unchecked``: run_fix's selectinload
    may not reach them (see ``generate``), so no oracle relies on it."""
    schema = program.schema
    rels = schema.rels_of(cls)
    columns = ["o.id", "o.name", "o.n"]
    for c in schema.lineage(cls):
        columns += [f"o.{c.lower()}c{k}" for k in range(schema.classes[c].extra)][:3]
    pick = rng.random()
    if pick < 0.2 or not rels:
        cols = ", ".join(rng.sample(columns, rng.randint(1, len(columns))))
        return [f"for o in {var}:", f"    out.append(({cols},))"]
    rel = rng.choice(rels)
    (program.unchecked_reads if unchecked else program.root_reads).add(rel.name)
    if pick < 0.45:
        return [
            f"for o in {var}:",
            f"    out.append((o.id, {_value('o.' + rel.name)}))",
        ]
    if pick < 0.6:
        return [f"out.append([{_value('o.' + rel.name)} for o in {var}])"]
    if pick < 0.72:
        stop = rng.randint(1, 3)
        return [
            f"for k, o in enumerate({var}):",
            f"    if k >= {stop}:",
            "        break",
            f"    out.append({_value('o.' + rel.name)})",
        ]
    inner = schema.rels_of(rel.target)
    if not inner:
        return [f"for o in {var}:", f"    out.append({_value('o.' + rel.name)})"]
    second = rng.choice(inner)
    program.unchecked_reads.add(second.name)
    return [
        f"for o in {var}:",
        f"    for c in objs(val(lambda: o.{rel.name})):",
        f"        out.append((o.id, c.id, {_value('c.' + second.name)}))",
    ]


def generate(rng: random.Random, index: int, seed: int) -> Program:
    schema = _schema(rng)
    rows, links, members = _data(rng, schema)
    program = Program(
        index,
        seed,
        schema,
        rows,
        links,
        [],
        rng.random() < 0.25 and HAS_AIOSQLITE,
        rng.random() < 0.7,
    )
    lists: list[tuple[str, str, bool]] = []  # (variable, class, unchecked)
    names = list(schema.classes)
    loaded: set[str] = set()

    def shared(cls: str) -> bool:
        """Whether objects of cls's hierarchy may be in the identity map
        already: session.get() returns them without a query, so the
        selectinload of run_fix does not load their relationships."""
        return schema.root(cls) in loaded or any(
            schema.same_tree(r.target, cls) for r in schema.rels
        )

    for n in range(rng.randint(1, 4)):
        pick = rng.random()
        if pick < 0.12 and lists:
            program.invalidates = True
            var = rng.choice(lists)[0]
            op = rng.choice(
                [
                    ["session.commit()"],
                    ["session.expire_all()"],
                    [f"if {var}:", f"    session.expire({var}[0])"],
                    [f"if {var}:", f"    session.refresh({var}[0])"],
                ]
            )
            program.steps.append(Step("op", code=op))
            continue
        cls = rng.choice(names)
        if pick < 0.25:
            # rows of a Core or text() statement, then per-row work
            table = schema.table_of(cls)
            var = f"rows{n}"
            source = rng.choice(
                [
                    f'session.execute(text("SELECT id FROM {table} ORDER BY id")).all()',
                    f"session.execute(select({cls}.__table__.c.id).order_by({cls}.__table__.c.id)).all()",
                ]
            )
            code = [f"{var} = {source}"]
            rels = schema.rels_of(cls)
            fks = [(c, t) for c, t in schema.classes[cls].fks.items()]
            choice = rng.random()
            if choice < 0.4 and rels:
                rel = rng.choice(rels)
                reads = program.unchecked_reads if shared(cls) else program.root_reads
                reads.add(rel.name)
                code += [
                    f"for row in {var}:",
                    f"    o = session.get({cls}, row.id{{get:{cls}.{rel.name}}})",
                    "    if o is not None:",
                    # the identity map holds objects weakly: keep them, or
                    # when the garbage collector runs decides what is loaded
                    "        keep.append(o)",
                    f"        out.append((row.id, {_value('o.' + rel.name)}))",
                ]
            elif choice < 0.7 and fks:
                col, target = rng.choice(fks)
                holder = cls
                code += [
                    f"for row in {var}:",
                    f"    hit = session.execute(text(\"SELECT {col} FROM {schema.table_of(holder)} WHERE id = :i\"), {{'i': row.id}}).scalar()",
                    f"    out.append((row.id, hit, session.execute(select({target}.__table__.c.id).where({target}.__table__.c.id == hit)).scalars().all() if hit is not None else None))",
                ]
            else:
                code += [f"out.append([tuple(r) for r in {var}])"]
            program.steps.append(Step("rows", code=code))
            loaded.add(schema.root(cls))
            continue
        var = f"objs{n}"
        query = _query(rng, schema, var, cls, members)
        if query.populate:
            program.invalidates = True
        program.steps.append(Step("query", query=query))
        lists.append((var, cls, query.style == "get" and shared(cls)))
        loaded.add(schema.root(cls))
        for _ in range(rng.choice([1, 1, 2])):
            target = rng.choice(lists) if rng.random() < 0.3 else lists[-1]
            program.steps.append(Step("access", code=_access(rng, program, *target)))
    return program


# ----------------------------------------------------------------- rendering


def _render_models(program: Program) -> list[str]:
    schema = program.schema
    lines = [
        "class Base(DeclarativeBase):",
        "    pass",
        "",
    ]
    for table, left, right in schema.m2m:
        lines += [
            f"{table} = Table(",
            f'    "{table}", Base.metadata,',
            f'    Column("left_id", ForeignKey("{schema.table_of(left)}.id"), primary_key=True),',
            f'    Column("right_id", ForeignKey("{schema.table_of(right)}.id"), primary_key=True),',
            ")",
            "",
        ]
    for name, cls in schema.classes.items():
        parent = cls.base or "Base"
        lines.append(f"class {name}({parent}):")
        if cls.table is not None:
            lines.append(f'    __tablename__ = "{cls.table}"')
        if cls.base is None:
            lines += [
                "    id = Column(Integer, primary_key=True, autoincrement=False)",
                "    name = Column(String(20))",
                "    n = Column(Integer)",
            ]
        elif cls.table is not None:
            lines.append(
                f'    id = Column(ForeignKey("{schema.table_of(cls.base)}.id"), primary_key=True)'
            )
        for k in range(cls.extra):
            lines.append(f"    {name.lower()}c{k} = Column(Integer)")
        for col, target in cls.fks.items():
            lines.append(
                f'    {col} = Column(ForeignKey("{schema.table_of(target)}.id"), nullable=True)'
            )
        if cls.polymorphic:
            lines += [
                "    kind = Column(String(10))",
                f'    __mapper_args__ = {{"polymorphic_on": "kind", "polymorphic_identity": "{name}"}}',
            ]
        elif cls.base is not None:
            lines.append(f'    __mapper_args__ = {{"polymorphic_identity": "{name}"}}')
        for rel in schema.rels:
            if rel.owner != name:
                continue
            args = [f'"{rel.target}"', f'lazy="{rel.lazy}"']
            if rel.fk is not None:
                args.append(f'foreign_keys="[{rel.fk[0]}.{rel.fk[1]}]"')
            if rel.self_ref and rel.kind == "m2o":
                args.append(f'remote_side="[{name}.id]"')
            if rel.secondary is not None:
                args.append(f"secondary={rel.secondary}")
            if rel.back is not None:
                args.append(f'back_populates="{rel.back}"')
            if rel.kind in ("o2m", "m2m"):
                args.append(f'order_by="{rel.target}.id"')
            lines.append(f"    {rel.name} = relationship({', '.join(args)})")
        lines.append("")
    return lines


def _option(schema: Schema, cls: str, chain: list[tuple[str, str]]) -> str:
    parts = []
    current = cls
    for loader, rel in chain:
        if rel == "*":
            parts.append(f'{loader}("*")')
            continue
        parts.append(f"{loader}({current}.{rel})")
        current = schema.rel(rel).target
    return ".".join(parts)


def _render_query(schema: Schema, query: Query, fix: set[str]) -> list[str]:
    cls = query.cls
    chains = [list(chain) for chain in query.chains]
    eager_join = query.eager_join
    if fix:
        available = {r.name for r in schema.rels_of(cls)}
        for rel in sorted(fix & available):
            for chain in chains:
                if chain[0][1] == rel:
                    chain[0] = ("selectinload", rel)
                    break
            else:
                chains.append([("selectinload", rel)])
            if eager_join == rel:
                eager_join = None
    options = [_option(schema, cls, chain) for chain in chains]
    if query.columns is not None:
        options.append(query.columns.format(c=cls))
    if eager_join is not None:
        options.append(f"contains_eager({cls}.{eager_join})")
    where = query.where.format(c=cls) if query.where else None
    order = query.order.format(c=cls)
    var = query.var
    if query.style == "get":
        keys = f"sorted({{r.id for r in session.execute(select({cls}.__table__.c.id)).all()}})"
        extra = f", options=[{', '.join(options)}]" if options else ""
        if query.populate:
            extra += ", populate_existing=True"
        return [
            f"{var} = []",
            f"for i in {keys}:",
            f"    o = session.get({cls}, i{extra})",
            "    if o is not None:",
            f"        {var}.append(o)",
        ]
    if query.style == "legacy":
        text = f"session.query({cls})"
        if eager_join is not None:
            text += f".outerjoin({cls}.{eager_join})"
        if options:
            text += f".options({', '.join(options)})"
        if where:
            text += f".filter({where})"
        text += f".order_by({order})"
        if query.limit is not None:
            text += f".limit({query.limit})"
        if query.populate:
            text += ".populate_existing()"
        return [f"{var} = {text}.all()"]
    stmt = f"select({cls})"
    if eager_join is not None:
        stmt += f".outerjoin({cls}.{eager_join})"
    if options:
        stmt += f".options({', '.join(options)})"
    if where:
        stmt += f".where({where})"
    stmt += f".order_by({order})"
    if query.limit is not None:
        stmt += f".limit({query.limit})"
    if query.populate:
        stmt += ".execution_options(populate_existing=True)"
    if query.style == "execute":
        return [f"{var} = session.execute({stmt}).unique().scalars().all()"]
    if query.style == "iterate":
        rows = f"session.scalars({stmt}).unique()"
        if query.yield_per:
            # the rows come in batches of two, from an open cursor
            rows = f"session.scalars({stmt}.execution_options(yield_per=2))"
        return [
            f"{var} = []",
            f"for o in {rows}:",
            f"    {var}.append(o)",
        ]
    return [f"{var} = session.scalars({stmt}).unique().all()"]


def _render_body(program: Program, fix: set[str]) -> list[str]:
    lines = ["out = []", "keep = []"]
    for step in program.steps:
        if step.kind == "query":
            assert step.query is not None
            lines += _render_query(program.schema, step.query, fix)
        else:
            # a session.get() of each row: run_fix loads the relationship
            # it reads with selectinload too
            for line in step.code:
                found = re.search(r"\{get:(\w+)\.(\w+)\}", line)
                if found is not None:
                    cls, rel = found.groups()
                    extra = (
                        f", options=[selectinload({cls}.{rel})]" if rel in fix else ""
                    )
                    line = line.replace(found.group(0), extra)
                lines.append(line)
    lines.append("return out")
    return lines


PRELUDE = """\
import asyncio
import pathlib

from sqlalchemy import Column, ForeignKey, Integer, String, Table, create_engine, select, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    contains_eager,
    defaultload,
    defer,
    immediateload,
    joinedload,
    lazyload,
    load_only,
    noload,
    raiseload,
    relationship,
    selectinload,
    subqueryload,
)

RAISE = "RAISE"


def val(read):
    try:
        return read()
    except InvalidRequestError:
        return RAISE


def v(value):
    if value is None or value == RAISE:
        return value
    if isinstance(value, list):
        return sorted(x.id for x in value)
    return value.id


def objs(value):
    if value is None or value == RAISE:
        return []
    if isinstance(value, list):
        return sorted(value, key=lambda x: x.id)
    return [value]

"""


def render_module(program: Program) -> str:
    schema = program.schema
    lines = [PRELUDE]
    lines += _render_models(program)
    lines += [
        "",
        "",
        "def seed(path):",
        "    engine = create_engine(f'sqlite:///{path}')",
    ]
    lines += [
        "    Base.metadata.create_all(engine)",
        "    with Session(engine) as session:",
    ]
    for name, rows in program.rows.items():
        for row in rows:
            args = ", ".join(f"{k}={v!r}" for k, v in row.items())
            lines.append(f"        session.add({name}({args}))")
    lines.append("        session.flush()")
    for table, pairs in program.links.items():
        if pairs:
            values = [{"left_id": a, "right_id": b} for a, b in pairs]
            lines.append(f"        session.execute({table}.insert(), {values!r})")
    lines += ["        session.commit()", "    engine.dispose()", "    return path", ""]
    for fname, fix in (("run", set()), ("run_fix", fix_set(program))):
        lines += ["", f"def {fname}(session):"]
        lines += ["    " + line for line in _render_body(program, fix)]
    eoc = program.expire_on_commit
    if program.is_async:
        lines += textwrap.dedent(
            f"""

            def main(path, which):
                from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

                async def go():
                    engine = create_async_engine(f"sqlite+aiosqlite:///{{path}}")
                    try:
                        async with AsyncSession(engine, expire_on_commit={eoc}) as session:
                            return await session.run_sync(globals()[which])
                    finally:
                        await engine.dispose()

                return asyncio.run(go())
            """
        ).splitlines()
    else:
        lines += textwrap.dedent(
            f"""

            def main(path, which):
                engine = create_engine(f"sqlite:///{{path}}")
                try:
                    with Session(engine, expire_on_commit={eoc}) as session:
                        return globals()[which](session)
                finally:
                    engine.dispose()
            """
        ).splitlines()
    del schema
    return "\n".join(lines) + "\n"


def fix_set(program: Program) -> set[str]:
    """The relationships read on query results, each loaded with selectinload in run_fix."""
    return set(program.root_reads)


TEST_MODULE_HEAD = """\
import os

import pytest

from pytest_queryguard import ignore


def _save(name, out):
    with open(os.path.join(os.environ["QG_FUZZ_OUT"], name + ".txt"), "w") as f:
        f.write(repr(out))

"""


def render_tests(programs: list[Program]) -> str:
    lines = [TEST_MODULE_HEAD]
    for p in programs:
        i = p.index
        lines += [
            f"import prog_{i}",
            "",
            "@pytest.fixture",
            f"def db_{i}(tmp_path):",
            f"    return prog_{i}.seed(tmp_path / 'db.sqlite')",
            "",
            f"def test_p{i}(db_{i}):",
            f"    _save('p{i}', prog_{i}.main(db_{i}, 'run'))",
            "",
            f"def test_p{i}_ignored(db_{i}):",
            "    with ignore('*'):",
            f"        out = prog_{i}.main(db_{i}, 'run')",
            f"    _save('p{i}_ignored', out)",
            "",
            f"def test_p{i}_fix(db_{i}):",
            f"    _save('p{i}_fix', prog_{i}.main(db_{i}, 'run_fix'))",
            "",
        ]
    return "\n".join(lines) + "\n"


CONFTEST = """\
import hashlib
import json
import os

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine


def _log(record):
    with open(os.environ["QG_FUZZ_EVENTS"], "a") as f:
        f.write(json.dumps(record) + "\\n")


# The SQL each test body sent: SQLAlchemy itself may load in another order
# from one process to the next (it iterates over sets of objects), and such
# a program is not compared between runs.
_sql = []


@event.listens_for(Engine, "before_cursor_execute")
def _note_sql(conn, cursor, statement, parameters, context, executemany):
    _sql.append(f"{statement} {parameters!r}")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    _sql.clear()


def pytest_runtest_logreport(report):
    if report.when == "call":
        digests = [hashlib.sha1(x.encode()).hexdigest()[:10] for x in _sql]
        _log({"sql": digests, "nodeid": report.nodeid})
    if report.when == "call" or report.outcome != "passed":
        _log(
            {
                "nodeid": report.nodeid,
                "when": report.when,
                "outcome": report.outcome,
                "text": report.longreprtext if report.outcome != "passed" else "",
            }
        )


# Lazy loads of parents whose query is unknown (an object that two queries or
# load paths read): the report counts them with every query line, strict
# mode never (documented). Noted per test, for the strict oracle.
_current = None
_unknown = set()


def pytest_runtest_setup(item):
    global _current
    _current = item.nodeid


try:
    from pytest_queryguard import collector as _collector
except ImportError:
    _collector = None
if _collector is not None:
    _add = _collector.QueryCollector.add_relationship_load

    def _add_relationship_load(self, event):
        parent = event.parent
        key = (_current, event.relationship)
        # eager loader queries (no parent object) are no lazy loads
        lazy = event.parent_key is not None
        if lazy and (parent is None or parent.callsite is None) and key not in _unknown:
            _unknown.add(key)
            _log({"unknown_parent": event.relationship, "nodeid": _current})
        return _add(self, event)

    _collector.QueryCollector.add_relationship_load = _add_relationship_load


def pytest_warning_recorded(warning_message, when, nodeid, location):
    _log(
        {
            "warning": warning_message.category.__name__,
            "message": str(warning_message.message),
            "nodeid": nodeid,
        }
    )
"""


@dataclass
class Settings:
    ini: dict[str, str]


def _settings(rng: random.Random) -> Settings:
    ini = {
        "queryguard_mode": "fail",
        "queryguard_relationship_threshold": str(rng.choice([2, 2, 2, 3, 4])),
        "queryguard_repeat_threshold": str(rng.choice([5, 5, 3, 8])),
        "queryguard_column_load_threshold": str(rng.choice([5, 5, 2, 3])),
        "queryguard_scope": rng.choice(["test", "test", "session"]),
        "queryguard_unused_eager_load": rng.choice(["off", "warn", "warn"]),
        "queryguard_unused_columns": rng.choice(["off", "warn"]),
        "queryguard_cartesian_load": rng.choice(["off", "warn", "fail"]),
        "queryguard_stack_depth": str(rng.choice([0, 3, 3, 8])),
    }
    small = rng.choice([0, 0, 2, 3])
    if small and small < int(ini["queryguard_repeat_threshold"]):
        ini["queryguard_small_repeat_threshold"] = str(small)
    if rng.random() < 0.15:
        ini["queryguard_max_events"] = str(rng.choice([3, 10]))
    return Settings(ini)


# ------------------------------------------------------------------- running


@dataclass
class Run:
    events: list[dict]
    outputs: dict[str, str]
    json: dict[str, dict]
    stdout: str


def _run(pytester: pytest.Pytester, root: Path, label: str, *args: str) -> Run:
    out = root / f"out_{label}"
    out.mkdir()
    events = root / f"events_{label}.jsonl"
    report = root / f"report_{label}.json"
    os.environ["QG_FUZZ_OUT"] = str(out)
    os.environ["QG_FUZZ_EVENTS"] = str(events)
    try:
        result = pytester.runpytest_subprocess(
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "-W",
            "default",
            "-q",
            *args,
            *([] if "no:queryguard" in args else [f"--queryguard-json={report}"]),
            timeout=300,
        )
    finally:
        del os.environ["QG_FUZZ_OUT"]
        del os.environ["QG_FUZZ_EVENTS"]
    lines = events.read_text().splitlines() if events.exists() else []
    outputs = {p.stem: p.read_text() for p in out.iterdir()}
    data = json.loads(report.read_text())["tests"] if report.exists() else {}
    return Run([json.loads(x) for x in lines], outputs, data, result.stdout.str())


def _test_name(nodeid: str) -> str:
    return nodeid.rsplit("::", 1)[-1].removeprefix("test_")


def _findings(run: Run) -> dict[str, list[dict]]:
    return {_test_name(k): v.get("findings", []) for k, v in run.json.items()}


def _identity(findings: list[dict]) -> list[tuple]:
    return sorted(
        (
            f["kind"],
            f["label"],
            f.get("count"),
            f.get("file"),
            f.get("function"),
            f.get("line"),
            json.dumps(f.get("stack"), sort_keys=True),
        )
        for f in findings
    )


def _rel_labels(findings: list[dict]) -> set[str]:
    return {f["label"] for f in findings if f["kind"] == "relationship-load"}


def _outcomes(run: Run) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in run.events:
        if "nodeid" in e and "outcome" in e:
            name = _test_name(e["nodeid"])
            if e["outcome"] != "passed" or name not in out:
                out[name] = e
    return out


# SQLAlchemy checks an object's load context after the ``refresh`` event only
# while some listener for it exists, which the plugin always has: with
# populate_existing and a subquery loader of a self-referential relationship
# the context changes inside SQLAlchemy, not in the handler, and SQLAlchemy
# warns (docs/limitations.md).
DOCUMENTED_WARNINGS = ("Loading context for",)


def _warnings(run: Run) -> set[tuple[str, str, str]]:
    return {
        (e["nodeid"], e["warning"], re.sub(r"0x[0-9a-f]+", "0x", e["message"]))
        for e in run.events
        if "warning" in e and not e["message"].startswith(DOCUMENTED_WARNINGS)
    }


def _check_clean(run: Run, plain: Run, label: str, problems: list[str]) -> None:
    for e in run.events:
        if "warning" in e and (
            "Internal" in e["warning"]
            or "queryguard" in e["message"].lower()
            and "QueryGuardWarning" != e["warning"]
        ):
            problems.append(
                f"[{label}] warning {e['warning']}: {e['message']} ({e['nodeid']})"
            )
    # the plugin adds no warning of SQLAlchemy or Python to the program
    for nodeid, category, message in sorted(_warnings(run) - _warnings(plain)):
        if not category.startswith("QueryGuard"):
            problems.append(f"[{label}] new warning {category}: {message} ({nodeid})")
    for marker in ("INTERNALERROR", "queryguard: measurement stopped"):
        if marker in run.stdout:
            problems.append(f"[{label}] {marker} in the output")


def check_batch(
    pytester: pytest.Pytester, programs: list[Program], settings: Settings
) -> list[str]:
    root = pytester.path
    for p in programs:
        (root / f"prog_{p.index}.py").write_text(render_module(p))
    (root / "test_fuzz_batch.py").write_text(render_tests(programs))
    (root / "conftest.py").write_text(CONFTEST)
    ini = "\n".join(f"{k} = {v}" for k, v in settings.ini.items())
    (root / "pytest.ini").write_text(f"[pytest]\n{ini}\n")
    problems: list[str] = []

    plain = _run(pytester, root, "plain", "-p", "no:queryguard")
    for name, e in _outcomes(plain).items():
        if e["outcome"] != "passed":
            # the generator wrote an invalid program: not the plugin's fault
            problems.append(
                f"[generator] {name} fails without the plugin:\n{e['text'][-3000:]}"
            )
    if problems:
        return problems

    first = _run(pytester, root, "report")
    second = _run(pytester, root, "again")
    strict = _run(pytester, root, "strict", "--queryguard-strict")
    for label, run in (("report", first), ("again", second), ("strict", strict)):
        _check_clean(run, plain, label, problems)

    # (1) a failure is a finding, never the plugin's own error
    for label, run in (("report", first), ("strict", strict)):
        for name, e in _outcomes(run).items():
            if e["outcome"] == "passed":
                continue
            text = e["text"]
            # the report fails the test with its text alone (no traceback);
            # strict mode raises NPlusOneDetected at the access
            if text.lstrip().startswith("queryguard") or (
                label == "strict"
                and "pytest_queryguard.NPlusOneDetected: queryguard strict:" in text
            ):
                continue
            problems.append(
                f"[{label}] {name} failed without a finding:\n{text[-3000:]}"
            )

    # (2) results are those of the run without the plugin
    for label, run in (("report", first), ("again", second), ("strict", strict)):
        for name, value in run.outputs.items():
            if plain.outputs.get(name) != value:
                problems.append(
                    f"[{label}] {name} read other values with the plugin:\n"
                    f"  without: {plain.outputs.get(name)}\n  with:    {value}"
                )
    for name in plain.outputs:
        if name not in first.outputs:
            problems.append(f"[report] {name} wrote no results")

    found = _findings(first)
    # Beyond queryguard_max_events the loads are only counted: the report's
    # findings are lower bounds, and ignore() blocks leave nothing out
    # (docs/ignoring.md); strict mode still counts every load.
    truncated = {_test_name(k) for k, v in first.json.items() if v.get("truncated")}
    for p in programs:
        i = p.index
        if found.get(f"p{i}") and f"p{i}_ignored" not in truncated:
            STATS["ignore checked"] += 1
        if found.get(f"p{i}_ignored") and f"p{i}_ignored" not in truncated:
            problems.append(
                f"[ignore] p{i}_ignored has findings: {_identity(found[f'p{i}_ignored'])}"
            )
        if not p.invalidates:
            fixed = fix_set(p)
            checked = {
                label
                for label in _rel_labels(found.get(f"p{i}", []))
                if label.split(".")[-1] in fixed
                and label.split(".")[-1] not in p.unchecked_reads
            }
            STATS["selectinload checked"] += len(checked)
            still = checked & _rel_labels(found.get(f"p{i}_fix", []))
            if still:
                problems.append(
                    f"[selectinload] p{i}_fix still reports {sorted(still)}"
                )

    # (3) strict raises iff the report has a relationship-load. Documented
    # exception: parents whose query is unknown (an object two queries or
    # load paths read) count with every query line in the report but never
    # in strict mode (docs/when-a-test-fails.md#strict-mode-fail-at-the-access).
    strict_outcomes = _outcomes(strict)
    unknown = {
        (_test_name(e["nodeid"]), e["unknown_parent"])
        for e in first.events
        if "unknown_parent" in e
    }

    # (only programs that sent the same SQL in each run: see _sql in CONFTEST)
    def sql(run: Run) -> dict[str, list[str]]:
        return {_test_name(e["nodeid"]): e["sql"] for e in run.events if "sql" in e}

    ran, again_ran, strict_ran = sql(first), sql(second), sql(strict)
    stable = {n for n, d in ran.items() if again_ran.get(n) == d}
    STATS["load order varies"] += len(found) - len(stable & set(found))
    for name, findings in found.items():
        # strict mode stops at the access: its SQL is the report's up to there
        done = strict_ran.get(name, [])
        if name not in stable or ran[name][: len(done)] != done:
            continue
        rel = [f for f in findings if f["kind"] == "relationship-load"]
        known = any((name, f["label"]) not in unknown for f in rel)
        e = strict_outcomes.get(name, {})
        raised = e.get("outcome") == "failed" and "queryguard strict:" in e.get(
            "text", ""
        )
        STATS["strict raised"] += raised
        STATS["strict exempt (unknown parents)"] += bool(rel) and not known
        if name in truncated:
            continue
        if raised and not rel or known and not raised:
            problems.append(
                f"[strict] {name}: report has relationship-load {bool(rel)}, strict raised {raised}"
                f"\n  report: {_identity(findings)}\n  strict: {e.get('text', '')[-1500:]}"
            )

    # (4) deterministic
    again = _findings(second)
    for name, findings in found.items():
        if name not in stable:
            continue
        if _identity(findings) != _identity(again.get(name, [])):
            problems.append(
                f"[determinism] {name}:\n  {_identity(findings)}\n  {_identity(again.get(name, []))}"
            )
        if first.json[next(k for k in first.json if _test_name(k) == name)][
            "statements"
        ] != next(
            v["statements"] for k, v in second.json.items() if _test_name(k) == name
        ):
            problems.append(f"[determinism] {name}: statement counts differ")
    return problems


STATS: Counter[str] = Counter()


@pytest.mark.parametrize("batch", range(BATCHES))
def test_generated_programs(pytester: pytest.Pytester, batch: int) -> None:
    STATS["batches"] += 1
    seed = SEED + 1000 * batch
    rng = random.Random(seed)
    settings = _settings(rng)
    programs = [generate(rng, i, seed) for i in range(PROGRAMS)]
    problems = check_batch(pytester, programs, settings)
    keep = os.environ.get("QUERYGUARD_FUZZ_KEEP")
    if keep and problems:
        target = Path(keep) / f"batch_{seed}"
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(pytester.path, target)
    assert not problems, f"seed {seed}, settings {settings.ini}:\n" + "\n\n".join(
        problems
    )


def test_the_oracles_were_checked() -> None:
    """The batches above (run first, in file order) reached every oracle."""
    if STATS["batches"] < BATCHES:
        # run alone, shuffled or split between workers
        pytest.skip("not every batch ran before this test in this process")
    for name in ("ignore checked", "selectinload checked", "strict raised"):
        assert STATS[name] > 0, STATS
