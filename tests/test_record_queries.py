"""The public API for reading recorded queries: record_queries() and QueryLog."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import textwrap
import threading
import warnings
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

import pytest_queryguard
from pytest_queryguard import (
    CallSite,
    NPlusOneDetected,
    Query,
    QueryGuardInternalWarning,
    QueryLog,
    listeners,
    max_queries,
    record_queries,
)
from tests.models import Order, make_sync_session

# The outer plugin measures these tests too; some cause N+1 on purpose.
pytestmark = pytest.mark.allow_nplusone

MODELS = (Path(__file__).parent / "models.py").read_text()


def test_records_sql_and_call_site_of_each_statement() -> None:
    engine = create_engine("sqlite://")
    with engine.connect() as conn, record_queries() as log:
        conn.execute(text("select 1"))
        conn.execute(text("select 2"))  # second
    assert isinstance(log, QueryLog)
    assert log.count == 2
    first, second = log.queries
    assert (first.sql, second.sql) == ("select 1", "select 2")
    assert first.relationship is None
    assert isinstance(second.callsite, CallSite)
    assert second.callsite.filename == __file__
    assert second.callsite.function == (
        "test_records_sql_and_call_site_of_each_statement"
    )
    source = Path(__file__).read_text().splitlines()
    assert source[second.callsite.lineno - 1].endswith("# second")


def test_lazy_loads_carry_the_relationship() -> None:
    session = make_sync_session()
    with record_queries() as log:
        orders = session.query(Order).order_by(Order.id).all()
        names = [order.customer.name for order in orders]
    assert len(names) == 6
    relationships = [query.relationship for query in log.queries]
    assert relationships == [None, *["Order.customer"] * 3]
    assert "customers" in log.queries[1].sql


def test_selectinload_queries_carry_the_relationship() -> None:
    from sqlalchemy.orm import selectinload

    session = make_sync_session()
    with record_queries() as log:
        session.query(Order).options(selectinload(Order.customer)).all()
    assert [query.relationship for query in log.queries] == [None, "Order.customer"]
    log.assert_no_nplusone()


def test_count_is_live_inside_the_block_and_fixed_after_it() -> None:
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        with record_queries() as log:
            assert log.count == 0
            conn.execute(text("select 1"))
            assert log.count == 1
        conn.execute(text("select 2"))
    assert log.count == 1
    assert [query.sql for query in log.queries] == ["select 1"]


def test_a_late_append_from_another_thread_does_not_change_a_finished_log() -> None:
    # A listener that captured the collector before the block ended can still
    # append after it; the finished log must stay consistent.
    engine = create_engine("sqlite://")
    with engine.connect() as conn, record_queries() as log:
        conn.execute(text("select 1"))
    late = log._collector.statements[0]
    log._collector.statements.append(late)
    assert log.count == len(log) == len(list(log)) == len(log.queries) == 1


def test_queries_from_other_threads_are_recorded() -> None:
    engine = create_engine("sqlite://")

    def worker() -> None:
        with engine.connect() as conn:
            conn.execute(text("select 1"))

    with record_queries() as log:
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
    assert [query.sql for query in log.queries] == ["select 1"]


def test_records_inside_max_queries_and_both_count() -> None:
    engine = create_engine("sqlite://")
    with (
        engine.connect() as conn,
        max_queries(2) as budget_log,
        record_queries() as log,
    ):
        conn.execute(text("select 1"))
        conn.execute(text("select 2"))
    assert log.count == 2
    assert [q.sql for q in budget_log.queries] == [q.sql for q in log.queries]


def test_nested_logs_see_only_their_own_block() -> None:
    engine = create_engine("sqlite://")
    with engine.connect() as conn, record_queries() as outer:
        conn.execute(text("select 1"))
        with record_queries() as inner:
            conn.execute(text("select 2"))
    assert (outer.count, inner.count) == (2, 1)


def test_a_finished_log_builds_its_queries_once() -> None:
    engine = create_engine("sqlite://")
    with engine.connect() as conn, record_queries() as log:
        for value in range(3):
            conn.execute(text("select :v"), {"v": value})
    assert log.queries is log.queries
    assert log[0] is log.queries[0]


def test_a_log_is_sized_iterable_and_indexable() -> None:
    engine = create_engine("sqlite://")
    with engine.connect() as conn, record_queries() as log:
        conn.execute(text("select 1"))
        assert len(log) == 1
        assert log[0].sql == "select 1"  # live inside the block
        conn.execute(text("select 2"))
        conn.execute(text("select 3"))
        assert log[-1].sql == "select 3"
        assert log[1:] == (log[1], log[2])
    assert len(log) == log.count == 3
    assert [query.sql for query in log] == ["select 1", "select 2", "select 3"]
    assert log[-1].sql == "select 3"
    assert [query.sql for query in log[:2]] == ["select 1", "select 2"]
    with pytest.raises(IndexError):
        log[3]
    with pytest.raises(TypeError):
        log["sql"]  # type: ignore[call-overload]


def test_an_empty_log_is_falsy_like_any_sized_container() -> None:
    with record_queries() as log:
        pass
    assert len(log) == 0
    assert not log
    assert list(log) == []


def test_a_log_is_not_a_collections_abc_sequence() -> None:
    # count is a property, not Sequence.count(value); the docs say so.
    with record_queries() as log:
        pass
    assert not isinstance(log, Sequence)


def test_query_and_call_site_are_frozen_values() -> None:
    site = CallSite(filename="/app/orders.py", lineno=9, function="list_orders")
    query = Query(sql="select 1", callsite=site, relationship=None)
    assert query == Query(
        sql="select 1",
        callsite=CallSite(filename="/app/orders.py", lineno=9, function="list_orders"),
        relationship=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        query.sql = "select 2"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        site.lineno = 10  # type: ignore[misc]


def test_query_and_call_site_take_keyword_arguments_only() -> None:
    # A field added later must not break callers, so positional use is refused.
    with pytest.raises(TypeError):
        CallSite("/app/orders.py", 9, "list_orders")  # type: ignore[misc]
    with pytest.raises(TypeError):
        Query("select 1", None, None)  # type: ignore[misc]


def test_repr_shows_the_count() -> None:
    with record_queries() as log:
        pass
    assert repr(log) == "<QueryLog 0 queries>"


# --------------------------------------------------------- assert_no_nplusone


def test_assert_no_nplusone_raises_with_the_plugin_message() -> None:
    session = make_sync_session()
    with record_queries() as log:
        for order in session.query(Order).all():
            _ = order.customer.name
    with pytest.raises(NPlusOneDetected) as info:
        log.assert_no_nplusone()
    assert isinstance(info.value, AssertionError)
    message = str(info.value)
    assert message.startswith("queryguard: N+1 detected\n\n")
    assert "  Order.customer was lazy-loaded 3 times\n" in message
    assert "tests/test_record_queries.py:" in message
    assert "select(Order).options(joinedload(Order.customer))" in message


def test_assert_no_nplusone_reports_repeated_statements() -> None:
    engine = create_engine("sqlite://")
    with engine.connect() as conn, record_queries() as log:
        for value in range(5):
            conn.execute(text("select :v"), {"v": value})
    with pytest.raises(NPlusOneDetected, match="the same statement ran 5 times"):
        log.assert_no_nplusone()


def test_assert_no_nplusone_takes_thresholds() -> None:
    session = make_sync_session()
    engine = create_engine("sqlite://")
    with engine.connect() as conn, record_queries() as log:
        for order in session.query(Order).all():
            _ = order.customer.name
        for value in range(5):
            conn.execute(text("select :v"), {"v": value})
    log.assert_no_nplusone(relationship_threshold=4, repeat_threshold=6)
    with pytest.raises(NPlusOneDetected, match="lazy-loaded 3 times"):
        log.assert_no_nplusone(relationship_threshold=3, repeat_threshold=6)


def test_assert_no_nplusone_passes_for_a_clean_block() -> None:
    engine = create_engine("sqlite://")
    with engine.connect() as conn, record_queries() as log:
        for value in range(4):
            conn.execute(text("select :v"), {"v": value})
    log.assert_no_nplusone()


@pytest.mark.parametrize("value", [1, 0, -3, "2", 2.0, True, None])
@pytest.mark.parametrize("name", ["relationship_threshold", "repeat_threshold"])
def test_assert_no_nplusone_rejects_invalid_thresholds(
    name: str, value: object
) -> None:
    with record_queries() as log:
        pass
    with pytest.raises(ValueError, match=f"{name} must be an integer >= 2"):
        log.assert_no_nplusone(**{name: value})  # type: ignore[arg-type]


# ------------------------------------------------------------ internal errors


@contextmanager
def _internal_error_state(error: Exception | None = None) -> Iterator[None]:
    """Set the plugin's recorded error for a moment, then restore the old one."""
    previous = listeners.swap_error(error)
    try:
        yield
    finally:
        listeners.swap_error(previous)


