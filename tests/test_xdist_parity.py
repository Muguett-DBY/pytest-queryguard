"""The same project run serially and under pytest-xdist writes the same output.

Each test runs one project both ways (and in other run modes: ``-k``, ``-x``,
a collection error, ``--collect-only``, Ctrl-C, pytest-randomly seeds) and
compares the files and summary sections after normalizing what may differ
(timing, the order of entries a run is free to choose).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("xdist")

MODELS = (Path(__file__).parent / "models.py").read_text()

TEST_A = """
import pytest
from sqlalchemy import select
from qgmodels import Customer, Order, make_sync_session
from pytest_queryguard import ignore


def test_a_orders():
    session = make_sync_session()
    for order in session.scalars(select(Order)).all():
        _ = order.customer.name


@pytest.mark.parametrize("i", range(3))
def test_a_clean(i):
    make_sync_session().query(Order).all()


def test_a_customers():
    session = make_sync_session()
    for customer in session.scalars(select(Customer)).all():
        _ = customer.orders


def test_a_block_unused():
    with ignore("relationship-load"):
        make_sync_session().query(Order).all()


@pytest.mark.queryguard_ignore("relationship-load Nothing.a")
def test_a_marker_stale():
    make_sync_session().query(Order).all()
"""

TEST_B = """
import pytest
from sqlalchemy import select
from qgmodels import Customer, Order, make_sync_session


def test_b_orders():
    session = make_sync_session()
    for order in session.scalars(select(Order)).all():
        _ = order.customer.name


@pytest.mark.parametrize("i", range(3))
def test_b_clean(i):
    session = make_sync_session()
    for _ in range(i + 1):
        session.query(Order).all()


def test_b_customers():
    session = make_sync_session()
    for customer in session.scalars(select(Customer)).all():
        _ = customer.orders


@pytest.mark.queryguard_ignore("relationship-load Nothing.b")
def test_b_marker_stale():
    make_sync_session().query(Order).all()
"""

TEST_C = """
from qgmodels import Order, make_sync_session


def test_c_orders():
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name


def test_c_fails():
    assert 0
"""

INI = """
[pytest]
queryguard_mode = warn
queryguard_ignore =
    relationship-load Nothing.here
    relationship-load Order.customer function=test_c_orders
"""

OUTPUTS = (
    "--queryguard-json=out.json",
    "--queryguard-sarif=out.sarif",
    "--queryguard-write-patch=out.patch",
)

XDIST = pytest.mark.parametrize(
    "xdist",
    [
        ("-n", "2"),
        ("-n", "3"),
        ("-n", "2", "--dist", "loadfile"),
        ("-n", "3", "--dist", "loadscope"),
        ("-n", "2", "--dist", "worksteal"),
    ],
    ids=lambda args: "-".join(a.lstrip("-") for a in args),
)


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS, test_a=TEST_A, test_b=TEST_B, test_c=TEST_C)
    pytester.makeini(INI)
    return pytester


def _run(project: pytest.Pytester, *args: str) -> pytest.RunResult:
    return project.runpytest_subprocess(*args, "-p", "no:cacheprovider")


_SECTION = re.compile(r"^=+ (queryguard[^=]*?) =+$")
_ANY_SECTION = re.compile(r"^(=+ .* =+|_+ .* _+|!+ .* !+)$")


def _sections(result: pytest.RunResult, root: Path) -> dict[str, list[str]]:
    """The queryguard summary sections, with the project path left out."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in result.outlines:
        match = _SECTION.match(line)
        if match:
            current = sections.setdefault(match.group(1), [])
            continue
        if _ANY_SECTION.match(line):
            current = None
            continue
        if current is not None and line.strip():
            current.append(line.replace(str(root), "<root>"))
    return sections


def _outputs(project: pytest.Pytester) -> dict[str, Any]:
    """The files the run wrote, normalized: no timing, no project path."""
    found: dict[str, Any] = {}
    for name in ("out.json", "out.sarif", "out.patch", "bl.json", "bud.json"):
        path = project.path / name
        if not path.exists():
            found[name] = None
            continue
        text = path.read_text().replace(str(project.path), "<root>")
        if name == "out.json":
            document = json.loads(text)
            for entry in document["tests"].values():
                entry.pop("db_time")
            found[name] = document
        else:
            found[name] = text
    return found


def _both(
    project: pytest.Pytester, args: tuple[str, ...], xdist: tuple[str, ...]
) -> tuple[tuple[dict[str, Any], dict[str, list[str]]], ...]:
    """Run serially and with xdist, from the same files; (outputs, sections) each."""
    saved = {
        name: (project.path / name).read_bytes()
        for name in ("bl.json", "bud.json")
        if (project.path / name).exists()
    }
    runs = []
    for extra in ((), xdist):
        for name, data in saved.items():
            (project.path / name).write_bytes(data)
        for name in OUTPUTS:
            (project.path / name.split("=")[1]).unlink(missing_ok=True)
        result = _run(project, *OUTPUTS, *args, *extra)
        runs.append((_outputs(project), _sections(result, project.path)))
    return tuple(runs)


