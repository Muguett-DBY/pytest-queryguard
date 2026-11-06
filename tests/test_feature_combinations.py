"""Features combined: the terminal, the JSON, the SARIF file and the patch agree.

One realistic project (an N+1 in a helper several tests call, a repeated
statement, a cartesian joinedload, an unused eager load, a wide row read in
part, budgets from a marker and from the budget file, an ``ignore()`` block,
the ``queryguard`` fixture, ``allow_nplusone``, a ``stack=`` ignore pattern
and a baselined finding) runs under a pairwise set of option combinations:
pytest-xdist on or off, strict mode, ``warn`` or ``fail``, the experimental
checks, a baseline, the patch file, ``queryguard_fail_on`` and a default
budget. Each run is checked against what the documentation says, and every
output against the others:

* the findings (kind, label, test) of the terminal report are those of the
  JSON file without the baselined ones, and the SARIF results are the same
  findings merged by identity, with the documented level, test count and sum;
* the outcome of each test and the exit status follow the mode, the options
  of each kind and ``queryguard_fail_on``; budgets always fail;
* every hunk of the patch file is a reported relationship-load finding, the
  file applies with ``git apply --check``, and the SARIF ``fixes`` make the
  same change;
* a pytest-xdist run gives the same JSON, SARIF, patch and outcomes as the
  serial run, and the stale-ignore and stale-baseline sections agree with
  the documented xdist differences;
* nothing reports an internal error.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pytest

from pytest_queryguard import compare, mcp, trend

# pytester runs sessions in a subprocess here, but keep the outer plugin quiet
pytestmark = pytest.mark.allow_nplusone

_GIT = shutil.which("git")
_XDIST = True
try:
    import xdist  # noqa: F401
except ImportError:  # pragma: no cover - xdist is a dev dependency
    _XDIST = False

MODELS = """\
from __future__ import annotations

from sqlalchemy import ForeignKey, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    orders: Mapped[list[Order]] = relationship(back_populates="customer")
    notes: Mapped[list[Note]] = relationship()


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list[Item]] = relationship()


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str] = mapped_column(default="e")
    phone: Mapped[str] = mapped_column(default="p")
    street: Mapped[str] = mapped_column(default="s")
    city: Mapped[str] = mapped_column(default="c")
    country: Mapped[str] = mapped_column(default="co")
    notes: Mapped[str] = mapped_column(default="n")
    bio: Mapped[str] = mapped_column(default="b")


def make_session() -> Session:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    for c in range(1, 4):
        customer = Customer(id=c, name=f"customer {c}")
        customer.orders = [
            Order(id=c * 10 + o, items=[Item(id=c * 100 + o * 10 + i) for i in range(2)])
            for o in range(2)
        ]
        customer.notes = [Note(id=c * 10 + n) for n in range(2)]
        session.add(customer)
    session.add_all(Account(id=a, name=f"account {a}") for a in range(1, 4))
    session.commit()
    session.expunge_all()
    return session
"""

QUERIES = """\
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import Account, Customer, Order


def customer_names(session: Session) -> list[str]:
    orders = session.scalars(select(Order).order_by(Order.id)).all()
    return [order.customer.name for order in orders]


def item_counts(session: Session) -> list[int]:
    orders = session.scalars(select(Order).order_by(Order.id)).all()
    return [len(order.items) for order in orders]


def order_counts(session: Session) -> list[int]:
    customers = session.scalars(select(Customer).order_by(Customer.id)).all()
    return [len(customer.orders) for customer in customers]


def order_ids(session: Session, ids: list[int]) -> list[int | None]:
    return [
        session.execute(select(Order.id).where(Order.id == i)).scalar() for i in ids
    ]


def customers_with_everything(session: Session) -> list[Customer]:
    query = select(Customer).options(
        joinedload(Customer.orders), joinedload(Customer.notes)
    )
    return list(session.scalars(query).unique().all())


def customers_with_orders(session: Session) -> list[Customer]:
    query = select(Customer).options(selectinload(Customer.orders))
    return list(session.scalars(query).all())


def account_names(session: Session) -> list[str]:
    return [account.name for account in session.scalars(select(Account)).all()]
"""

TESTS = """\
import pytest
from sqlalchemy import select

from pytest_queryguard import ignore
from app import queries
from app.models import Customer, make_session


def test_nplusone():
    assert len(queries.customer_names(make_session())) == 6


def test_repeated():
    assert len(queries.order_ids(make_session(), list(range(10, 17)))) == 7