def _record_and_fail() -> QueryLog:
    """Record one statement; the plugin hits an internal error in the block."""
    engine = create_engine("sqlite://")
    with engine.connect() as conn, record_queries() as log:
        listeners.record_error(RuntimeError("boom"))
        conn.execute(text("select 1"))
    return log


def _record_nothing() -> None:
    with record_queries():
        pass


def test_an_error_recorded_in_the_block_warns_on_exit() -> None:
    with _internal_error_state():
        with pytest.warns(QueryGuardInternalWarning, match="may be incomplete"):
            log = _record_and_fail()
        assert log.count == 0


def test_an_error_recorded_before_the_block_also_warns() -> None:
    # The listeners stay off once an error is recorded, so the log is empty.
    earlier = _internal_error_state(RuntimeError("earlier"))
    with (
        earlier,
        pytest.warns(QueryGuardInternalWarning, match="RuntimeError.*earlier"),
    ):
        _record_nothing()


def test_a_block_left_by_an_exception_is_finished_and_does_not_warn() -> None:
    engine = create_engine("sqlite://")
    with _internal_error_state(), warnings.catch_warnings():
        warnings.simplefilter("error")  # a warning would replace the ValueError
        with (
            pytest.raises(ValueError, match="stop"),
            engine.connect() as conn,
            record_queries() as log,
        ):
            conn.execute(text("select 1"))
            listeners.record_error(RuntimeError("boom"))
            raise ValueError("stop")
        listeners.swap_error(None)  # the state at the end of the block is kept
        with pytest.raises(RuntimeError, match="may be incomplete.*boom"):
            log.assert_no_nplusone()
        log._collector.statements.append(log._collector.statements[0])
        assert len(log) == len(list(log)) == 1