def _record_baseline(project: pytest.Pytester) -> None:
    result = _run(
        project,
        "--queryguard-baseline=bl.json",
        "--queryguard-update-baseline",
        "--queryguard-budgets=bud.json",
        "--queryguard-update-budgets",
    )
    result.stdout.fnmatch_lines(["*queryguard: baseline updated*"])


@XDIST
def test_full_run_writes_the_same_outputs_and_summary(
    project: pytest.Pytester, xdist: tuple[str, ...]
) -> None:
    (serial, serial_sections), (parallel, parallel_sections) = _both(
        project, ("--queryguard-report",), xdist
    )
    assert parallel == serial
    assert parallel_sections == serial_sections
    assert serial["out.patch"].count("+++ b/") == 2
    assert serial_sections["queryguard: stale ignores"] == [
        "queryguard_ignore patterns that matched no finding in this run:",
        "  relationship-load Nothing.here",
        "queryguard_ignore marker patterns that matched no finding in their test:",
        "  test_a.py::test_a_marker_stale: * relationship-load Nothing.a",
        "  test_b.py::test_b_marker_stale: * relationship-load Nothing.b",
        "ignore() blocks that left out no finding in this run:",
        "  test_a.py:25: ignore('relationship-load')",
        "Remove them if the finding is fixed, or fix a mistyped pattern.",
    ]
    report = serial_sections["queryguard report"]
    assert len(report) == 10
    # equal counts are listed by node id, whatever order the tests ran in
    counts = [(-int(line.split()[0]), line.split()[1]) for line in report]
    assert counts == sorted(counts)


@pytest.mark.parametrize("xdist", [("-n", "2"), ("-n", "3", "--dist", "loadscope")])
def test_baseline_and_budget_update_is_the_same(
    project: pytest.Pytester, xdist: tuple[str, ...]
) -> None:
    _record_baseline(project)
    args = (
        "--queryguard-baseline=bl.json",
        "--queryguard-update-baseline",
        "--queryguard-budgets=bud.json",
        "--queryguard-update-budgets",
    )
    (serial, serial_sections), (parallel, parallel_sections) = _both(
        project, args, xdist
    )
    assert parallel == serial
    assert parallel_sections == serial_sections
    assert serial["bl.json"].count('"kind"') == 4


SHUFFLE = """
import os
import random


def pytest_collection_modifyitems(items):
    # the reordering pytest-randomly does, with the seed of the run; every
    # pytest-xdist worker gets the same order, as it requires
    random.Random(int(os.environ["QG_SEED"])).shuffle(items)
"""


