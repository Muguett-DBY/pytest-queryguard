"""``queryguard_scope``: count per test or per Session / connection checkout.

The unit tests build collectors by hand; the pytester tests run the plugin
against real SQLAlchemy sessions, connections, async sessions and xdist workers.
``queryguard_repeat_scope`` / ``repeat_scope=`` are the deprecated names.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from pytest_queryguard import (
    NPlusOneDetected,
    QueryGuardDeprecationWarning,
    record_queries,
)
from pytest_queryguard.collector import QueryCollector
from pytest_queryguard.detectors import (
    ColumnLoadFinding,
    RelationshipFinding,
    RepeatFinding,
    WriteLoopFinding,
    detect,
)
from pytest_queryguard.events import (
    CAUSE_EXPIRED,
    CallSite,
    ColumnLoadEvent,
    RelationshipLoadEvent,
    StatementEvent,
)
from pytest_queryguard.listeners import scope_of_connection, scope_of_session
from pytest_queryguard.messages import make_findings
from tests.models import Base

# pytester runs sessions in-process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone

SITE = CallSite(filename="app.py", lineno=10, function="handler")
BY_ID = "SELECT * FROM orgs WHERE id = ?"
MODELS = (Path(__file__).parent / "models.py").read_text()


def _collect(**kwargs: object) -> QueryCollector:
    c = QueryCollector()
    for sql, scope in kwargs.get("statements", []):  # type: ignore[attr-defined]
        c.statements.append(StatementEvent(sql, SITE, None, scope=scope))
    return c


def _detect(c: QueryCollector, scope: str, **kwargs: int | None) -> list[object]:
    return list(
        detect(c, relationship_threshold=2, repeat_threshold=5, scope=scope, **kwargs)  # type: ignore[arg-type]
    )


# --------------------------------------------------------- unit: statements


def test_five_scopes_with_one_run_each_are_a_finding_only_per_test() -> None:
    c = _collect(statements=[(BY_ID, n) for n in range(1, 6)])
    (finding,) = _detect(c, "test")
    assert isinstance(finding, RepeatFinding)
    assert (finding.count, finding.scopes, finding.scoped) == (5, 5, False)
    assert _detect(c, "session") == []


def test_one_scope_with_a_loop_is_a_finding_in_both_scopes() -> None:
    c = _collect(statements=[(BY_ID, 1)] * 6)
    for scope in ("test", "session"):
        (finding,) = _detect(c, scope)
        assert isinstance(finding, RepeatFinding)
        assert finding.count == 6
        assert finding.scopes == 1
        assert finding.scoped == (scope == "session")


def test_session_scope_reports_the_largest_scope() -> None:
    stmts = [(BY_ID, 1)] * 2 + [(BY_ID, 2)] * 7 + [(BY_ID, 3)] * 5
    (finding,) = _detect(_collect(statements=stmts), "session")
    assert isinstance(finding, RepeatFinding)
    assert finding.count == 7


def test_test_scope_counts_all_scopes_together_and_names_how_many() -> None:
    stmts = [(BY_ID, 1)] * 3 + [(BY_ID, 2)] * 4
    (finding,) = _detect(_collect(statements=stmts), "test")
    assert isinstance(finding, RepeatFinding)
    assert (finding.count, finding.scopes) == (7, 2)


def test_unknown_scopes_count_as_one() -> None:
    c = _collect(statements=[(BY_ID, None)] * 5)
    (finding,) = _detect(c, "session")
    assert isinstance(finding, RepeatFinding)
    assert finding.count == 5


def test_small_repeat_rule_counts_inside_one_scope() -> None:
    c = QueryCollector()
    for scope in (1, 2, 3):  # three requests, one run each, from one line
        c.statements.append(
            StatementEvent(BY_ID, SITE, None, parameters=(scope,), scope=scope)
        )
    found = _detect(c, "test", small_repeat_threshold=3)
    assert [type(f) for f in found] == [RepeatFinding]
    assert _detect(c, "session", small_repeat_threshold=2) == []
    c.statements.append(StatementEvent(BY_ID, SITE, None, parameters=(9,), scope=3))
    (finding,) = _detect(c, "session", small_repeat_threshold=2)
    assert isinstance(finding, RepeatFinding)
    assert (finding.count, finding.varying) == (2, True)


def test_a_key_over_the_threshold_in_one_scope_is_not_also_a_small_repeat() -> None:
    c = QueryCollector()
    for i in range(5):
        c.statements.append(StatementEvent(BY_ID, SITE, None, parameters=(i,), scope=1))
    for i in range(3):
        c.statements.append(StatementEvent(BY_ID, SITE, None, parameters=(i,), scope=2))
    (finding,) = _detect(c, "session", small_repeat_threshold=2)
    assert isinstance(finding, RepeatFinding)
    assert not finding.varying


# ------------------------------------------- unit: relationship, columns, writes


def _loads(scopes: list[int | None], keys: list[object]) -> QueryCollector:
    c = QueryCollector()
    for scope, key in zip(scopes, keys, strict=True):
        c.relationship_loads.append(
            RelationshipLoadEvent("Order.customer", key, SITE, True, scope)
        )
    return c


def test_relationship_parents_of_different_sessions_do_not_add_up_per_session() -> None:
    c = _loads([1, 2, 3], ["a", "b", "c"])
    (finding,) = _detect(c, "test")
    assert isinstance(finding, RelationshipFinding)
    assert (finding.count, finding.scopes) == (3, 3)
    assert _detect(c, "session") == []


def test_relationship_parents_of_one_session_are_a_finding() -> None:
    c = _loads([1, 1, 2], ["a", "b", "c"])
    (finding,) = _detect(c, "session")
    assert isinstance(finding, RelationshipFinding)
    assert (finding.count, finding.scoped) == (2, True)


def test_column_loads_are_counted_per_session() -> None:
    c = QueryCollector()
    for n in range(5):
        c.column_loads.append(
            ColumnLoadEvent("User", (), CAUSE_EXPIRED, ("User", n), SITE, scope=n)
        )
    (finding,) = _detect(c, "test")
    assert isinstance(finding, ColumnLoadFinding)
    assert (finding.instances, finding.scopes) == (5, 5)
    assert _detect(c, "session") == []
    for n in range(5):
        c.column_loads.append(
            ColumnLoadEvent("User", (), CAUSE_EXPIRED, ("User", 10 + n), SITE, scope=99)
        )
    (finding,) = _detect(c, "session")
    assert isinstance(finding, ColumnLoadFinding)
    assert (finding.instances, finding.count) == (5, 5)


def test_write_loops_are_counted_per_session() -> None:
    insert = "INSERT INTO log (msg) VALUES (?)"
    c = QueryCollector()
    for scope in range(1, 6):
        c.statements.append(StatementEvent(insert, SITE, None, scope=scope))
    found = _detect(c, "test", write_loop_threshold=5)
    assert [type(f) for f in found] == [WriteLoopFinding]
    assert _detect(c, "session", write_loop_threshold=5) == []


# ------------------------------------------------------------ unit: messages


def _message(c: QueryCollector, scope: str) -> str:
    (finding,) = make_findings(_detect(c, scope), cwd=Path("."))  # type: ignore[arg-type]
    return finding.message


def test_message_names_the_scopes_and_the_option_per_test() -> None:
    message = _message(_collect(statements=[(BY_ID, n) for n in range(9)]), "test")
    assert "ran 9 times across 9 different Sessions or connection checkouts" in message
    assert "queryguard_scope = session" in message
    assert "scope='session'" in message


def test_message_has_no_hint_for_one_scope() -> None:
    message = _message(_collect(statements=[(BY_ID, 1)] * 6), "test")
    assert "queryguard_scope" not in message
    assert "across" not in message


def test_message_says_one_session_when_scoped() -> None:
    message = _message(_collect(statements=[(BY_ID, 1)] * 6), "session")
    assert "ran 6 times in one Session or connection checkout" in message
    assert "queryguard_scope" not in message


# ------------------------------------------------------ real SQLAlchemy objects


def test_scopes_follow_sessions_and_checkouts() -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with record_queries() as log:
        for _ in range(2):  # two Sessions, two transactions each
            with Session(engine) as session:
                session.execute(text("SELECT 1"))
                session.commit()
                session.execute(text("SELECT 2"))
        with engine.connect() as conn:  # one checkout, two transactions
            conn.execute(text("SELECT 3"))
            conn.commit()
            conn.execute(text("SELECT 4"))
        with engine.connect() as conn:
            conn.execute(text("SELECT 5"))
    ids = [e.scope for e in log._collector.statements]
    assert len(ids) == 7
    assert None not in ids
    # Session one (both transactions), Session two, checkout one (both), checkout two
    assert ids[0] == ids[1]
    assert ids[2] == ids[3]
    assert ids[4] == ids[5]
    assert len({ids[0], ids[2], ids[4], ids[6]}) == 4


def test_a_shared_connection_gets_the_scope_of_the_session_using_it() -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with record_queries() as log, engine.connect() as conn:
        for _ in range(3):
            with Session(conn) as session:
                session.execute(text("SELECT 1"))
    assert len({e.scope for e in log._collector.statements}) == 3


def test_scope_of_returns_a_stable_number() -> None:
    engine = create_engine("sqlite://")
    with Session(engine) as session, engine.connect() as conn:
        assert scope_of_session(session) == scope_of_session(session)
        assert scope_of_connection(conn) == scope_of_connection(conn)
        assert scope_of_connection(conn) != scope_of_session(session)


def test_assert_no_nplusone_takes_the_scope() -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with record_queries() as log:
        for i in range(5):
            with Session(engine) as session:
                session.execute(text("SELECT 1 WHERE :i = :i"), {"i": i})
    with pytest.raises(NPlusOneDetected, match="across 5 different Sessions"):
        log.assert_no_nplusone()
    log.assert_no_nplusone(scope="session")
    with pytest.raises(ValueError, match="scope must be one of"):
        log.assert_no_nplusone(scope="request")


def test_assert_no_nplusone_repeat_scope_is_deprecated() -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with record_queries() as log:
        for i in range(5):
            with Session(engine) as session:
                session.execute(text("SELECT 1 WHERE :i = :i"), {"i": i})
    with pytest.warns(QueryGuardDeprecationWarning) as record:
        log.assert_no_nplusone(repeat_scope="session")
    (warning,) = record
    assert str(warning.message) == (
        "repeat_scope= is deprecated and will be removed in pytest-queryguard "
        "2.0; use scope= instead"
    )
    assert warning.filename == __file__  # the caller's line, not the package's
    with pytest.warns(QueryGuardDeprecationWarning):
        log.assert_no_nplusone(scope="session", repeat_scope="session")
    with (
        pytest.warns(QueryGuardDeprecationWarning),
        pytest.raises(ValueError, match="scope='test' and its deprecated name"),
    ):
        log.assert_no_nplusone(scope="test", repeat_scope="session")
    with pytest.warns(QueryGuardDeprecationWarning), pytest.raises(NPlusOneDetected):
        log.assert_no_nplusone(repeat_scope="test")


# ------------------------------------------------------------------- pytester

IMPORTS = """
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from qgmodels import Base, Customer, Order


