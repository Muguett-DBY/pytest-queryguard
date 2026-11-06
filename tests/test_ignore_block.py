"""``with pytest_queryguard.ignore(...)``: leave out the findings of a block (#234)."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

from pytest_queryguard import blocks, ignore, record_queries

from .models import Order, make_sync_session

# The in-process tests enter blocks that leave nothing out; the bare marker
# keeps them out of this run's own stale-block report.
pytestmark = pytest.mark.allow_nplusone

MODELS = (Path(__file__).parent / "models.py").read_text()

HEADER = """
import asyncio
import contextvars
import threading

import pytest
from pytest_queryguard import ignore, record_queries
from qgmodels import Order, make_sync_session


def loop(session):
    for order in session.query(Order).all():
        _ = order.customer.name
"""


def _project(pytester: pytest.Pytester, body: str) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS, test_suite=HEADER + body)
    return pytester


def _run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest_subprocess("-p", "no:cacheprovider", *args)


# --- the block in a test ---


def test_a_finding_inside_the_block_is_left_out(pytester: pytest.Pytester) -> None:
    _project(
        pytester,
        """
def test_inside():
    session = make_sync_session()
    with ignore("relationship-load", reason="legacy report"):
        loop(session)
""",
    )
    result = _run(pytester)
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_a_block_for_another_kind_leaves_it_in_and_is_stale(
    pytester: pytest.Pytester,
) -> None:
    _project(
        pytester,
        """
def test_other_kind():
    session = make_sync_session()
    with ignore("repeated-statement"):
        loop(session)
""",
    )
    result = _run(pytester)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*kind: relationship-load  label: Order.customer*",
            "*queryguard: stale ignores*",
            "ignore() blocks that left out no finding in this run:",
            "  test_suite.py:*: ignore('repeated-statement')",
        ]
    )


def test_patterns_match_kinds(pytester: pytest.Pytester) -> None:
    _project(
        pytester,
        """
def test_star():
    with ignore("*"):
        loop(make_sync_session())


def test_glob():
    with ignore("relationship-*"):
        loop(make_sync_session())
""",
    )
    _run(pytester).assert_outcomes(passed=2)


def test_a_finding_partly_outside_the_block_is_reported(
    pytester: pytest.Pytester,
) -> None:
    # Two of the six lazy loads run outside the block: the finding counts
    # all six, and the block left nothing out.
    _project(
        pytester,
        """
def test_partly():
    session = make_sync_session()
    orders = session.query(Order).all()
    for order in orders[:2]:
        _ = order.customer.name
    with ignore("relationship-load"):
        for order in orders[2:]:
            _ = order.customer.name
""",
    )
    result = _run(pytester)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*Order.customer*lazy-loaded*",
            "*kind: relationship-load  label: Order.customer*",
            "  test_suite.py:*: ignore('relationship-load')",
        ]
    )


def test_findings_after_the_block_and_after_an_exception_count(
    pytester: pytest.Pytester,
) -> None:
    _project(
        pytester,
        """
def test_after():
    session = make_sync_session()
    with pytest.raises(KeyError):
        with ignore("relationship-load"):
            raise KeyError
    loop(session)
""",
    )
    result = _run(pytester)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*kind: relationship-load  label: Order.customer*"])


def test_nested_blocks(pytester: pytest.Pytester) -> None:
    # The inner block is for another kind: the outer one leaves the finding
    # out, and the inner one is stale.
    _project(
        pytester,
        """
def test_nested():
    session = make_sync_session()
    with ignore("relationship-load"):
        with ignore("column-load"):
            loop(session)
""",
    )
    result = _run(pytester)
    result.assert_outcomes(passed=1)
    lines = [line for line in result.stdout.lines if "ignore('" in line]
    assert len(lines) == 1, result.stdout.str()
    assert "ignore('column-load')" in lines[0]


def test_as_a_decorator(pytester: pytest.Pytester) -> None:
    _project(
        pytester,
        """