def test_cartesian():
    assert len(queries.customers_with_everything(make_session())) == 3


def test_unused_eager_load():
    customers = queries.customers_with_orders(make_session())
    assert [c.name for c in customers]


def test_wide_rows():
    assert len(queries.account_names(make_session())) == 3


@pytest.mark.max_queries(2)
def test_budget():
    session = make_session()
    for _ in range(3):
        session.scalars(select(Customer)).all()


def test_budget_file():
    make_session().scalars(select(Customer)).all()


def test_ignore_block():
    session = make_session()
    with ignore("relationship-load"):
        queries.customer_names(session)
    with ignore("repeated-statement"):  # stale: nothing repeats here
        session.scalars(select(Customer)).all()


def test_fixture(queryguard):
    session = make_session()
    with queryguard.num_queries(1):
        session.scalars(select(Customer)).all()
    with queryguard.max_queries(1):
        session.scalars(select(Customer)).all()


@pytest.mark.allow_nplusone("Order.customer")
def test_allowed():
    queries.customer_names(make_session())


@pytest.mark.allow_nplusone("Nothing.here", kind="relationship-load")
def test_stale_marker():
    make_session().scalars(select(Customer)).all()


def test_stack_ignored():
    assert queries.order_counts(make_session()) == [2, 2, 2]


def test_baselined():
    assert queries.item_counts(make_session()) == [2] * 6


@pytest.mark.parametrize("n", range(3))
def test_param(n):
    assert len(queries.customer_names(make_session())) == 6
