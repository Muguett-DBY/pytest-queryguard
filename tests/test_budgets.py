from __future__ import annotations

import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from pytest_queryguard import (
    NPlusOneDetected,
    QueryBudgetExceeded,
    QueryLog,
    max_db_time,
    max_queries,
)

pytestmark = pytest.mark.allow_nplusone

MODELS = (Path(__file__).parent / "models.py").read_text()

BODY = """
import pytest
from sqlalchemy import create_engine, text


@pytest.mark.max_queries({limit})
def test_budget():
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        for _ in range(3):
            conn.execute(text("select 1"))
"""


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS)
    return pytester


def test_marker_fails_over_budget(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=BODY.format(limit=2))
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*expected at most 2 queries, got 3*"])


def test_marker_passes_at_budget(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=BODY.format(limit=3))
    project.runpytest().assert_outcomes(passed=1)


def test_budget_fails_even_in_off_mode(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=BODY.format(limit=1))
    result = project.runpytest("--queryguard-mode=off")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*expected at most 1 query, got 3*"])


def test_marker_rejects_invalid_limit(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=BODY.format(limit="'x'"))
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*max_queries*test_budget*non-negative integer*"])


def test_budget_failure_comes_before_nplusone(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        import pytest
        from qgmodels import Order, make_sync_session


        @pytest.mark.max_queries(1)
        def test_both():
            session = make_sync_session()
            for order in session.query(Order).all():
                _ = order.customer.name
        """
    )
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    out = result.stdout.str()
    assert out.index("expected at most 1 query") < out.index("N+1 detected")


def test_context_manager_counts_only_inside_block() -> None:
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(text("select 1"))
        with max_queries(1) as log:
            conn.execute(text("select 2"))
        conn.execute(text("select 3"))
    assert log.count == 1


@pytest.mark.parametrize(
    "make_budget", [lambda: max_queries(5), lambda: max_db_time(60)]
)
def test_budget_managers_yield_the_public_query_log(
    make_budget: Callable[[], Any],
) -> None:
    engine = create_engine("sqlite://")
    with engine.connect() as conn, make_budget() as log:
        conn.execute(text("select 1"))
        conn.execute(text("select 2"))
        assert isinstance(log, QueryLog)
        assert log.count == 2  # live inside the block
    assert type(log) is QueryLog
    assert [q.sql for q in log.queries] == ["select 1", "select 2"]
    assert log.db_time > 0
    assert len(log.slowest(1)) == 1
    log.assert_no_nplusone()  # the whole QueryLog API works


def test_the_log_of_an_exceeded_budget_is_still_there() -> None:
    engine = create_engine("sqlite://")
    holder: list[QueryLog] = []
    with (
        pytest.raises(QueryBudgetExceeded),
        engine.connect() as conn,
        max_queries(1) as log,
    ):
        holder.append(log)
        conn.execute(text("select 1"))
        conn.execute(text("select 2"))
    assert holder[0].count == 2


def test_the_yielded_log_checks_for_nplusone() -> None:
    engine = create_engine("sqlite://")
    with engine.connect() as conn, max_queries(10) as log:
        for value in range(3):
            conn.execute(text("select 1 where :v = :v"), {"v": value})
    with pytest.raises(NPlusOneDetected):
        log.assert_no_nplusone(repeat_threshold=3)


def test_context_manager_raises_when_exceeded() -> None:
    engine = create_engine("sqlite://")
    with (
        pytest.raises(QueryBudgetExceeded, match="at most 1 query, got 2"),
        max_queries(1),
        engine.connect() as conn,
    ):
        conn.execute(text("select 1"))
        conn.execute(text("select 2"))


def test_context_manager_keeps_original_exception() -> None:
    engine = create_engine("sqlite://")
    with pytest.raises(KeyError), max_queries(0), engine.connect() as conn:
        conn.execute(text("select 1"))
        raise KeyError("boom")


def test_context_manager_works_without_pytest() -> None:
    script = textwrap.dedent(
        """
        from sqlalchemy import create_engine, text
        from pytest_queryguard import max_queries

        engine = create_engine("sqlite://")
        with engine.connect() as conn:
            with max_queries(1):
                conn.execute(text("select 1"))
                conn.execute(text("select 2"))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "expected at most 1 query" in proc.stderr


def test_negative_limit_rejected() -> None:
    with pytest.raises(ValueError, match="limit"):
        max_queries(-1)


NPLUSONE_BUDGET = """
import pytest
from qgmodels import Order, make_sync_session


@pytest.mark.max_queries(1)
def test_both():
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name
"""


def test_warn_mode_budget_and_nplusone_in_one_failure(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(test_a=NPLUSONE_BUDGET)
    result = project.runpytest("--queryguard-mode=warn")
    result.assert_outcomes(failed=1)
    out = result.stdout.str()
    assert out.index("expected at most 1 query") < out.index("N+1 detected")
    assert "QueryGuardWarning" not in out


def test_off_mode_unmarked_test_is_not_measured(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        from pytest_queryguard import collector
        from qgmodels import Order, make_sync_session


        def test_plain():
            session = make_sync_session()
            for order in session.query(Order).all():
                _ = order.customer.name
            assert collector.active() == ()
        """
    )
    # In a subprocess: in-process, the outer session's collector is active too.
    result = project.runpytest_subprocess("--queryguard-mode=off")
    result.assert_outcomes(passed=1)


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        ("max_queries(-1)", "*non-negative integer*-1*"),
        ("max_queries('3')", "*non-negative integer*'3'*"),
        ("max_queries()", "*non-negative integer*None*"),
        ("max_queries(1, 2)", "*takes a single argument*"),
        ("max_queries(1, n=2)", "*limit more than once*"),
        ("max_queries(1, limit=2)", "*limit more than once*"),
        ("max_queries(limit=1, n=2)", "*limit more than once*"),
        ("max_queries(m=2)", "*takes a single argument*"),
    ],
)
def test_marker_rejects_bad_arguments(
    project: pytest.Pytester, marker: str, message: str
) -> None:
    project.makepyfile(
        test_a=f"""
        import pytest


        @pytest.mark.{marker}
        def test_x():
            pass
        """
    )
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines([message])


def test_marker_zero_budget(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        import pytest
        from sqlalchemy import create_engine, text


        @pytest.mark.max_queries(0)
        def test_none():
            create_engine("sqlite://")


        @pytest.mark.max_queries(0)
        def test_one():
            with create_engine("sqlite://").connect() as conn:
                conn.execute(text("select 1"))
        """
    )
    result = project.runpytest()
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*expected at most 0 queries, got 1*"])


def test_marker_keyword_form(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=BODY.format(limit="limit=2"))
    result = project.runpytest("test_a.py")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*expected at most 2 queries, got 3*"])
    project.makepyfile(test_b=BODY.format(limit="limit=3"))
    project.runpytest("test_b.py").assert_outcomes(passed=1)


STRICT = ("-W", "error::DeprecationWarning", "-W", "error::UserWarning")


def test_marker_n_still_works_with_a_deprecation_warning(
    project: pytest.Pytester,
) -> None:
    """``n=`` was the keyword of 0.1 to 0.3; it works, and warns since 0.8."""
    project.makepyfile(test_a=BODY.format(limit="n=2"))
    result = project.runpytest("test_a.py")
    result.assert_outcomes(failed=1, warnings=1)
    result.stdout.fnmatch_lines(
        [
            "*expected at most 2 queries, got 3*",
            (
                "*QueryGuardDeprecationWarning: max_queries(n=...) is deprecated and "
                "will be removed in pytest-queryguard 2.0; pass the limit "
                "positionally, max_queries(2), or as limit=*"
            ),
        ]
    )
    project.makepyfile(test_b=BODY.format(limit="n=3"))
    project.runpytest("test_b.py").assert_outcomes(passed=1, warnings=1)
    # an error filter for deprecations fails the test on the warning
    result = project.runpytest("test_b.py", *STRICT)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*QueryGuardDeprecationWarning*n=*"])
    result.stdout.no_fnmatch_line("*measurement stopped*")


@pytest.mark.parametrize("limit", ["2", "limit=2"])
def test_marker_limit_spellings_do_not_warn(
    project: pytest.Pytester, limit: str
) -> None:
    project.makepyfile(test_a=BODY.format(limit=limit))
    project.runpytest("test_a.py", *STRICT).assert_outcomes(failed=1, warnings=0)


ALIASES = """
import pytest
from sqlalchemy import create_engine, text


def run(count):
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        for _ in range(count):
            conn.execute(text("select 1"))


@pytest.mark.queryguard_max_queries(2)
def test_over():
    run(3)


@pytest.mark.queryguard_max_queries(limit=3)
def test_at():
    run(3)


@pytest.mark.queryguard_max_queries(5)
@pytest.mark.max_queries(1)
def test_prefixed_wins():
    run(3)


@pytest.mark.queryguard_max_db_time(0)
def test_time_over():
    run(1)


@pytest.mark.queryguard_max_db_time(seconds=60)
@pytest.mark.max_db_time(0)
def test_time_prefixed_wins():
    run(1)
"""


def test_prefixed_budget_markers(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=ALIASES)
    result = project.runpytest("--strict-markers", *STRICT)
    result.assert_outcomes(passed=3, failed=2)
    result.stdout.fnmatch_lines(
        [
            "*expected at most 2 queries, got 3*",
            "FAILED test_a.py::test_over*",
            "FAILED test_a.py::test_time_over*",
        ],
        consecutive=False,
    )


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        ("queryguard_max_queries(1, 2)", "queryguard_max_queries on *single argument*"),
        ("queryguard_max_queries(-1)", "queryguard_max_queries on *non-negative*"),
        ("queryguard_max_db_time(-1)", "queryguard_max_db_time on *non-negative*"),
    ],
)
def test_prefixed_budget_markers_name_themselves_in_errors(
    project: pytest.Pytester, marker: str, message: str
) -> None:
    project.makepyfile(
        test_a=f"""
        import pytest


        @pytest.mark.{marker}
        def test_x():
            pass
        """
    )
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines([f"*{message}*"])