@ignore("relationship-load")
def legacy(session):
    loop(session)


def test_decorated():
    legacy(make_sync_session())


def test_decorated_twice_is_still_one_block():
    legacy(make_sync_session())
""",
    )
    result = _run(pytester)
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_a_block_used_in_one_test_is_not_stale(pytester: pytest.Pytester) -> None:
    _project(
        pytester,
        """
def helper(session, n_plus_one):
    with ignore("relationship-load"):
        if n_plus_one:
            loop(session)


def test_used():
    helper(make_sync_session(), True)


def test_not_used():
    helper(make_sync_session(), False)
""",
    )
    result = _run(pytester)
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_a_block_that_never_ran_in_a_measured_test_is_not_listed(
    pytester: pytest.Pytester,
) -> None:
    _project(
        pytester,
        """
@pytest.mark.allow_nplusone
def test_marked():
    with ignore("relationship-load"):
        pass


def test_off_the_test():
    pass


if False:
    with ignore("relationship-load"):
        pass
""",
    )
    result = _run(pytester)
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_no_stale_report_with_measurement_off(pytester: pytest.Pytester) -> None:
    _project(
        pytester,
        """
def test_block():
    with ignore("relationship-load"):
        pass
""",
    )
    result = _run(pytester, "--queryguard-mode=off")
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_an_ignore_pattern_that_a_block_also_covers_is_not_stale(
    pytester: pytest.Pytester,
) -> None:
    _project(
        pytester,
        """
@pytest.mark.allow_nplusone("Order.customer")
def test_both_marker():
    with ignore("relationship-load"):
        loop(make_sync_session())


def test_both_ini():
    with ignore("relationship-load"):
        loop(make_sync_session())
""",
    )
    pytester.makeini(
        """
        [pytest]
        queryguard_ignore = relationship-load Order.customer function=loop
        """
    )
    result = _run(pytester)
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_past_the_event_cap_a_block_leaves_nothing_out(
    pytester: pytest.Pytester,
) -> None:
    # Unstored events may have run outside the block: the finding stays, and
    # the block is not called stale either.
    _project(
        pytester,
        """
def test_capped():
    session = make_sync_session()
    orders = session.query(Order).all()
    with ignore("relationship-load"):
        for order in orders:
            _ = order.customer.name
""",
    )
    pytester.makeini("[pytest]\nqueryguard_max_events = 2\n")
    result = _run(pytester)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*kind: relationship-load  label: Order.customer*"])
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_a_block_for_a_kind_not_checked_is_not_stale(
    pytester: pytest.Pytester,
) -> None:
    _project(
        pytester,
        """
def test_off_kinds():
    with ignore("write-loop", "cartesian-load", "missing-index"):
        pass
""",
    )
    result = _run(pytester, "-o", "queryguard_cartesian_load=off")
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_only_blocks_in_the_test_context_are_noted(
    pytester: pytest.Pytester,
) -> None:
    # A thread without the test's context (a leftover worker, say) is not the
    # test's; one given a copy of the context is.
    _project(
        pytester,
        """
def enter(kind):
    with ignore(kind):
        pass


def test_threads():
    plain = threading.Thread(target=enter, args=("repeated-statement",))
    plain.start()
    plain.join()
    context = contextvars.copy_context()
    given = threading.Thread(target=context.run, args=(enter, "column-load"))
    given.start()
    given.join()
""",
    )
    result = _run(pytester)
    result.assert_outcomes(passed=1)
    text = result.stdout.str()
    assert "ignore('column-load')" in text
    assert "ignore('repeated-statement')" not in text


# --- threads and tasks ---


def test_a_thread_started_inside_the_block_is_not_covered(
    pytester: pytest.Pytester,
) -> None:
    if getattr(sys.flags, "thread_inherit_context", False):
        pytest.skip("threads inherit the context on this interpreter")
    _project(
        pytester,
        """
def test_thread():
    session = make_sync_session()
    with ignore("relationship-load"):
        worker = threading.Thread(target=loop, args=(session,))
        worker.start()
        worker.join()