"""

BASELINE = {
    "version": 1,
    "findings": [
        {
            "kind": "relationship-load",
            "label": "Order.items",
            "file": "app/queries.py",
            "function": "item_counts",
        },
        {
            "kind": "relationship-load",
            "label": "Gone.away",
            "file": "app/queries.py",
            "function": "gone",
        },
    ],
}
BUDGETS = {"format": 1, "budgets": {"tests/test_app.py::test_budget_file": 1}}

T = "tests/test_app.py::"
REL = "relationship-load"
REPEAT = "repeated-statement"
CARTESIAN = "cartesian-load"
EAGER = "unused-eager-load"
COLUMNS = "unused-columns"
NODEIDS = [
    T + name
    for name in (
        "test_nplusone",
        "test_repeated",
        "test_cartesian",
        "test_unused_eager_load",
        "test_wide_rows",
        "test_budget",
        "test_budget_file",
        "test_ignore_block",
        "test_fixture",
        "test_allowed",
        "test_stale_marker",
        "test_stack_ignored",
        "test_baselined",
        "test_param[0]",
        "test_param[1]",
        "test_param[2]",
    )
]
BUDGET_TESTS = {T + "test_budget", T + "test_budget_file"}
CUSTOMER = (REL, "Order.customer")
ITEMS = (REL, "Order.items")
STACK_PATTERN = (
    "    relationship-load Customer.orders stack=tests/test_app.py:test_stack_ignored"
)
STALE_PATTERN = "repeated-statement *nothing_like_this*"
STALE_MARKER = f"{T}test_stale_marker: relationship-load Nothing.here"
STALE_BLOCK = "tests/test_app.py:{}: ignore('repeated-statement')".format(
    next(
        number
        for number, line in enumerate(TESTS.splitlines(), start=1)
        if 'ignore("repeated-statement")' in line
    )
)
STALE_ENTRY = "relationship-load Gone.away (app/queries.py, gone)"

Key = tuple[str, str]  # (kind, label)


@dataclass(frozen=True)
class Combo:
    """One combination of options; ``xdist`` adds a ``-n 2`` twin of the run."""

    name: str
    xdist: bool = False
    strict: bool = False
    mode: str = "fail"
    cartesian: str = "warn"
    unused_eager: str = "off"
    unused_columns: str = "off"
    baseline: bool = False
    patch: bool = False
    fail_on: tuple[str, ...] = ()
    default_budget: int | None = None
    show_patch: bool = False

    def own_option(self, kind: str) -> str | None:
        return {
            CARTESIAN: self.cartesian,
            EAGER: self.unused_eager,
            COLUMNS: self.unused_columns,
        }.get(kind)

    def fails(self, kind: str) -> bool:
        """docs/configuration.md, queryguard_fail_on."""
        if self.mode != "fail" or self.own_option(kind) not in (None, "fail"):
            return False
        return not self.fail_on or any(fnmatchcase(kind, p) for p in self.fail_on)

    def found(self) -> dict[str, set[Key]]:
        """Findings of each test after ignores, before the baseline (the JSON)."""
        found: dict[str, set[Key]] = {nodeid: set() for nodeid in NODEIDS}
        found[T + "test_nplusone"].add(CUSTOMER)
        found[T + "test_repeated"].add(
            (REPEAT, "SELECT orders.id FROM orders WHERE orders.id = ?")
        )
        if self.cartesian != "off":
            found[T + "test_cartesian"].add(
                (CARTESIAN, "Customer.notes + Customer.orders")
            )
        if self.unused_eager != "off":
            found[T + "test_unused_eager_load"].add((EAGER, "Customer.orders"))
        if self.unused_columns != "off":
            found[T + "test_wide_rows"].add((COLUMNS, "Account"))
        found[T + "test_baselined"].add(ITEMS)
        for n in range(3):
            found[f"{T}test_param[{n}]"].add(CUSTOMER)
        return found

    def baselined(self) -> set[Key]:
        return {ITEMS} if self.baseline else set()

    def reported(self) -> dict[str, set[Key]]:
        return {
            nodeid: keys - self.baselined() for nodeid, keys in self.found().items()
        }

    def outcomes(self) -> dict[str, str]:
        outcomes = {}
        for nodeid, keys in self.reported().items():
            failed = nodeid in BUDGET_TESTS or any(self.fails(k) for k, _ in keys)
            outcomes[nodeid] = "failed" if failed else "passed"
        return outcomes


# A pairwise set (an L8 orthogonal array over xdist, strict, mode, the
# experimental checks, the baseline and the patch file); fail_on, the
# cartesian option and the default budget ride along on some rows.
COMBOS = [
    Combo("defaults"),
    Combo(
        "experimental-baseline-patch",
        unused_eager="fail",
        unused_columns="warn",
        cartesian="fail",
        baseline=True,
        patch=True,
        default_budget=500,
    ),
    Combo("strict-warn-patch", strict=True, mode="warn", patch=True, show_patch=True),
    Combo(
        "strict-warn-experimental-baseline",
        strict=True,
        mode="warn",
        unused_eager="warn",
        unused_columns="fail",
        cartesian="off",
        baseline=True,
    ),
    Combo("xdist-warn-baseline", xdist=True, mode="warn", baseline=True),
    Combo(
        "xdist-warn-experimental-patch",
        xdist=True,
        mode="warn",
        unused_eager="warn",
        unused_columns="warn",
        patch=True,
    ),
    Combo(
        "xdist-strict-baseline-patch-fail-on",
        xdist=True,
        strict=True,
        baseline=True,
        patch=True,
        fail_on=("relationship-load", "unused-*"),
        default_budget=500,
        show_patch=True,
    ),
    Combo(
        "xdist-strict-experimental",
        xdist=True,
        strict=True,
        unused_eager="fail",
        unused_columns="fail",
        cartesian="fail",
        # relationship-load only warns: strict mode must not raise
        fail_on=("cartesian-*", "unused-*", "repeated-statement"),
    ),
]


def _ini(combo: Combo) -> str:
    lines = [
        "[pytest]",
        "testpaths = tests",
        "queryguard_ignore =",
        STACK_PATTERN,
        f"    {STALE_PATTERN}",
        "queryguard_budgets = budgets.json",
        f"queryguard_mode = {combo.mode}",
        f"queryguard_strict = {str(combo.strict).lower()}",
        f"queryguard_cartesian_load = {combo.cartesian}",
        f"queryguard_unused_eager_load = {combo.unused_eager}",
        f"queryguard_unused_columns = {combo.unused_columns}",
    ]
    if combo.baseline:
        lines.append("queryguard_baseline = baseline.json")
    if combo.fail_on:
        lines.append("queryguard_fail_on =")
        lines.extend(f"    {pattern}" for pattern in combo.fail_on)
    if combo.default_budget is not None:
        lines.append(f"queryguard_max_queries = {combo.default_budget}")
    return "\n".join(lines) + "\n"


def _write_project(pytester: pytest.Pytester, combo: Combo) -> None:
    root = pytester.path
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "models.py").write_text(MODELS)
    (root / "app" / "queries.py").write_text(QUERIES)
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(TESTS)
    (root / "pytest.ini").write_text(_ini(combo))
    (root / "budgets.json").write_text(json.dumps(BUDGETS))
    if combo.baseline:
        (root / "baseline.json").write_text(json.dumps(BASELINE))


# --- one run and what it wrote ---


@dataclass
class Run:
    ret: int
    stdout: str
    outcomes: dict[str, str]
    terminal: dict[str, set[Key]]
    json: dict[str, Any]
    sarif: dict[str, Any]
    patch: str | None
    sections: dict[str, list[str]] = field(default_factory=dict)


_SECTION = re.compile(r"^={3,} (.+?) ={3,}$")
_FAILURE = re.compile(r"^_{3,} (\S+) _{3,}$")
_WARNED = re.compile(r"^(tests/test_app\.py::\S+)$")
_KIND = re.compile(r"kind: (\S+)  label: (.+?)\s*$")
_SHORT = re.compile(r"^(PASSED|FAILED|ERROR) (tests/test_app\.py::\S+)")


def _terminal_findings(lines: list[str]) -> dict[str, set[Key]]:
    """(kind, label) per test from the failures and the warnings summary."""
    found: dict[str, set[Key]] = {}
    section = ""
    current: set[str] = set()
    previous_was_nodeid = False
    for line in lines:
        heading = _SECTION.match(line)
        if heading:
            section = heading[1]
            current = set()
            previous_was_nodeid = False
            continue
        if section == "FAILURES":
            failure = _FAILURE.match(line)
            if failure:
                current = {T + failure[1]}
        elif section == "warnings summary":
            warned = _WARNED.match(line)
            if warned:
                # pytest lists the tests of one warning text together
                if not previous_was_nodeid:
                    current = set()
                current.add(warned[1])
                previous_was_nodeid = True
                continue
        else:
            continue
        previous_was_nodeid = False
        kind = _KIND.search(line)
        if kind:
            for nodeid in current:
                found.setdefault(nodeid, set()).add((kind[1], kind[2]))
    return found


def _sections(lines: list[str]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    name = None
    for line in lines:
        heading = _SECTION.match(line)
        if heading:
            name = heading[1]
            sections[name] = []
        elif name is not None:
            sections[name].append(line)
    return sections


def _run(pytester: pytest.Pytester, combo: Combo, *, xdist: bool) -> Run:
    tag = "xdist" if xdist else "serial"
    args = [
        "-p",
        "no:cacheprovider",
        "-rA",
        "-W",
        "error::pytest_queryguard.QueryGuardInternalWarning",
        f"--queryguard-json=out-{tag}.json",
        f"--queryguard-sarif=out-{tag}.sarif",
    ]
    if combo.patch:
        args.append(f"--queryguard-write-patch=out-{tag}.patch")
    if combo.show_patch:
        args.append("--queryguard-show-patch")
    if xdist:
        args += ["-p", "xdist", "-n", "2"]
    result = pytester.runpytest_subprocess(*args)
    lines = result.outlines
    if xdist:
        result.stdout.fnmatch_lines(["created: 2/2 workers"])
    outcomes = {}
    for line in lines:
        short = _SHORT.match(line)
        if short:
            outcomes[short[2]] = short[1].lower()
    root = pytester.path
    patch_file = root / f"out-{tag}.patch"
    return Run(
        ret=int(result.ret),
        stdout="\n".join(lines),
        outcomes=outcomes,
        terminal=_terminal_findings(lines),
        json=json.loads((root / f"out-{tag}.json").read_text()),
        sarif=json.loads((root / f"out-{tag}.sarif").read_text()),
        patch=patch_file.read_text() if patch_file.exists() else None,
        sections=_sections(lines),
    )


# --- the checks ---


def _json_findings(run: Run, *, baselined: bool | None = None) -> dict[str, set[Key]]:
    found: dict[str, set[Key]] = {}
    for nodeid, entry in run.json["tests"].items():
        keys = {
            (f["kind"], f["label"])
            for f in entry["findings"]
            if baselined is None or f["baselined"] is baselined
        }
        if keys:
            found[nodeid] = keys
    return found


def _non_empty(found: dict[str, set[Key]]) -> dict[str, set[Key]]:
    return {nodeid: keys for nodeid, keys in found.items() if keys}


def _results(run: Run) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = run.sarif["runs"][0]["results"]
    return results


def _check_outcomes(run: Run, combo: Combo) -> None:
    assert run.outcomes == combo.outcomes(), run.stdout
    # budgets always fail, so every run fails
    assert run.ret == pytest.ExitCode.TESTS_FAILED, run.stdout


def _check_no_internal_error(run: Run) -> None:
    assert "QueryGuardInternalWarning" not in run.stdout
    assert "measurement stopped" not in run.stdout
    assert "internal error" not in run.stdout


def _check_findings(run: Run, combo: Combo) -> None:
    """The terminal, the JSON and the SARIF file report the same findings."""
    expected = _non_empty(combo.reported())
    assert run.terminal == expected, run.stdout
    assert _json_findings(run, baselined=False) == expected
    assert _json_findings(run, baselined=True) == _non_empty(
        {T + "test_baselined": combo.baselined()}
    )
    assert _json_findings(run) == _non_empty(combo.found())
    assert run.json["complete"] is True
    assert sorted(run.json["tests"]) == sorted(NODEIDS)

    # SARIF: one result per identity of a finding that counts, merged over tests
    by_identity: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for nodeid, entry in run.json["tests"].items():
        for f in entry["findings"]:
            if not f["baselined"]:
                key = (f["kind"], f["label"], f["file"], f["function"])
                by_identity.setdefault(key, []).append({"test": nodeid, **f})
    results = _results(run)
    assert len(results) == len(by_identity), results
    for result in results:
        kind = result["ruleId"].removeprefix("queryguard/")
        location = result["locations"][0]
        file = location["physicalLocation"]["artifactLocation"]["uri"]
        function = location["logicalLocations"][0]["name"]
        label = result["properties"]["label"]
        entries = by_identity[(kind, label, file, function)]
        assert result["level"] == ("error" if combo.fails(kind) else "warning")
        assert result["properties"]["count"] == sum(f["count"] for f in entries)
        assert result["properties"].get("tests", 1) == len(entries)
        if len(entries) > 1:
            peak = max(f["count"] for f in entries)
            assert result["properties"]["peakCount"] == peak
        assert result["locations"][0]["physicalLocation"]["region"]["startLine"] == (
            min(f["line"] for f in entries)
        )

    # compare against an empty base: every identity that counts is new
    comparison = compare.compare(
        {"tests": {}, "complete": True}, {"tests": run.json["tests"], "complete": True}
    )
    new = {(r.kind, r.label, r.file, r.function) for r in comparison.new}
    assert new == set(by_identity)

    # each JSON message is the text the terminal shows; the error strict mode
    # raises at the access has no sql: line, the SELECT has not run yet
    shown = {
        re.sub(r"^E\s", "", line.strip()).strip() for line in run.stdout.splitlines()
    }
    raised = combo.strict and combo.fails(REL)
    for entries in by_identity.values():
        for f in entries:
            for line in f["message"].splitlines():
                if raised and f["kind"] == REL and line.startswith("sql: "):
                    continue
                assert line.strip() in shown, (line, run.stdout)

    # the grouped summary names the helper all four tests reach once
    groups = run.sections.get("queryguard: the same finding in several tests", [])
    assert any(
        re.match(r"\s+4 tests\s+relationship-load\s+Order\.customer\s+\(", line)
        for line in groups
    ), run.stdout
    customer = [r for r in results if r["properties"]["label"] == "Order.customer"]
    assert [r["properties"].get("tests") for r in customer] == [4]


def _git_apply(cwd: Path, patch: Path, *args: str) -> None:
    assert _GIT is not None
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(cwd.parent)}
    subprocess.run(
        [_GIT, "-c", "core.autocrlf=false", "apply", *args, str(patch)],
        cwd=cwd,
        check=True,
        env=env,
        capture_output=True,
    )


_LOADER = re.compile(r"\b(joinedload|selectinload)\((\w+\.\w+)\)")


def _apply_sarif_fixes(text: str, results: list[dict[str, Any]]) -> str:
    lines = text.splitlines(keepends=True)
    edits = []
    for result in results:
        for fix in result.get("fixes", []):
            for change in fix["artifactChanges"]:
                assert change["artifactLocation"]["uri"] == "app/queries.py"
                for replacement in change["replacements"]:
                    region = replacement["deletedRegion"]
                    assert region["startLine"] == region["endLine"]
                    assert region["startColumn"] == region["endColumn"]
                    edits.append(
                        (
                            region["startLine"],
                            region["startColumn"] - 1,  # ASCII: utf-16 == str
                            replacement["insertedContent"]["text"],
                        )
                    )
    for line, column, inserted in sorted(edits, reverse=True):
        old = lines[line - 1]
        lines[line - 1] = old[:column] + inserted + old[column:]
    return "".join(lines)


def _check_patch(run: Run, combo: Combo, pytester: pytest.Pytester) -> None:
    """Every patch is a reported relationship load; SARIF fixes make the same change."""
    reported_rel = {
        label
        for keys in combo.reported().values()
        for kind, label in keys
        if kind == REL
    }
    results = _results(run)
    fixed = {r["properties"]["label"] for r in results if r.get("fixes")}
    # the parent query line is certain for every relationship load reported
    assert fixed == reported_rel
    assert all(r["ruleId"] == f"queryguard/{REL}" for r in results if r.get("fixes")), (
        results
    )
    if not combo.patch:
        assert run.patch is None
        return
    assert run.patch is not None
    assert run.patch.startswith("--- a/app/queries.py\n+++ b/app/queries.py\n")
    added = {
        match[2]
        for line in run.patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
        for match in _LOADER.finditer(line)
    }
    removed = {
        match[2]
        for line in run.patch.splitlines()
        if line.startswith("-") and not line.startswith("---")
        for match in _LOADER.finditer(line)
    }
    assert added - removed == reported_rel, run.patch
    if combo.show_patch:
        # under its finding, also in the error strict mode raises at the access
        shown = {re.sub(r"^E\s", "", line).strip() for line in run.stdout.splitlines()}
        for line in run.patch.splitlines():
            if line.startswith(("+", "-")):
                assert line.strip() in shown, (line, run.stdout)
    if _GIT is None:
        return
    patch = pytester.path / "check.patch"
    patch.write_bytes(run.patch.encode())
    _git_apply(pytester.path, patch, "--check")
    copy = pytester.path / "applied"
    if copy.exists():
        shutil.rmtree(copy)
    (copy / "app").mkdir(parents=True)
    original = pytester.path / "app" / "queries.py"
    shutil.copyfile(original, copy / "app" / "queries.py")
    _git_apply(copy, patch)
    patched = (copy / "app" / "queries.py").read_text()
    assert _apply_sarif_fixes(original.read_text(), results) == patched


def _check_stale(run: Run, combo: Combo, *, xdist: bool) -> None:
    stale = run.sections.get("queryguard: stale ignores", [])
    # docs/ignoring.md: the ini patterns and markers are matched in the
    # workers, so pytest-xdist lists only the ignore() blocks
    assert (STALE_PATTERN in "\n".join(stale)) is not xdist, run.stdout
    assert (STALE_MARKER in "\n".join(stale)) is not xdist, run.stdout
    assert any(line.strip() == STALE_BLOCK for line in stale), run.stdout
    # only the stale ones: what the ignores used is never listed
    text = "\n".join(stale)
    assert "Customer.orders" not in text
    assert "ignore('relationship-load')" not in text
    entries = run.sections.get("queryguard: stale baseline entries", [])
    if combo.baseline:
        assert [line.strip() for line in entries if "Gone.away" in line] == [
            STALE_ENTRY
        ], run.stdout
        assert "Order.items" not in "\n".join(entries)
    else:
        assert not entries


def _check_run(
    run: Run, combo: Combo, pytester: pytest.Pytester, *, xdist: bool
) -> None:
    _check_no_internal_error(run)
    _check_outcomes(run, combo)
    _check_findings(run, combo)
    _check_patch(run, combo, pytester)
    _check_stale(run, combo, xdist=xdist)


def _comparable_json(run: Run) -> dict[str, Any]:
    tests = {}
    for nodeid, entry in run.json["tests"].items():
        tests[nodeid] = {k: v for k, v in entry.items() if k != "db_time"}
    return tests


def _check_same(serial: Run, parallel: Run) -> None:
    """pytest-xdist changes nothing but the documented summary sections."""
    assert parallel.outcomes == serial.outcomes
    assert parallel.ret == serial.ret
    assert parallel.terminal == serial.terminal
    assert _comparable_json(parallel) == _comparable_json(serial)
    assert _results(parallel) == _results(serial)
    assert parallel.patch == serial.patch
    for name in (
        "queryguard: the same finding in several tests",
        "queryguard: stale baseline entries",
    ):
        assert parallel.sections.get(name) == serial.sections.get(name)
    comparison = compare.compare(serial.json, parallel.json)
    assert not (
        comparison.more or comparison.fewer or comparison.new or comparison.fixed
    )
    moved = trend.trend([serial.json, parallel.json], ["serial", "xdist"])
    assert moved.complete
    assert not (moved.changed or moved.new or moved.gone)
    assert [run.statements for run in moved.runs] == [
        sum(e["statements"] for e in serial.json["tests"].values())
    ] * 2


@pytest.mark.parametrize("combo", COMBOS, ids=[c.name for c in COMBOS])
def test_outputs_agree(pytester: pytest.Pytester, combo: Combo) -> None:
    _write_project(pytester, combo)
    serial = _run(pytester, combo, xdist=False)
    _check_run(serial, combo, pytester, xdist=False)
    if not combo.xdist:
        return
    if not _XDIST:
        pytest.skip("pytest-xdist is not installed")
    parallel = _run(pytester, combo, xdist=True)
    _check_run(parallel, combo, pytester, xdist=True)
    _check_same(serial, parallel)


STOPPED = """\
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Account, Order, make_session


