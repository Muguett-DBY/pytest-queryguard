"""The plugin under a real pytest-xdist run (``-n 2`` and ``-n 4``).

Every test starts a fresh pytest process with workers, so the plugin runs
the way it does for users: once in each worker and once in the controller.

Behaviour these tests pin down (observed with pytest-xdist 3.8):

* Findings, budgets, ``allow_nplusone`` and ``queryguard_ignore`` work in the
  workers, and the failure text reaches the controller's output.
* The same tests pass and fail as in a serial run.
* Measurement state is per worker process and per test: nothing leaks from
  one test to the next on a worker, or between workers.
* The workers send their counts, ignores and index-check notes to the
  controller, so ``--queryguard-report`` and the summary sections are those of
  a serial run (tests/test_xdist_parity.py compares the two).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("xdist")

MODELS = (Path(__file__).parent / "models.py").read_text()

WORKERS = pytest.mark.parametrize("workers", [2, 4])

MIXED = """
import pytest
from qgmodels import Order, make_sync_session


def _lazy_loop():
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name


@pytest.mark.parametrize("i", range(8))
def test_clean(i):
    make_sync_session().query(Order).all()


@pytest.mark.parametrize("i", range(4))
def test_nplusone(i):
    _lazy_loop()


@pytest.mark.allow_nplusone
def test_allowed():
    _lazy_loop()


@pytest.mark.max_queries(1)
def test_over_budget():
    session = make_sync_session()
    session.query(Order).all()
    session.query(Order).all()
"""


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS)
    return pytester


def _failed_ids(result: pytest.RunResult) -> set[str]:
    return set(
        re.findall(r"^FAILED (\S+?)(?: - |$)", result.stdout.str(), re.MULTILINE)
    )


@WORKERS
def test_findings_and_budgets_come_from_the_workers(
    project: pytest.Pytester, workers: int
) -> None:
    project.makepyfile(test_mixed=MIXED)
    result = project.runpytest_subprocess("-n", str(workers), "-p", "no:cacheprovider")
    result.assert_outcomes(passed=9, failed=5)
    result.stdout.fnmatch_lines(
        [
            "*queryguard: N+1 detected*",
            "*Order.customer was lazy-loaded*",
            "*queryguard: expected at most 1 query, got *",
        ]
    )
    assert any(f"[gw{n}]" in result.stdout.str() for n in range(workers))


@WORKERS
def test_same_outcomes_as_a_serial_run(project: pytest.Pytester, workers: int) -> None:
    project.makepyfile(test_mixed=MIXED)
    serial = project.runpytest_subprocess("-p", "no:cacheprovider")
    parallel = project.runpytest_subprocess(
        "-n", str(workers), "-p", "no:cacheprovider"
    )
    assert serial.ret == parallel.ret == 1
    assert _failed_ids(serial) == _failed_ids(parallel)
    assert len(_failed_ids(parallel)) == 5


@WORKERS
def test_one_lazy_load_per_test_never_adds_up_across_tests(
    project: pytest.Pytester, workers: int
) -> None:
    # A single lazy load is below the threshold of two. If a worker kept its
    # collector between tests, the loads would add up and some test would fail.
    project.makepyfile(
        test_single="""
        import pytest
        from qgmodels import Order, make_sync_session


        @pytest.mark.parametrize("i", range(40))
        def test_one_lazy_load(i):
            session = make_sync_session()
            order = session.query(Order).first()
            _ = order.customer.name
        """
    )
    result = project.runpytest_subprocess("-n", str(workers), "-p", "no:cacheprovider")
    result.assert_outcomes(passed=40)


@WORKERS
def test_budgets_count_each_test_on_its_own(
    project: pytest.Pytester, workers: int
) -> None:
    # A test that runs k statements has a budget of exactly k. Another test's
    # statements on the same worker must not show up in its count. The
    # repeats are allowed: this test is about counting, not about N+1.
    body = "\n".join(
        f"""
@pytest.mark.allow_nplusone
@pytest.mark.max_queries({k})
def test_exact_{k}(session):
    for _ in range({k}):
        session.execute(text("select 1"))