def make_engine():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for c in range(1, 9):
            customer = Customer(id=c, name=f"customer {c}")
            customer.orders = [Order(id=c * 10)]
            s.add(customer)
        s.commit()
    return engine


def handle_request(engine, customer_id):
    with Session(engine) as s:
        return s.scalars(select(Customer).where(Customer.id == customer_id)).one()
"""

CASES = (
    IMPORTS
    + """

def test_five_requests():
    engine = make_engine()
    for i in range(1, 6):
        handle_request(engine, i)


def test_loop_in_one_session():
    engine = make_engine()
    with Session(engine) as s:
        for i in range(1, 7):
            s.scalars(select(Customer).where(Customer.id == i)).one()


def test_loop_with_a_commit_in_each_round():
    engine = make_engine()
    with Session(engine) as s:
        for i in range(1, 7):
            s.scalars(select(Customer).where(Customer.id == i)).one()
            s.commit()


def test_core_statements_in_five_checkouts():
    engine = make_engine()
    for i in range(5):
        with engine.connect() as conn:
            conn.execute(text("SELECT id FROM customers WHERE id = :i"), {"i": i})


def test_core_statements_in_one_checkout():
    engine = make_engine()
    with engine.connect() as conn:
        for i in range(6):
            conn.execute(text("SELECT id FROM customers WHERE id = :i"), {"i": i})


