"""The same finding in several tests, named once in the terminal summary (#228)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pytest_queryguard import groups
from pytest_queryguard.findings import KIND_RELATIONSHIP, KIND_REPEAT, Finding

MODELS = (Path(__file__).parent / "models.py").read_text()

SHARED = """
import pytest
from qgmodels import Customer, Order, make_sync_session


def _lazy_loop():
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name


@pytest.mark.parametrize("i", range(4))
def test_nplusone(i):
    _lazy_loop()


def test_once():
    session = make_sync_session()
    for customer in session.query(Customer).all():
        _ = customer.orders


def test_clean():
    make_sync_session().query(Order).all()
"""

HEADER = "*queryguard: the same finding in several tests*"


def _finding(kind: str, label: str, count: int = 3) -> Finding:
    return Finding(
        kind=kind,
        label=label,
        file="app.py",
        function="f",
        message="",
        count=count,
    )


def test_a_finding_is_counted_once_per_test_and_its_counts_add_up() -> None:
    store: groups.Groups = {}
    groups.add(store, [_finding(KIND_RELATIONSHIP, "Order.customer")], "t.py::b")
    groups.add(
        store,
        [
            _finding(KIND_RELATIONSHIP, "Order.customer", 2),
            # the same label from another call site in the same test
            _finding(KIND_RELATIONSHIP, "Order.customer", 5),
        ],
        "t.py::a",
    )
    group = store[(KIND_RELATIONSHIP, "Order.customer")]
    assert (group.tests, group.count, group.first) == (2, 10, "t.py::a")


def test_only_findings_of_two_tests_or_more_are_listed_most_tests_first() -> None:
    store: groups.Groups = {}
    for n in range(3):
        groups.add(store, [_finding(KIND_REPEAT, "SELECT 1")], f"t.py::r{n}")
    for n in range(2):
        groups.add(store, [_finding(KIND_RELATIONSHIP, "A.b")], f"t.py::a{n}")
    groups.add(store, [_finding(KIND_RELATIONSHIP, "C.d")], "t.py::c")
    assert groups.summary_lines(store) == [
        (
            "    3 tests  repeated-statement  SELECT 1  "
            "(9 times in total, first in t.py::r0)"
        ),
        "    2 tests  relationship-load  A.b  (6 times in total, first in t.py::a0)",
    ]


def test_a_long_statement_is_cut_with_a_star() -> None:
    sql = "SELECT " + ", ".join(f"c{n}" for n in range(60)) + "\nFROM t"
    store: groups.Groups = {}
    for n in range(2):
        groups.add(store, [_finding(KIND_REPEAT, sql)], f"t.py::{n}")
    (line,) = groups.summary_lines(store)
    label = line.split("  repeated-statement  ", 1)[1].split("  (", 1)[0]
    assert label.endswith("*") and len(label) == 101 and "\n" not in line


def test_the_wire_form_round_trips_and_merges() -> None:
    worker_a: groups.Groups = {}
    worker_b: groups.Groups = {}
    groups.add(worker_a, [_finding(KIND_RELATIONSHIP, "A.b")], "t.py::z")
    groups.add(worker_b, [_finding(KIND_RELATIONSHIP, "A.b")], "t.py::y")
    controller: groups.Groups = {}
    groups.merge_wire(controller, groups.to_wire(worker_a))
    groups.merge_wire(controller, groups.to_wire(worker_b))
    group = controller[(KIND_RELATIONSHIP, "A.b")]
    assert (group.tests, group.count, group.first) == (2, 6, "t.py::y")


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS, test_shared=SHARED)
    return pytester


def test_the_summary_names_a_finding_of_several_tests_once(
    project: pytest.Pytester,
) -> None:
    result = project.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, failed=5)
    result.stdout.fnmatch_lines(
        [
            HEADER,
            (
                "    4 tests  relationship-load  Order.customer  "
                "(* times in total, first in test_shared.py::test_nplusone[[]0[]])"
            ),
            "fix it once where it starts*",
        ]
    )
    # a finding of one test only is not repeated in the summary
    summary = result.stdout.str().split("several tests", 1)[1]
    assert "Customer.orders" not in summary


def test_no_summary_when_every_finding_belongs_to_one_test(
    project: pytest.Pytester,
) -> None:
    result = project.runpytest_subprocess(
        "-p", "no:cacheprovider", "-k", "nplusone and 0 or once"
    )
    result.assert_outcomes(failed=2)
    assert "several tests" not in result.stdout.str()


def test_ignored_findings_are_not_counted(project: pytest.Pytester) -> None:
    project.makeini("[pytest]\nqueryguard_ignore = relationship-load Order.customer\n")
    result = project.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=5, failed=1)
    assert "several tests" not in result.stdout.str()


@pytest.mark.parametrize("workers", [2, 4])
def test_the_workers_findings_are_grouped_on_the_controller(
    project: pytest.Pytester, workers: int
) -> None:
    pytest.importorskip("xdist")
    result = project.runpytest_subprocess("-n", str(workers), "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, failed=5)
    result.stdout.fnmatch_lines(
        [
            HEADER,
            (
                "    4 tests  relationship-load  Order.customer  "
                "(* times in total, first in test_shared.py::test_nplusone[[]0[]])"
            ),
        ]
    )