def test_marker_counts_writes(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        import pytest
        from sqlalchemy import create_engine, text


        @pytest.mark.max_queries(2)
        def test_writes():
            engine = create_engine("sqlite://")
            with engine.begin() as conn:
                conn.execute(text("create table t (x integer)"))
                conn.execute(text("insert into t values (1)"))
                conn.execute(text("insert into t values (2)"))
        """
    )
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*got 3*", "*insert into t values*"])


def test_nested_context_manager_keeps_outer_count(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a="""
        import pytest
        from sqlalchemy import create_engine, text
        from pytest_queryguard import max_queries


        @pytest.mark.max_queries(3)
        def test_nested():
            with create_engine("sqlite://").connect() as conn:
                conn.execute(text("select 1"))
                with max_queries(1):
                    conn.execute(text("select 2"))
                conn.execute(text("select 3"))
                conn.execute(text("select 4"))
        """
    )
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*expected at most 3 queries, got 4*"])


def test_failure_lists_statements_with_locations(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(test_a=BODY.format(limit=1))
    result = project.runpytest()
    result.stdout.fnmatch_lines(
        ["*1. select 1*(test_a.py:*)*", "*3. select 1*(test_a.py:*)*"]
    )


def test_context_manager_failure_hides_plugin_frame(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        from sqlalchemy import create_engine, text

        from pytest_queryguard import max_queries


        def test_cm():
            engine = create_engine("sqlite://")
            with engine.connect() as conn, max_queries(1):
                conn.execute(text("select 1"))
                conn.execute(text("select 2"))
        """
    )
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*with engine.connect() as conn, max_queries(1):*"])
    result.stdout.no_fnmatch_line("*raise QueryBudgetExceeded*")


def test_own_failure_wins_over_exceeded_budget(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=BODY.format(limit=1) + "    assert False\n")
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*assert False*"])
    result.stdout.no_fnmatch_line("*expected at most*")