def test_the_order_tests_run_in_does_not_change_the_outputs(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.makeconftest(SHUFFLE)
    monkeypatch.setenv("QG_SEED", "0")
    _record_baseline(project)
    runs = []
    for seed, xdist in (
        (1, ()),
        (2, ()),
        (3, ("-n", "3")),
        (4, ("-n", "2", "--dist", "worksteal")),
    ):
        monkeypatch.setenv("QG_SEED", str(seed))
        (project.path / "out.patch").unlink(missing_ok=True)
        result = _run(
            project,
            *OUTPUTS,
            "--queryguard-baseline=bl.json",
            "--queryguard-report",
            *xdist,
        )
        runs.append((_outputs(project), _sections(result, project.path)))
    assert all(run == runs[0] for run in runs[1:])
    assert len(runs[0][1]["queryguard: stale ignores"]) == 8  # with the two markers


PARTIAL = pytest.mark.parametrize(
    "args",
    [
        ("-k", "test_a"),
        ("--deselect", "test_b.py::test_b_orders"),
        ("test_a.py",),
        ("-x", "-p", "no:randomly"),
        ("--maxfail=1", "-p", "no:randomly"),
    ],
    ids=["k", "deselect", "file", "x", "maxfail"],
)


def _fix_test_a_orders(project: pytest.Pytester) -> None:
    """Fix the N+1 of test_a_orders, so its baseline entry becomes stale."""
    path = project.path / "test_a.py"
    path.write_text(
        path.read_text()
        .replace("select(Order))", "select(Order).options(joinedload(Order.customer)))")
        .replace(
            "from sqlalchemy import select",
            "from sqlalchemy import select\nfrom sqlalchemy.orm import joinedload",
        )
    )


_STALE_A = "  relationship-load Order.customer (test_a.py, test_a_orders)"
_INI_STALE = "queryguard_ignore patterns that matched no finding in this run:"


def test_a_full_run_lists_the_fixed_entry_and_the_unused_pattern(
    project: pytest.Pytester,
) -> None:
    _record_baseline(project)
    _fix_test_a_orders(project)
    for xdist in ((), ("-n", "2")):
        result = _run(project, "--queryguard-baseline=bl.json", *xdist)
        sections = _sections(result, project.path)
        assert sections["queryguard: stale baseline entries"][1:2] == [_STALE_A]
        assert _INI_STALE in sections["queryguard: stale ignores"]


@PARTIAL
@pytest.mark.parametrize("xdist", [(), ("-n", "2")], ids=["serial", "n2"])
def test_a_partial_run_claims_nothing_stale_for_tests_that_did_not_run(
    project: pytest.Pytester, args: tuple[str, ...], xdist: tuple[str, ...]
) -> None:
    _record_baseline(project)
    _fix_test_a_orders(project)
    result = _run(project, "--queryguard-baseline=bl.json", *args, *xdist)
    sections = _sections(result, project.path)
    stale = sections.get("queryguard: stale baseline entries", [])
    # only the entry of test_a_orders, which ran (-x may stop before it)
    assert [line for line in stale if line.startswith("  ")] in ([_STALE_A], [])
    if args[0] != "-x" and args[0] != "--maxfail=1":
        assert stale[1:2] == [_STALE_A]
    # an ini pattern is for any test: a partial run cannot tell it is stale
    ignores = sections.get("queryguard: stale ignores", [])
    assert _INI_STALE not in ignores
    if args[0] in ("-k", "test_a.py"):
        marker = "  test_a.py::test_a_marker_stale: * relationship-load Nothing.a"
        assert marker in ignores


@pytest.mark.parametrize("xdist", [(), ("-n", "2")], ids=["serial", "n2"])
@pytest.mark.parametrize("case", ["collection-error", "collect-only", "ctrl-c"])
def test_an_unfinished_run_claims_nothing_stale(
    project: pytest.Pytester, case: str, xdist: tuple[str, ...]
) -> None:
    _record_baseline(project)
    _fix_test_a_orders(project)
    args: tuple[str, ...] = ()
    if case == "collection-error":
        project.makepyfile(test_d="import no_such_module_qg\n")
    elif case == "collect-only":
        args = ("--collect-only",)
    else:
        project.makepyfile(
            test_0="def test_0_interrupt():\n    raise KeyboardInterrupt\n"
        )
    (project.path / "out.sarif").write_text("previous")
    result = _run(
        project,
        "--queryguard-baseline=bl.json",
        *OUTPUTS,
        "-p",
        "no:randomly",
        *args,
        *xdist,
    )
    sections = _sections(result, project.path)
    stale = sections.get("queryguard: stale baseline entries", [])
    if xdist and case == "collection-error":
        # pytest-xdist runs the tests of the files that were collected
        assert stale[1:2] == [_STALE_A]
    else:
        assert not stale
    assert _INI_STALE not in sections.get("queryguard: stale ignores", [])
    # an empty or partial SARIF file would close the alerts of the tests
    # that did not run
    assert (project.path / "out.sarif").read_text() == "previous"
    assert sections["queryguard: SARIF"] == [
        line for line in sections["queryguard: SARIF"] if "not written" in line
    ]
    if case != "collect-only":
        assert any(
            "incomplete" in line for line in sections["queryguard: patch written"]
        )


INDEX_CHECK = """
from qgmodels import Order, make_sync_session


def test_other_dialect():
    session = make_sync_session()
    session.get_bind().dialect.name = "oracle"
    assert session.query(Order).filter(Order.customer_id == 1).count() == 2
"""


@pytest.mark.parametrize("xdist", [("-n", "2"), ("-n", "2", "--dist", "loadfile")])
def test_the_index_check_summary_comes_from_the_workers(
    project: pytest.Pytester, xdist: tuple[str, ...]
) -> None:
    project.makepyfile(test_d=INDEX_CHECK)
    serial = _run(project, "--queryguard-index-check")
    parallel = _run(project, "--queryguard-index-check", *xdist)
    expected = _sections(serial, project.path)["queryguard index check"]
    assert expected[0].startswith("oracle statements were not checked")
    assert _sections(parallel, project.path)["queryguard index check"] == expected


def test_a_crashed_worker_leaves_the_report_and_the_stale_lists_incomplete(
    project: pytest.Pytester,
) -> None:
    _record_baseline(project)
    _fix_test_a_orders(project)
    project.makepyfile(test_0="import os\n\n\ndef test_0_crash():\n    os._exit(3)\n")
    result = _run(
        project,
        "-n",
        "2",
        "--queryguard-baseline=bl.json",
        "--queryguard-report",
        "--queryguard-index-check",
    )
    sections = _sections(result, project.path)
    assert sections["queryguard report"][0] == (
        "queryguard report: incomplete, a pytest-xdist worker did not send its counts"
    )
    assert sections["queryguard index check"][-1].startswith(
        "incomplete: a pytest-xdist worker did not send"
    )
    # the crashed worker's tests may have used a pattern or held an entry
    assert "queryguard: stale baseline entries" not in sections
    assert _INI_STALE not in sections.get("queryguard: stale ignores", [])