def test_requests_sharing_one_connection():
    engine = make_engine()
    with engine.connect() as conn:
        for i in range(1, 6):
            with Session(conn) as s:
                s.scalars(select(Customer).where(Customer.id == i)).one()


def test_lazy_loads_in_three_requests():
    engine = make_engine()
    for i in range(1, 4):
        with Session(engine) as s:
            s.scalars(select(Order).where(Order.id == i * 10)).one().customer


def test_lazy_loads_in_one_request():
    engine = make_engine()
    with Session(engine) as s:
        for order in s.scalars(select(Order)).all():
            order.customer


def test_expired_instances_in_five_requests():
    engine = make_engine()
    for i in range(1, 6):
        with Session(engine) as s:
            customer = s.get(Customer, i)
            s.commit()
            customer.name


def test_expired_instances_in_one_request():
    engine = make_engine()
    with Session(engine) as s:
        customers = s.scalars(select(Customer)).all()
        s.commit()
        for customer in customers[:6]:
            customer.name
"""
)

FAILING_BY_DEFAULT = {
    "test_five_requests",
    "test_loop_in_one_session",
    "test_loop_with_a_commit_in_each_round",
    "test_core_statements_in_five_checkouts",
    "test_core_statements_in_one_checkout",
    "test_requests_sharing_one_connection",
    "test_lazy_loads_in_three_requests",
    "test_lazy_loads_in_one_request",
    "test_expired_instances_in_five_requests",
    "test_expired_instances_in_one_request",
}
REAL_LOOPS = {
    "test_loop_in_one_session",
    "test_loop_with_a_commit_in_each_round",
    "test_core_statements_in_one_checkout",
    "test_lazy_loads_in_one_request",
    "test_expired_instances_in_one_request",
}


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS)
    return pytester


def _outcomes(
    pytester: pytest.Pytester, *args: str, scope: str | None = None
) -> tuple[set[str], pytest.RunResult]:
    if scope is not None:
        pytester.makeini(f"[pytest]\nqueryguard_scope = {scope}\n")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rf", *args)
    failed = {
        line.split("::")[-1].split(" ")[0]
        for line in result.outlines
        if line.startswith("FAILED ")
    }
    return failed, result


def test_default_scope_is_the_whole_test(project: pytest.Pytester) -> None:
    project.makepyfile(test_cases=CASES)
    failed, result = _outcomes(project)
    assert failed == FAILING_BY_DEFAULT
    output = result.stdout.str()
    assert "different Sessions or connection checkouts" in output
    assert "queryguard_scope = session" in output


def test_session_scope_keeps_only_the_real_loops(project: pytest.Pytester) -> None:
    project.makepyfile(test_cases=CASES)
    failed, result = _outcomes(project, scope="session")
    assert failed == REAL_LOOPS
    assert "in one Session or connection checkout" in result.stdout.str() or (
        "in one Session" in result.stdout.str()
    )
    assert "different Sessions" not in result.stdout.str()


def test_the_scope_is_case_insensitive_and_validated(project: pytest.Pytester) -> None:
    project.makepyfile(test_cases=CASES)
    failed, _ = _outcomes(project, scope="Session")
    assert failed == REAL_LOOPS
    project.makeini("[pytest]\nqueryguard_scope = request\n")
    result = project.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*queryguard_scope must be one of test, session*"])


DEPRECATED_INI = (
    "*QueryGuardDeprecationWarning: queryguard_repeat_scope is deprecated and "
    "will be removed in pytest-queryguard 2.0; rename it to queryguard_scope*"
)


def test_the_old_option_name_still_works_and_warns_once(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(test_cases=CASES)
    project.makeini("[pytest]\nqueryguard_repeat_scope = session\n")
    failed, result = _outcomes(project)
    assert failed == REAL_LOOPS
    output = result.stdout.str()
    assert output.count("QueryGuardDeprecationWarning") == 1
    result.stdout.fnmatch_lines([DEPRECATED_INI])
    # the old name is validated like the new one
    project.makeini("[pytest]\nqueryguard_repeat_scope = request\n")
    result = project.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        ["*queryguard_repeat_scope must be one of test, session*"]
    )


def test_the_old_option_name_warns_once_with_xdist(project: pytest.Pytester) -> None:
    pytest.importorskip("xdist")
    project.makepyfile(test_cases=CASES)
    project.makeini("[pytest]\nqueryguard_repeat_scope = session\n")
    failed, result = _outcomes(project, "-n", "2")
    assert failed == REAL_LOOPS
    assert result.stdout.str().count("QueryGuardDeprecationWarning") == 1


def test_both_option_names(project: pytest.Pytester) -> None:
    project.makepyfile(test_cases=CASES)
    project.makeini(
        "[pytest]\nqueryguard_scope = session\nqueryguard_repeat_scope = Session\n"
    )
    failed, result = _outcomes(project)
    assert failed == REAL_LOOPS
    result.stdout.fnmatch_lines([DEPRECATED_INI])
    project.makeini(
        "[pytest]\nqueryguard_scope = test\nqueryguard_repeat_scope = session\n"
    )
    result = project.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        [
            (
                "*queryguard_scope = test and its deprecated name "
                "queryguard_repeat_scope = session disagree; keep only "
                "queryguard_scope*"
            )
        ]
    )


def test_xdist_workers_count_per_session(project: pytest.Pytester) -> None:
    pytest.importorskip("xdist")
    project.makepyfile(test_cases=CASES)
    failed, _ = _outcomes(project, "-n", "2", scope="session")
    assert failed == REAL_LOOPS


ASYNC_CASES = """
import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from qgmodels import Base, Customer