def test_reads_after_the_n_plus_one():
    session = make_session()
    orders = session.scalars(select(Order).options(selectinload(Order.items))).all()
    accounts = session.scalars(select(Account)).all()
    assert accounts[0].name
    names = [order.customer.name for order in orders]  # strict raises here
    assert [len(order.items) for order in orders] == [2] * 6
    assert {(a.email, a.phone, a.street, a.city, a.country, a.notes, a.bio)
            for a in accounts}
    assert names
"""


@pytest.mark.parametrize("strict", [False, True], ids=["report", "strict"])
def test_strict_reports_no_unused_load_the_test_never_reached(
    pytester: pytest.Pytester, strict: bool
) -> None:
    """A test strict mode stopped never read what it would have read later.

    The eager load and the columns are read after the loop that raises, so
    only the relationship load is a finding, with and without strict mode.
    """
    _write_project(pytester, Combo("stopped"))
    (pytester.path / "tests" / "test_app.py").write_text(STOPPED)
    args = [
        "-p",
        "no:cacheprovider",
        "-o",
        "queryguard_unused_eager_load=fail",
        "-o",
        "queryguard_unused_columns=fail",
        "--queryguard-json=out.json",
        "--queryguard-sarif=out.sarif",
    ]
    if strict:
        args.append("--queryguard-strict")
    result = pytester.runpytest_subprocess(*args)
    result.assert_outcomes(failed=1)
    if strict:
        result.stdout.fnmatch_lines(["*NPlusOneDetected*"])
    doc = json.loads((pytester.path / "out.json").read_text())
    (entry,) = doc["tests"].values()
    assert [(f["kind"], f["label"]) for f in entry["findings"]] == [CUSTOMER]
    sarif = json.loads((pytester.path / "out.sarif").read_text())
    assert [r["ruleId"] for r in _results_of(sarif)] == [f"queryguard/{REL}"]
    assert "kind: unused" not in result.stdout.str()


def _results_of(sarif: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = sarif["runs"][0]["results"]
    return results


def test_strict_error_shows_the_patch(pytester: pytest.Pytester) -> None:
    """The error strict mode raises replaces the report, so it has the patch."""
    _write_project(pytester, Combo("strict-patch"))
    result = pytester.runpytest_subprocess(
        "-p",
        "no:cacheprovider",
        "--queryguard-strict",
        "--queryguard-show-patch",
        "-k",
        "test_nplusone",
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "E*NPlusOneDetected: queryguard strict: Order.customer*",
            "E*patch (git apply from the rootdir*",
            "E*+++ b/app/queries.py",
            "E*+*select(Order).options(joinedload(Order.customer)).order_by(*",
            "E*(queryguard_strict raises at the lazy load*",
        ]
    )


@pytest.mark.skipif(not _XDIST, reason="pytest-xdist is not installed")
def test_mcp_run_tests_agrees_with_the_files(pytester: pytest.Pytester) -> None:
    """The MCP server's run_tests returns the findings and patch of a pytest run."""
    combo = next(c for c in COMBOS if c.name == "xdist-strict-baseline-patch-fail-on")
    _write_project(pytester, combo)
    serial = _run(pytester, combo, xdist=False)
    reply = mcp.run_tests({"args": ["-p", "no:cacheprovider", "-n", "2"]})
    assert reply["exit_code"] == serial.ret
    assert reply["complete"] is True
    assert reply["rootdir"] == str(pytester.path)
    assert reply["tests"] == len(NODEIDS)
    assert reply["findings_total"] == len(reply["findings"])
    got = sorted(
        (f["test"], f["kind"], f["label"], f["baselined"], f["count"], f["message"])
        for f in reply["findings"]
    )
    expected = sorted(
        (nodeid, f["kind"], f["label"], f["baselined"], f["count"], f["message"])
        for nodeid, entry in serial.json["tests"].items()
        for f in entry["findings"]
    )
    assert got == expected
    assert reply["patch"] == serial.patch


