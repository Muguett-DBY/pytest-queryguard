"""queryguard_strict: raise at the lazy load that makes the N+1 (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

MODELS = (Path(__file__).parent / "models.py").read_text()

SUITE = """
import pytest
from qgmodels import Order, make_sync_session


def _helper_query(session):
    return session.query(Order).all()


def test_loop():
    session = make_sync_session()
    for order in _helper_query(session):
        name = order.customer.name  # the access


def test_one_object():
    session = make_sync_session()
    order = session.query(Order).first()
    _ = order.customer.name


def test_two_queries_one_object_each():
    session = make_sync_session()
    first = session.query(Order).order_by(Order.id).first()
    _ = first.customer.name
    second = session.query(Order).order_by(Order.id.desc()).first()
    _ = second.customer.name


@pytest.mark.allow_nplusone
def test_allowed():
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name


def test_caught_by_the_code_under_test():
    session = make_sync_session()
    try:
        for order in session.query(Order).all():
            _ = order.customer.name
    except AssertionError:
        pass
"""


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS, test_strict_suite=SUITE)
    return pytester


def _run(project: pytest.Pytester, *args: str) -> pytest.RunResult:
    return project.runpytest_subprocess(
        "-p", "no:cacheprovider", "-W", "default", *args
    )


def test_the_second_parent_raises_at_the_access(project: pytest.Pytester) -> None:
    result = _run(project, "--queryguard-strict", "-k", "loop")
    result.assert_outcomes(failed=1)
    text = result.stdout.str()
    result.stdout.fnmatch_lines(
        [
            ">*name = order.customer.name  # the access",
            (
                "E*pytest_queryguard.NPlusOneDetected: queryguard strict: "
                "Order.customer was lazy-loaded for 2 parents loaded at one line*"
            ),
            "E*parents loaded at test_strict_suite.py:* in _helper_query",
            "E*select(Order).options(joinedload(Order.customer))",
            # the traceback ends at the access: no SQLAlchemy frames below it
            "test_strict_suite.py:*: NPlusOneDetected",
        ]
    )
    assert "sqlalchemy/orm" not in text and "listeners.py" not in text


def test_single_objects_and_allowed_tests_do_not_raise(
    project: pytest.Pytester,
) -> None:
    result = _run(project, "--queryguard-strict", "-k", "one_object or allowed")
    # objects of two different queries, one each: no raise at the access, and
    # the report at the end, which groups the parents the same way, agrees
    # (#307)
    result.assert_outcomes(passed=3)
    assert "queryguard strict" not in result.stdout.str()


def test_an_ignore_pattern_or_the_baseline_lets_it_pass(
    project: pytest.Pytester,
) -> None:
    project.makeini(
        "[pytest]\nqueryguard_strict = true\n"
        "queryguard_ignore = relationship-load Order.customer\n"
    )
    _run(project, "-k", "loop").assert_outcomes(passed=1)
    project.makeini("[pytest]\nqueryguard_strict = true\n")
    # strict does not raise while the baseline is updated: the loop must run
    # to the end for its N+1 to be recorded
    _run(
        project,
        "-k",
        "loop",
        "--queryguard-baseline=b.json",
        "--queryguard-update-baseline",
    ).assert_outcomes(passed=1)
    _run(project, "-k", "loop", "--queryguard-baseline=b.json").assert_outcomes(
        passed=1
    )


def test_without_strict_the_report_comes_at_the_end(
    project: pytest.Pytester,
) -> None:
    result = _run(project, "-k", "loop")
    result.assert_outcomes(failed=1)
    assert "queryguard strict" not in result.stdout.str()
    result.stdout.fnmatch_lines(["*queryguard: N+1 detected*"])


def test_warn_mode_never_raises(project: pytest.Pytester) -> None:
    result = _run(
        project, "--queryguard-strict", "--queryguard-mode=warn", "-k", "loop"
    )
    result.assert_outcomes(passed=1)


def test_an_exception_caught_by_the_code_still_fails_the_test(
    project: pytest.Pytester,
) -> None:
    # the code under test swallowed the strict exception; the report at the
    # end still fails the test
    result = _run(project, "--queryguard-strict", "-k", "caught")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*queryguard: N+1 detected*"])


ENGINE_SIDE = """
from sqlalchemy import event, inspect
from qgmodels import Customer, Order, make_sync_session


def test_handle_error():
    session = make_sync_session()
    engine = session.get_bind()
    seen = []

    def replace(context):
        seen.append(type(context.original_exception).__name__)
        raise RuntimeError("replaced by handle_error")

    event.listen(engine, "handle_error", replace)
    try:
        for order in session.query(Order).all():
            _ = order.customer.name
    finally:
        print("HANDLE_ERROR SAW", seen)