def test_no_warning_without_an_internal_error() -> None:
    engine = create_engine("sqlite://")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with _internal_error_state(), engine.connect() as conn:
            with record_queries() as log:
                conn.execute(text("select 1"))
            log.assert_no_nplusone()


def test_assert_no_nplusone_refuses_a_log_that_may_be_incomplete() -> None:
    with _internal_error_state():
        with pytest.warns(QueryGuardInternalWarning):
            log = _record_and_fail()
        with pytest.raises(RuntimeError, match="may be incomplete.*boom"):
            log.assert_no_nplusone()


def test_a_finished_log_is_not_affected_by_a_later_error() -> None:
    with _internal_error_state():
        with record_queries() as log:
            pass
        listeners.record_error(RuntimeError("later"))
        log.assert_no_nplusone()


def test_assert_no_nplusone_inside_the_block_sees_the_error_too() -> None:
    with _internal_error_state(), warnings.catch_warnings():
        warnings.simplefilter("ignore", QueryGuardInternalWarning)
        with record_queries() as log:
            listeners.record_error(RuntimeError("boom"))
            with pytest.raises(RuntimeError, match="may be incomplete"):
                log.assert_no_nplusone()


def test_the_warning_is_a_public_user_warning() -> None:
    assert issubclass(QueryGuardInternalWarning, UserWarning)