STOPPED_MARKER = """\
import pytest

from app import queries
from app.models import make_session


@pytest.mark.allow_nplusone("Order.items")
def test_two_loops():
    session = make_session()
    queries.customer_names(session)  # strict raises here
    queries.item_counts(session)  # the N+1 the marker allows
"""


@pytest.mark.parametrize("strict", [False, True], ids=["report", "strict"])
def test_strict_lists_no_marker_of_a_stopped_test_as_stale(
    pytester: pytest.Pytester, strict: bool
) -> None:
    """The marker is for the second loop, which a strict run never reaches."""
    _write_project(pytester, Combo("stopped-marker"))
    (pytester.path / "tests" / "test_app.py").write_text(STOPPED_MARKER)
    args = ["-p", "no:cacheprovider"]
    if strict:
        args.append("--queryguard-strict")
    result = pytester.runpytest_subprocess(*args)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*kind: relationship-load  label: Order.customer"])
    result.stdout.no_fnmatch_line("*allow_nplusone patterns that matched no finding*")
    result.stdout.no_fnmatch_line("*test_two_loops: * Order.items")


TWO_HELPERS = """\
from sqlalchemy import select

from app.models import Order, make_session


def names_a(session):
    orders = session.scalars(select(Order).where(Order.id < 20)).all()
    return [order.customer.name for order in orders]


def names_b(session):
    orders = session.scalars(select(Order).where(Order.id >= 20)).all()
    return [order.customer.name for order in orders]


def test_two_helpers():
    session = make_session()
    names_a(session)
    names_b(session)
"""