def test_no_flush():
    session = make_sync_session()
    orders = session.query(Order).order_by(Order.id).all()
    _ = orders[0].customer.name
    new = Customer(id=99, name="pending")
    session.add(new)
    try:
        _ = orders[2].customer.name  # the second parent: raises
    finally:
        print("STILL PENDING", inspect(new).pending)
"""


def test_the_engine_never_sees_the_strict_error(pytester: pytest.Pytester) -> None:
    # Raised before the ORM takes a connection: a handle_error listener (which
    # could replace it or invalidate the connection) never gets it.
    pytester.makepyfile(qgmodels=MODELS, test_engine_side=ENGINE_SIDE)
    result = _run(pytester, "--queryguard-strict", "-k", "handle_error")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*NPlusOneDetected: queryguard strict*"])
    result.stdout.fnmatch_lines(["HANDLE_ERROR SAW []"])
    assert "RuntimeError: replaced by handle_error" not in result.stdout.str()


def test_the_strict_error_comes_before_the_autoflush(
    pytester: pytest.Pytester,
) -> None:
    # Pending objects stay pending: nothing is flushed for a SELECT that is
    # never sent (and a flush error cannot hide the N+1).
    pytester.makepyfile(qgmodels=MODELS, test_engine_side=ENGINE_SIDE)
    result = _run(pytester, "--queryguard-strict", "-k", "no_flush")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*NPlusOneDetected: queryguard strict*"])
    result.stdout.fnmatch_lines(["STILL PENDING True"])


ASYNC = """
import asyncio
from sqlalchemy import ForeignKey, select
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    customer: Mapped[Customer] = relationship()


async def main():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine)() as session:
            session.add_all([Order(id=n, customer=Customer(id=n)) for n in range(3)])
            await session.commit()
            session.expunge_all()
            for order in (await session.scalars(select(Order))).all():
                await order.awaitable_attrs.customer  # async access
    finally:
        # SQLAlchemy 2.0.13 with aiosqlite 0.22 leaves the connection's
        # thread running after an exception: the process would not exit
        await engine.dispose()


def test_async():
    asyncio.run(main())
"""


def test_async_access_raises_at_the_await(pytester: pytest.Pytester) -> None:
    pytest.importorskip("aiosqlite")
    pytest.importorskip("greenlet")
    pytester.makepyfile(test_async_suite=ASYNC)
    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "--queryguard-strict"
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*await order.awaitable_attrs.customer  # async access*",
            "*queryguard strict*",
        ]
    )


SPLIT = """
from qgmodels import Order, make_sync_session


def first_access(order):
    return order.customer.name


def render(orders):
    for order in orders[1:]:
        _ = order.customer.name


def test_split():
    session = make_sync_session()
    orders = session.query(Order).all()
    first_access(orders[0])
    render(orders)


def test_two_sessions():
    # one order per Session, the same helper line: two requests, not a loop
    for i in range(2):
        session = make_sync_session()
        order = session.query(Order).order_by(Order.id).offset(i).first()
        _ = order.customer.name
"""


@pytest.fixture
def split(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS, test_split=SPLIT)
    return pytester


def test_ignore_rules_match_the_first_lazy_load_as_in_the_report(
    split: pytest.Pytester,
) -> None:
    # the report locates the finding at first_access, where the first load is
    split.makeini(
        "[pytest]\nqueryguard_strict = true\n"
        "queryguard_ignore = relationship-load Order.customer function=first_access\n"
    )
    result = _run(split, "test_split.py::test_split")
    result.assert_outcomes(passed=1)
    # without the rule it raises
    split.makeini("[pytest]\nqueryguard_strict = true\n")
    _run(split, "test_split.py::test_split").assert_outcomes(failed=1)


def test_session_scope_counts_each_session_on_its_own(split: pytest.Pytester) -> None:
    split.makeini("[pytest]\nqueryguard_strict = true\nqueryguard_scope = session\n")
    _run(split, "-k", "two_sessions").assert_outcomes(passed=1)
    split.makeini("[pytest]\nqueryguard_strict = true\n")
    result = _run(split, "-k", "two_sessions")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*queryguard strict*"])


def test_the_strict_n_plus_one_is_in_sarif_and_json(project: pytest.Pytester) -> None:
    import json

    result = _run(
        project,
        "--queryguard-strict",
        "-k",
        "loop",
        "--queryguard-sarif=out.sarif",
        "--queryguard-json=q.json",
    )
    result.assert_outcomes(failed=1)
    sarif = json.loads((project.path / "out.sarif").read_text())
    (found,) = sarif["runs"][0]["results"]
    assert found["ruleId"] == "queryguard/relationship-load"
    tests = json.loads((project.path / "q.json").read_text())["tests"]
    (finding,) = tests["test_strict_suite.py::test_loop"]["findings"]
    assert finding["label"] == "Order.customer"


BUDGETS = """
import pytest
from sqlalchemy import text
from pytest_queryguard import max_queries
from qgmodels import Order, make_sync_session