def test_outside_pytest_an_internal_error_is_not_silent(tmp_path: Path) -> None:
    script = tmp_path / "script.py"
    script.write_text(
        textwrap.dedent(
            """
            import warnings
            from sqlalchemy import create_engine, text
            from pytest_queryguard import QueryGuardInternalWarning, listeners
            from pytest_queryguard import record_queries

            listeners.record_error(RuntimeError("boom"))
            engine = create_engine("sqlite://")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                with engine.connect() as conn, record_queries() as log:
                    conn.execute(text("select 1"))
            print(log.count, [w.category.__name__ for w in caught])
            try:
                log.assert_no_nplusone()
            except RuntimeError as exc:
                print("raised", "may be incomplete" in str(exc))
            """
        )
    )
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, check=True
    )
    assert result.stdout.splitlines() == [
        "0 ['QueryGuardInternalWarning']",
        "raised True",
    ]


@pytest.mark.parametrize("mode", ["off", "fail"])
def test_in_a_pytest_run_an_internal_error_is_not_silent(
    pytester: pytest.Pytester, mode: str
) -> None:
    pytester.makepyfile(
        test_a="""
        import pytest
        from sqlalchemy import create_engine, text
        from pytest_queryguard import QueryGuardInternalWarning, listeners
        from pytest_queryguard import record_queries


        def test_log():
            listeners.record_error(RuntimeError("boom"))
            engine = create_engine("sqlite://")
            with pytest.warns(QueryGuardInternalWarning, match="may be incomplete"):
                with engine.connect() as conn, record_queries() as log:
                    conn.execute(text("select 1"))
            with pytest.raises(RuntimeError, match="may be incomplete"):
                log.assert_no_nplusone()
        """
    )
    result = pytester.runpytest(f"--queryguard-mode={mode}")
    result.assert_outcomes(passed=1)


# ------------------------------------------------------------- in a pytest run


