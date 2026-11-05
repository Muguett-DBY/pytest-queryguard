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
                "Order.customer was lazy-loaded for 2 parents that one query loaded*"
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
    # objects of two different queries: no raise at the access; the usual
    # report at the end still counts the two lazy loads of Order.customer
    result.assert_outcomes(passed=2, failed=1)
    assert "queryguard strict" not in result.stdout.str()
    result.stdout.fnmatch_lines(
        ["FAILED test_strict_suite.py::test_two_queries_one_object_each - Failed:*"]
    )


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
    session = async_sessionmaker(engine)()
    session.add_all([Order(id=n, customer=Customer(id=n)) for n in range(3)])
    await session.commit()
    session.expunge_all()
    for order in (await session.scalars(select(Order))).all():
        await order.awaitable_attrs.customer  # async access


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