""",
    )
    result = _run(pytester)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*kind: relationship-load  label: Order.customer*"])


def test_a_thread_given_the_context_is_covered(pytester: pytest.Pytester) -> None:
    _project(
        pytester,
        """
def test_thread_with_context():
    session = make_sync_session()
    with ignore("relationship-load"):
        context = contextvars.copy_context()
        worker = threading.Thread(target=context.run, args=(loop, session))
        worker.start()
        worker.join()
""",
    )
    _run(pytester).assert_outcomes(passed=1)


def test_tasks_created_inside_the_block_are_covered(pytester: pytest.Pytester) -> None:
    _project(
        pytester,
        """
async def run(session):
    loop(session)


def test_task():
    session = make_sync_session()
    with ignore("relationship-load"):
        asyncio.run(run(session))


def test_to_thread():
    session = make_sync_session()

    async def main():
        with ignore("relationship-load"):
            await asyncio.to_thread(loop, session)

    asyncio.run(main())
""",
    )
    _run(pytester).assert_outcomes(passed=2)


def test_the_block_of_one_task_does_not_cover_another() -> None:
    seen: dict[str, tuple[object, ...]] = {}

    async def inside(started: asyncio.Event, done: asyncio.Event) -> None:
        with ignore("relationship-load"):
            started.set()
            await done.wait()
            seen["inside"] = blocks.current()

    async def outside(started: asyncio.Event, done: asyncio.Event) -> None:
        await started.wait()
        seen["outside"] = blocks.current()
        done.set()

    async def main() -> None:
        started, done = asyncio.Event(), asyncio.Event()
        await asyncio.gather(inside(started, done), outside(started, done))

    asyncio.run(main())
    assert len(seen["inside"]) == 1
    assert seen["outside"] == ()
    assert blocks.current() == ()


def test_a_decorated_coroutine_runs_in_its_block_in_each_task() -> None:
    @ignore("repeated-statement")
    async def job() -> tuple[object, ...]:
        await asyncio.sleep(0)
        return blocks.current()

    async def main() -> list[tuple[object, ...]]:
        return list(await asyncio.gather(job(), job(), job()))

    results = asyncio.run(main())
    assert all(len(found) == 1 for found in results)
    assert blocks.current() == ()


def test_a_thread_does_not_see_the_block() -> None:
    seen: list[tuple[object, ...]] = []
    with ignore("relationship-load"):
        inside = blocks.current()
        worker = threading.Thread(target=lambda: seen.append(blocks.current()))
        worker.start()
        worker.join()
    assert len(inside) == 1
    inherits = getattr(sys.flags, "thread_inherit_context", False)
    assert seen == ([inside] if inherits else [()])


# --- strict mode ---

STRICT = """
def test_strict_inside():
    with ignore("relationship-load"):
        loop(make_sync_session())


def test_strict_partly():
    session = make_sync_session()
    orders = session.query(Order).all()
    with ignore("relationship-load"):
        _ = orders[0].customer.name
    _ = orders[2].customer.name  # the access (orders 0 and 1 share a customer)
"""


def test_strict_mode_does_not_raise_inside_the_block(
    pytester: pytest.Pytester,
) -> None:
    _project(pytester, STRICT)
    result = _run(pytester, "--queryguard-strict")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        [
            "*_ = orders?2?.customer.name  # the access*",
            "*queryguard strict: Order.customer was lazy-loaded for 2 parents*",
        ]
    )
    assert "test_strict_inside" not in "".join(
        line for line in result.stdout.lines if line.startswith("FAILED")
    )


# --- record_queries() ---


def test_assert_no_nplusone_leaves_out_the_block() -> None:
    session = make_sync_session()
    with record_queries() as log, ignore("relationship-load"):
        for order in session.query(Order).all():
            _ = order.customer.name
    log.assert_no_nplusone()


def test_a_block_used_only_by_assert_no_nplusone_is_not_stale(
    pytester: pytest.Pytester,
) -> None:
    # The plugin's threshold is too high to find the N+1, so only the
    # assert_no_nplusone() call leaves out a finding with the block (#284).
    _project(
        pytester,
        """