def test_recording_leaves_the_plugin_checks_alone(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(qgmodels=MODELS)
    pytester.makepyfile(
        test_a="""
        import pytest
        from pytest_queryguard import record_queries
        from qgmodels import Order, make_sync_session


        @pytest.fixture
        def session():
            return make_sync_session()


        @pytest.mark.max_queries(2)
        def test_orders(session):
            with record_queries() as log:
                for order in session.query(Order).all():
                    _ = order.customer.name
            assert log.count == 4
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*queryguard: expected at most 2 queries, got 4",
            "*queryguard: N+1 detected",
        ]
    )


def test_assert_no_nplusone_fails_a_test_with_the_plugin_off(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(qgmodels=MODELS)
    pytester.makepyfile(
        test_a="""
        import pytest
        from pytest_queryguard import record_queries
        from qgmodels import Order, make_sync_session


        @pytest.mark.allow_nplusone
        def test_orders():
            session = make_sync_session()
            with record_queries() as log:
                for order in session.query(Order).all():
                    _ = order.customer.name
            log.assert_no_nplusone()
        """
    )
    result = pytester.runpytest("--queryguard-mode=off")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["E   *NPlusOneDetected: queryguard: N+1 detected*"])


def test_paths_in_the_message_are_relative_to_the_pytest_rootdir(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(qgmodels=MODELS)
    pytester.makepyfile(
        test_a="""
        import os
        import pytest
        from pytest_queryguard import NPlusOneDetected, record_queries
        from qgmodels import Order, make_sync_session


        @pytest.mark.allow_nplusone
        def test_orders(request, tmp_path):
            session = make_sync_session()
            with record_queries() as log:
                for order in session.query(Order).all():
                    _ = order.customer.name
            os.chdir(tmp_path)  # the rootdir is not the working directory
            with pytest.raises(NPlusOneDetected) as info:
                log.assert_no_nplusone()
            assert "  first at test_a.py:" in str(info.value), str(info.value)
        """
    )
    result = pytester.runpytest("--queryguard-mode=off")
    result.assert_outcomes(passed=1)


def test_budget_failures_use_the_same_path_base_as_the_nplusone_message(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        test_a="""
        import os
        import pytest
        from sqlalchemy import create_engine, text
        from pytest_queryguard import QueryBudgetExceeded, max_queries


        def test_budget(tmp_path):
            engine = create_engine("sqlite://")
            os.chdir(tmp_path)  # the rootdir is not the working directory
            with engine.connect() as conn:
                with pytest.raises(QueryBudgetExceeded) as info:
                    with max_queries(0):
                        conn.execute(text("select 1"))
            assert "(test_a.py:" in str(info.value), str(info.value)
        """
    )
    result = pytester.runpytest("--queryguard-mode=off")
    result.assert_outcomes(passed=1)


def test_outside_pytest_paths_are_relative_to_the_working_directory(
    tmp_path: Path,
) -> None:
    script = tmp_path / "script.py"
    script.write_text(
        "from sqlalchemy import create_engine, text\n"
        "from pytest_queryguard import NPlusOneDetected, record_queries\n"
        "engine = create_engine('sqlite://')\n"
        "with engine.connect() as conn, record_queries() as log:\n"
        "    for value in range(5):\n"
        "        conn.execute(text('select :v'), {'v': value})\n"
        "try:\n"
        "    log.assert_no_nplusone()\n"
        "except NPlusOneDetected as exc:\n"
        "    print([line for line in str(exc).splitlines() if 'first at' in line])\n"
    )
    result = subprocess.run(
        [sys.executable, "script.py"],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
    )
    assert result.stdout.strip() == "['  first at script.py:6 in <module>']"


def test_record_queries_works_without_pytest(tmp_path: Path) -> None:
    script = tmp_path / "script.py"
    script.write_text(
        textwrap.dedent(
            """
            from sqlalchemy import create_engine, text
            from pytest_queryguard import record_queries

            engine = create_engine("sqlite://")
            with engine.connect() as conn, record_queries() as log:
                conn.execute(text("select 1"))
            query = log.queries[0]
            print(log.count, query.sql, query.callsite.function)
            """
        )
    )
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "1 select 1 <module>"


# ------------------------------------------------------------- public surface


def test_public_names() -> None:
    assert sorted(pytest_queryguard.__all__) == [
        "CallSite",
        "NPlusOneDetected",
        "Query",
        "QueryBudgetExceeded",
        "QueryGuardDeprecationWarning",
        "QueryGuardFixture",
        "QueryGuardInternalWarning",
        "QueryGuardWarning",
        "QueryLog",
        "__version__",
        "ignore",
        "max_db_time",
        "max_queries",
        "record_queries",
    ]
    for name in pytest_queryguard.__all__:
        assert hasattr(pytest_queryguard, name)


def test_public_api_type_checks(tmp_path: Path) -> None:
    mypy_api = pytest.importorskip("mypy.api")
    snippet = tmp_path / "use_api.py"
    snippet.write_text(
        textwrap.dedent(
            """
            from pytest_queryguard import CallSite, Query, QueryLog, record_queries

            with record_queries() as log:
                pass
            reveal_type(log)
            reveal_type(log.count)
            reveal_type(log.queries)
            query: Query = log.queries[0]
            reveal_type(query.callsite)
            reveal_type(query.relationship)
            site: CallSite | None = query.callsite
            if site is not None:
                reveal_type(site.lineno)
            log.assert_no_nplusone(relationship_threshold=3)
            log.count = 3
            """
        )
    )
    stdout, _, status = mypy_api.run(
        ["--strict", "--no-incremental", "--no-error-summary", str(snippet)]
    )
    # Older mypy versions print "builtins.int" where newer ones print "int".
    notes = [
        line.split(": ", 1)[1].replace("builtins.", "") for line in stdout.splitlines()
    ]
    assert notes == [
        'note: Revealed type is "pytest_queryguard.api.QueryLog"',
        'note: Revealed type is "int"',
        'note: Revealed type is "tuple[pytest_queryguard.api.Query, ...]"',
        'note: Revealed type is "pytest_queryguard.events.CallSite | None"',
        'note: Revealed type is "str | None"',
        'note: Revealed type is "int"',
        'error: Property "count" defined in "QueryLog" is read-only  [misc]',
    ]
    assert status == 1