async def make_engine():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


async def request(maker, i):
    async with maker() as s:
        await s.execute(select(Customer).where(Customer.id == i))


def test_five_sequential_async_requests():
    async def main():
        engine = await make_engine()
        maker = async_sessionmaker(engine, expire_on_commit=False)
        for i in range(5):
            await request(maker, i)
        await engine.dispose()
    asyncio.run(main())


def test_five_concurrent_async_requests():
    async def main():
        engine = await make_engine()
        maker = async_sessionmaker(engine, expire_on_commit=False)
        await asyncio.gather(*(request(maker, i) for i in range(5)))
        await engine.dispose()
    asyncio.run(main())


def test_async_loop_in_one_session():
    async def main():
        engine = await make_engine()
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            for i in range(6):
                await s.execute(select(Customer).where(Customer.id == i))
        await engine.dispose()
    asyncio.run(main())


def test_async_core_loop_in_one_connection():
    async def main():
        engine = await make_engine()
        async with engine.connect() as conn:
            for i in range(6):
                await conn.execute(text("SELECT id FROM customers WHERE id = :i"), {"i": i})
        await engine.dispose()
    asyncio.run(main())
"""


def test_async_sessions_and_connections(project: pytest.Pytester) -> None:
    pytest.importorskip("greenlet")
    pytest.importorskip("aiosqlite")
    project.makepyfile(test_async=ASYNC_CASES)
    failed, _ = _outcomes(project)
    assert failed == {
        "test_five_sequential_async_requests",
        "test_five_concurrent_async_requests",
        "test_async_loop_in_one_session",
        "test_async_core_loop_in_one_connection",
    }
    failed, _ = _outcomes(project, scope="session")
    assert failed == {
        "test_async_loop_in_one_session",
        "test_async_core_loop_in_one_connection",
    }