def test_log():
    session = make_sync_session()
    with record_queries() as log, ignore("relationship-load"):
        loop(session)
    log.assert_no_nplusone(relationship_threshold=2)


def test_log_in_a_thread():
    # a check run in a copy of the test's context counts too
    session = make_sync_session()
    with record_queries() as log, ignore("repeated-statement"):
        for _ in range(3):
            session.query(Order).filter(Order.id == 1).all()
    context = contextvars.copy_context()
    thread = threading.Thread(
        target=context.run,
        args=(lambda: log.assert_no_nplusone(repeat_threshold=2),),
    )
    thread.start()
    thread.join()
""",
    )
    pytester.makeini(
        """
[pytest]
queryguard_relationship_threshold = 50
queryguard_repeat_threshold = 50
"""
    )
    result = _run(pytester)
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_a_block_used_in_a_test_with_a_bare_marker_is_not_stale(
    pytester: pytest.Pytester,
) -> None:
    _project(
        pytester,
        """
def helper(session, n_plus_one):
    with ignore("relationship-load"):
        if n_plus_one:
            loop(session)


@pytest.mark.allow_nplusone
def test_marked():
    session = make_sync_session()
    with record_queries() as log:
        helper(session, True)
    log.assert_no_nplusone(relationship_threshold=2)


def test_not_used():
    helper(make_sync_session(), False)
""",
    )
    result = _run(pytester)
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_a_block_assert_no_nplusone_did_not_use_is_still_stale(
    pytester: pytest.Pytester,
) -> None:
    _project(
        pytester,
        """
def test_log():
    session = make_sync_session()
    with record_queries() as log, ignore("relationship-load"):
        loop(session)
    log.assert_no_nplusone(relationship_threshold=2)
    with ignore("relationship-load"):
        session.query(Order).all()
""",
    )
    pytester.makeini(
        """
[pytest]
queryguard_relationship_threshold = 50
"""
    )
    result = _run(pytester)
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        [
            "*queryguard: stale ignores*",
            "ignore() blocks that left out no finding in this run:",
            "  test_suite.py:19: ignore('relationship-load')",
        ]
    )
    result.stdout.no_fnmatch_line("*test_suite.py:16:*")


# --- other kinds ---


def test_repeated_statements_and_column_loads(pytester: pytest.Pytester) -> None:
    _project(
        pytester,
        """
from sqlalchemy import text


def test_repeat():
    session = make_sync_session()
    with ignore("repeated-statement"):
        for _ in range(6):
            session.execute(text("SELECT 1")).all()


def test_column_load():
    session = make_sync_session()
    orders = session.query(Order).all()
    session.expire_all()
    with ignore("column-load"):
        for order in orders:
            _ = order.customer_id
""",
    )
    result = _run(pytester)
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_a_statement_run_again_outside_the_block_is_reported(
    pytester: pytest.Pytester,
) -> None:
    # The index check explains a statement at its first run only; the later
    # run outside the block must still count.
    _project(
        pytester,
        """
def test_scan_again():
    session = make_sync_session()
    with ignore("missing-index"):
        session.query(Order).filter(Order.customer_id == 1).count()
    session.query(Order).filter(Order.customer_id == 1).count()


def test_scan_twice_inside():
    session = make_sync_session()
    with ignore("missing-index"):
        session.query(Order).filter(Order.customer_id == 1).count()
    with ignore("missing-index"):
        session.query(Order).filter(Order.customer_id == 1).count()
""",
    )
    result = _run(pytester, "--queryguard-index-check")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["FAILED*test_scan_again*"])


def test_a_run_outside_the_block_before_ddl_still_counts(
    pytester: pytest.Pytester,
) -> None:
    # DDL makes the index check explain a statement again; the runs made
    # before it outside the block must still count (#284).
    _project(
        pytester,
        """