"""
        for k in range(1, 13)
    )
    project.makepyfile(
        test_exact=f"""
        import pytest
        from sqlalchemy import text
        from qgmodels import make_sync_session


        @pytest.fixture
        def session():
            return make_sync_session()  # set-up statements are outside the budget

        {body.replace(chr(10), chr(10) + "        ")}
        """
    )
    result = project.runpytest_subprocess("-n", str(workers), "-p", "no:cacheprovider")
    result.assert_outcomes(passed=12)


@WORKERS
def test_an_ignore_in_the_ini_file_applies_in_every_worker(
    project: pytest.Pytester, workers: int
) -> None:
    project.makepyfile(test_mixed=MIXED)
    project.makeini(
        """
        [pytest]
        queryguard_ignore =
            relationship-load Order.customer
        """
    )
    result = project.runpytest_subprocess("-n", str(workers), "-p", "no:cacheprovider")
    # Only the over-budget test is left failing.
    result.assert_outcomes(passed=13, failed=1)
    assert _failed_ids(result) == {"test_mixed.py::test_over_budget"}


@WORKERS
def test_mode_off_under_workers_keeps_budgets(
    project: pytest.Pytester, workers: int
) -> None:
    project.makepyfile(test_mixed=MIXED)
    result = project.runpytest_subprocess(
        "-n", str(workers), "--queryguard-mode", "off", "-p", "no:cacheprovider"
    )
    result.assert_outcomes(passed=13, failed=1)
    assert _failed_ids(result) == {"test_mixed.py::test_over_budget"}


@WORKERS
def test_warn_mode_under_workers_fails_nothing_on_n_plus_one(
    project: pytest.Pytester, workers: int
) -> None:
    project.makepyfile(test_mixed=MIXED)
    result = project.runpytest_subprocess(
        "-n", str(workers), "--queryguard-mode", "warn", "-p", "no:cacheprovider"
    )
    result.assert_outcomes(passed=13, failed=1)


@WORKERS
def test_report_lists_the_tests_the_workers_ran(
    project: pytest.Pytester, workers: int
) -> None:
    project.makepyfile(test_mixed=MIXED)
    result = project.runpytest_subprocess(
        "-n", str(workers), "--queryguard-report", "-p", "no:cacheprovider"
    )
    result.stdout.fnmatch_lines(["*queryguard report*", "*test_mixed.py::*"])
    out = result.stdout.str()
    assert "not available with pytest-xdist" not in out
    assert "incomplete" not in out
    assert "no tests were measured" not in out


def test_report_works_with_n_0(project: pytest.Pytester) -> None:
    # -n 0 runs in the main process and registers no controller.
    project.makepyfile(test_mixed=MIXED)
    result = project.runpytest_subprocess(
        "-n", "0", "--queryguard-report", "-p", "no:cacheprovider"
    )
    out = result.stdout.str()
    assert "not available with pytest-xdist" not in out
    assert "test_mixed.py::test_over_budget" in out


@WORKERS
def test_stale_ignores_are_reported_by_the_controller(
    project: pytest.Pytester, workers: int
) -> None:
    project.makepyfile(test_mixed=MIXED)
    project.makeini(
        """
        [pytest]
        queryguard_ignore =
            relationship-load Nothing.here
        """
    )
    parallel = project.runpytest_subprocess(
        "-n", str(workers), "-p", "no:cacheprovider"
    )
    parallel.stdout.fnmatch_lines(
        ["*stale ignores*", "  relationship-load Nothing.here"]
    )
    serial = project.runpytest_subprocess("-p", "no:cacheprovider")
    serial.stdout.fnmatch_lines(["*stale ignores*", "  relationship-load Nothing.here"])


@WORKERS
def test_a_bad_option_is_a_usage_error_not_a_hang(
    project: pytest.Pytester, workers: int
) -> None:
    project.makepyfile(test_mixed=MIXED)
    result = project.runpytest_subprocess(
        "-n", str(workers), "--queryguard-mode", "bogus", "-p", "no:cacheprovider"
    )
    assert result.ret != 0
    assert "queryguard_mode" in result.stderr.str() + result.stdout.str() or (
        "--queryguard-mode" in result.stderr.str() + result.stdout.str()
    )