def _loop(session):
    for order in session.query(Order).all():
        _ = order.customer.name  # the access


@pytest.mark.max_queries(1)
def test_marker_over():
    _loop(make_sync_session())


@pytest.mark.max_db_time(0)
def test_db_time_over():
    _loop(make_sync_session())


@pytest.mark.max_queries(100)
def test_marker_under():
    _loop(make_sync_session())


def test_fixture_over(queryguard):
    session = make_sync_session()
    with queryguard.max_queries(1):
        session.execute(text("select 1"))
        _loop(session)


def test_function_over():
    session = make_sync_session()
    with max_queries(1):
        session.execute(text("select 1"))
        _loop(session)


def test_function_under():
    session = make_sync_session()
    with max_queries(100):
        _loop(session)
"""


@pytest.fixture
def budgets(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS, test_budgets=BUDGETS)
    pytester.makeini("[pytest]\nqueryguard_strict = true\n")
    return pytester


@pytest.mark.parametrize("name", ["marker_over", "fixture_over", "function_over"])
def test_a_budget_gone_over_before_the_access_is_in_the_error(
    budgets: pytest.Pytester, name: str
) -> None:
    result = _run(budgets, "-k", name)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            ">*_ = order.customer.name  # the access",
            "E*NPlusOneDetected: queryguard strict: Order.customer*",
            "E*(queryguard_strict raises at the lazy load*",
            "E*queryguard: expected at most 1 query, got *",
        ]
    )
    # the budget part is in the error once, not also as a second failure;
    # the short summary (which shows the whole message when CI is set) is
    # left out
    failures = result.stdout.str().split("short test summary info")[0]
    assert failures.count("expected at most 1 query") == 1


def test_a_time_budget_gone_over_before_the_access_is_in_the_error(
    budgets: pytest.Pytester,
) -> None:
    result = _run(budgets, "-k", "db_time_over")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "E*NPlusOneDetected: queryguard strict: Order.customer*",
            "E*queryguard: database time * exceeded the budget of *",
        ]
    )


@pytest.mark.parametrize("name", ["marker_under", "function_under"])
def test_a_budget_not_gone_over_before_the_access_is_not_in_the_error(
    budgets: pytest.Pytester, name: str
) -> None:
    result = _run(budgets, "-k", name)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["E*NPlusOneDetected: queryguard strict:*"])
    assert "expected at most" not in result.stdout.str()


STOPPED_BLOCK = """
from sqlalchemy import select
from pytest_queryguard import ignore
from qgmodels import Customer, Order, make_sync_session


def test_block_for_a_later_finding():
    session = make_sync_session()
    with ignore("repeated-statement"):
        for order in session.query(Order).all():
            _ = order.customer.name  # strict raises here
        for number in range(1, 7):  # the repeat the block leaves out
            session.execute(select(Customer).where(Customer.id == number)).all()
"""


@pytest.mark.parametrize("strict", [False, True], ids=["report", "strict"])
def test_a_block_of_a_stopped_test_is_not_listed_as_stale(
    pytester: pytest.Pytester, strict: bool
) -> None:
    """The block is for the second loop, which a strict run never reaches."""
    pytester.makepyfile(qgmodels=MODELS, test_stopped_block=STOPPED_BLOCK)
    args = ["--queryguard-strict"] if strict else []
    result = _run(pytester, *args)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*kind: relationship-load  label: Order.customer"])
    result.stdout.no_fnmatch_line("*queryguard: stale ignores*")
    result.stdout.no_fnmatch_line("*ignore('repeated-statement')*")


BAD_MARKER = """
import pytest
from pytest_queryguard import collector
from qgmodels import Order, make_sync_session


@pytest.mark.allow_nplusone(1)
def test_a_bad_marker():
    pass


@pytest.mark.allow_nplusone
def test_b_allowed():
    assert len(collector.active()) == 1  # this test's own collector only
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name
"""


def test_a_bad_ignore_marker_leaves_no_strict_collector_behind(
    pytester: pytest.Pytester,
) -> None:
    # the bad marker fails its test before it runs; the collector of its
    # measurement must not stay on the stack for the rest of the session
    pytester.makepyfile(qgmodels=MODELS, test_bad_marker=BAD_MARKER)
    result = _run(pytester, "--queryguard-strict")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*allow_nplusone on *test_a_bad_marker takes*"])