from sqlalchemy import text


def scan(session):
    session.query(Order).filter(Order.customer_id == 1).count()


def ddl(session):
    session.execute(text("CREATE TABLE IF NOT EXISTS qg_extra (id INTEGER)"))


def test_outside_then_ddl_then_inside():
    session = make_sync_session()
    scan(session)
    ddl(session)
    with ignore("missing-index"):
        scan(session)


def test_inside_outside_then_ddl_then_inside():
    session = make_sync_session()
    with ignore("missing-index"):
        scan(session)
    scan(session)
    ddl(session)
    with ignore("missing-index"):
        scan(session)


def test_inside_then_ddl_then_inside():
    session = make_sync_session()
    with ignore("missing-index"):
        scan(session)
    ddl(session)
    with ignore("missing-index"):
        scan(session)


def test_inside_then_an_index_then_outside():
    # the runs after the index do not scan: only the run inside did
    session = make_sync_session()
    with ignore("missing-index"):
        scan(session)
    session.execute(text("CREATE INDEX ix_orders_customer ON orders (customer_id)"))
    for _ in range(3):
        scan(session)
""",
    )
    result = _run(pytester, "--queryguard-index-check")
    result.assert_outcomes(passed=2, failed=2)
    result.stdout.fnmatch_lines(
        [
            "FAILED*test_outside_then_ddl_then_inside*",
            "FAILED*test_inside_outside_then_ddl_then_inside*",
        ],
        consecutive=False,
    )


def test_missing_index(pytester: pytest.Pytester) -> None:
    _project(
        pytester,
        """
def test_scan():
    session = make_sync_session()
    with ignore("missing-index"):
        session.query(Order).filter(Order.customer_id == 1).count()


def test_scan_outside():
    session = make_sync_session()
    session.query(Order).filter(Order.customer_id == 2).count()
""",
    )
    result = _run(pytester, "--queryguard-index-check")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["FAILED*test_scan_outside*"])
    result.stdout.no_fnmatch_line("*stale ignores*")


# --- pytest-xdist ---


def test_stale_blocks_are_merged_over_xdist_workers(
    pytester: pytest.Pytester,
) -> None:
    pytest.importorskip("xdist")
    _project(
        pytester,
        """
def helper(session, n_plus_one):
    with ignore("relationship-load"):
        if n_plus_one:
            loop(session)


def test_used():
    helper(make_sync_session(), True)


def test_not_used():
    helper(make_sync_session(), False)


def test_stale():
    with ignore("column-load"):
        pass
""",
    )
    result = _run(pytester, "-n", "2")
    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(
        [
            "*queryguard: stale ignores*",
            "ignore() blocks that left out no finding in this run:",
            "  test_suite.py:*: ignore('column-load')",
        ]
    )
    assert "ignore('relationship-load')" not in result.stdout.str()


# --- the API ---


def test_kinds_are_checked() -> None:
    with pytest.raises(TypeError, match="at least one finding kind"):
        ignore()
    with pytest.raises(ValueError, match="matches no finding kind"):
        ignore("relationship")
    with pytest.raises(TypeError, match="non-empty string"):
        ignore(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="reason"):
        ignore("*", reason=1)  # type: ignore[arg-type]


def test_a_generator_function_cannot_be_decorated() -> None:
    def gen() -> object:
        yield 1

    with pytest.raises(TypeError, match="generator"):
        ignore("*")(gen)


def test_the_block_records_where_it_was_written() -> None:
    block = ignore("relationship-load")
    assert block.site is not None
    assert block.site.filename == __file__
    assert block.site.function == "test_the_block_records_where_it_was_written"
    assert block.describe() == "ignore('relationship-load')"


def test_one_object_entered_twice_nests() -> None:
    block = ignore("*")
    with block as entered:
        assert entered is block
        with block:
            assert blocks.current() == (block, block)
        assert blocks.current() == (block,)
    assert blocks.current() == ()