@pytest.mark.parametrize("accepted_by", ["pattern", "baseline"])
@pytest.mark.parametrize("strict", [False, True], ids=["report", "strict"])
def test_strict_accepts_what_the_report_accepts_across_parent_lines(
    pytester: pytest.Pytester, strict: bool, accepted_by: str
) -> None:
    """One relationship lazy-loaded for the parents of two query lines.

    The report makes one finding, at the first lazy load (in ``names_a``), so
    an ignore pattern or a baseline entry for ``names_a`` accepts it. Strict
    mode must not raise at the loads of ``names_b`` then.
    """
    _write_project(pytester, Combo("two-helpers"))
    (pytester.path / "tests" / "test_app.py").write_text(TWO_HELPERS)
    args = ["-p", "no:cacheprovider", "--queryguard-json=out.json"]
    if accepted_by == "pattern":
        args += [
            "-o",
            "queryguard_ignore=relationship-load Order.customer function=names_a",
        ]
    else:
        entry = {
            "kind": REL,
            "label": "Order.customer",
            "file": "tests/test_app.py",
            "function": "names_a",
        }
        (pytester.path / "baseline.json").write_text(
            json.dumps({"version": 1, "findings": [entry]})
        )
        args.append("--queryguard-baseline=baseline.json")
    if strict:
        args.append("--queryguard-strict")
    result = pytester.runpytest_subprocess(*args)
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*NPlusOneDetected*")
    doc = json.loads((pytester.path / "out.json").read_text())
    (entry_,) = doc["tests"].values()
    expected = [] if accepted_by == "pattern" else [("names_a", True)]
    assert [(f["function"], f["baselined"]) for f in entry_["findings"]] == expected


def test_strict_error_has_no_patch_when_parents_come_from_two_lines(
    pytester: pytest.Pytester,
) -> None:
    """As in the report, a patch of one of two parent queries would not fix it."""
    _write_project(pytester, Combo("two-lines-patch"))
    (pytester.path / "tests" / "test_app.py").write_text(
        TWO_HELPERS.replace("Order.id < 20", "Order.id == 10")
    )
    result = pytester.runpytest_subprocess(
        "-p",
        "no:cacheprovider",
        "--queryguard-strict",
        "--queryguard-show-patch",
        "--queryguard-write-patch=out.patch",
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "E*NPlusOneDetected: queryguard strict: Order.customer*",
            "E*first at tests/test_app.py:8 in names_a",
            "E*parents loaded at tests/test_app.py:12 in names_b and 1 other place",
        ]
    )
    result.stdout.no_fnmatch_line("*patch (git apply*")
    assert (pytester.path / "out.patch").read_text() == ""
