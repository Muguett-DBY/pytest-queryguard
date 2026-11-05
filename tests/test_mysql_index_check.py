"""The missing-index check on a live MySQL or MariaDB server.

These tests need a server and skip without one. Point them at a server with

    QUERYGUARD_MYSQL_URL=mysql+pymysql://root:secret@127.0.0.1:3306/mysql
    QUERYGUARD_MARIADB_URL=mysql+pymysql://root:secret@127.0.0.1:3307/mysql

(a user that may create and drop databases; each session works in a scratch
database named qg_<random> and drops it, and keeps one small database,
qg_other, for the test of a table in another database). The mysql job of the
CI workflow runs them against MySQL and MariaDB.

QUERYGUARD_REQUIRE_BACKENDS (a comma-separated list of "mysql" and "mariadb")
turns the skips into failures for the servers it names: a missing driver, an
unset URL or an unreachable server then fails the run instead of skipping
every test of that server. CI sets it, so the job cannot pass without having
run them.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from pytest_queryguard import mysql_index

from . import mysql_cases
from .test_mysql_index import DIFFERENT, EXPECTED, NOTHING, summary

VARIABLES = {"mysql": "QUERYGUARD_MYSQL_URL", "mariadb": "QUERYGUARD_MARIADB_URL"}
REQUIRED = {
    name.strip()
    for name in os.environ.get("QUERYGUARD_REQUIRE_BACKENDS", "").split(",")
    if name.strip()
}
# a misspelt name would quietly turn the requirement off
if REQUIRED - set(VARIABLES):
    raise ValueError(
        f"QUERYGUARD_REQUIRE_BACKENDS names {sorted(REQUIRED - set(VARIABLES))};"
        f" known: {sorted(VARIABLES)}"
    )

if REQUIRED:
    # an ImportError here fails the collection, and with it the run
    import aiomysql  # noqa: F401
    import greenlet  # noqa: F401
    import pymysql
else:
    pymysql = pytest.importorskip("pymysql")
    pytest.importorskip("greenlet")
    pytest.importorskip("aiomysql")
# pytester drops modules first imported by an inner run from sys.modules when
# that run ends; importing the async stack here keeps it loaded.
import sqlalchemy.ext.asyncio  # noqa: F401
from sqlalchemy.engine import make_url

# pytester runs sessions in-process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone

ARGS = ("--queryguard-index-check",)


@dataclass(frozen=True)
class Server:
    kind: str
    url: str  # of the scratch database, with the pymysql driver
    database: str
    maria: bool
    version: str

    @property
    def aiomysql_url(self) -> str:
        return self.url.replace("+pymysql", "+aiomysql", 1)


def _connect(url: str, database: str | None = None, **kwargs: Any) -> Any:
    parsed = make_url(url)
    return pymysql.connect(
        host=parsed.host or "127.0.0.1",
        port=parsed.port or 3306,
        user=parsed.username or "root",
        password=parsed.password or "",
        database=database,
        **kwargs,
    )


@pytest.fixture(scope="session", params=sorted(VARIABLES))
def server(request: pytest.FixtureRequest) -> Iterator[Server]:
    """A scratch database filled with tests/mysql_cases.py, dropped afterwards."""
    kind: str = request.param
    # with the server required, the reason to skip is a failure instead
    unavailable = pytest.fail if kind in REQUIRED else pytest.skip
    url = os.environ.get(VARIABLES[kind])
    if not url:
        unavailable(f"{VARIABLES[kind]} is not set")
    try:
        admin = _connect(url, autocommit=True, connect_timeout=3)
    except pymysql.err.OperationalError as exc:
        unavailable(f"{VARIABLES[kind]} is not reachable: {exc}")
    database = f"qg_{uuid.uuid4().hex[:12]}"
    cursor = admin.cursor()
    cursor.execute("SELECT VERSION()")
    version = str(cursor.fetchone()[0])
    cursor.execute(f"CREATE DATABASE {database}")
    try:
        cursor.execute(f"USE {database}")
        for statement in mysql_cases.DDL:
            cursor.execute(statement)
        mysql_cases.fill(cursor)
        scratch = (
            make_url(url).set(database=database).render_as_string(hide_password=False)
        )
        yield Server(kind, scratch, database, "mariadb" in version.lower(), version)
    finally:
        cursor.execute(f"DROP DATABASE IF EXISTS {database}")
        admin.close()


@pytest.fixture
def connection(server: Server) -> Iterator[Any]:
    conn = _connect(server.url, server.database, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------- plans against a live server


def _live(connection: Any, name: str) -> list[tuple[str, str, str, str | None]]:
    sql, params = mysql_cases.CASES[name]
    return summary(mysql_index.explain(connection, sql, params))


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_live_scan_without_an_index_is_reported(
    server: Server, connection: Any, name: str
) -> None:
    assert _live(connection, name) == EXPECTED[name]


@pytest.mark.parametrize("name", NOTHING)
def test_live_no_finding_where_nothing_is_missing(
    server: Server, connection: Any, name: str
) -> None:
    assert _live(connection, name) == []


def test_live_plans_that_differ_between_servers(
    server: Server, connection: Any
) -> None:
    for name, expected in DIFFERENT.items():
        key = "mariadb11" if server.maria else "mysql8"
        assert _live(connection, name) == expected[key]


def test_live_index_becomes_visible_and_the_finding_goes(
    server: Server, connection: Any
) -> None:
    cursor = connection.cursor()
    cursor.execute("CREATE TABLE t_vis (a INT, b INT, KEY ix_a (a))")
    try:
        cursor.executemany(
            "INSERT INTO t_vis VALUES (%s, %s)", [(n, n) for n in range(500)]
        )
        cursor.execute("ANALYZE TABLE t_vis")
        cursor.fetchall()
        sql = "SELECT b FROM t_vis WHERE b = %s"
        assert len(mysql_index.explain(connection, sql, (5,))) == 1
        cursor.execute("CREATE INDEX ix_b ON t_vis (b)")
        assert mysql_index.explain(connection, sql, (5,)) == []
        if server.maria:
            cursor.execute("ALTER TABLE t_vis ALTER INDEX ix_b IGNORED")
        else:
            cursor.execute("ALTER TABLE t_vis ALTER INDEX ix_b INVISIBLE")
        assert len(mysql_index.explain(connection, sql, (5,))) == 1
    finally:
        cursor.execute("DROP TABLE t_vis")


# ---------------------------------------- EXPLAIN does not disturb the connection


def _checksum(cursor: Any) -> tuple[Any, ...]:
    cursor.execute("SELECT COUNT(*), SUM(total), SUM(customer_id) FROM orders")
    return tuple(cursor.fetchone())


def _transaction(cursor: Any) -> tuple[Any, ...]:
    """What the server says about this connection's open transaction."""
    cursor.execute(
        "SELECT trx_state, trx_rows_modified, trx_rows_locked, trx_tables_locked "
        "FROM information_schema.innodb_trx WHERE trx_mysql_thread_id = CONNECTION_ID()"
    )
    return tuple(cursor.fetchall())


WRITES = ["update_where", "delete_where", "update_pk"]


@pytest.mark.parametrize("name", WRITES)
def test_explain_of_a_write_changes_nothing_in_autocommit(
    server: Server, connection: Any, name: str
) -> None:
    cursor = connection.cursor()
    before = _checksum(cursor)
    _live(connection, name)
    assert _checksum(cursor) == before


@pytest.mark.parametrize("name", WRITES)
def test_explain_of_a_write_leaves_an_open_transaction_alone(
    server: Server, name: str
) -> None:
    conn = _connect(server.url, server.database, autocommit=False)
    other = _connect(server.url, server.database, autocommit=True)
    try:
        cursor, watcher = conn.cursor(), other.cursor()
        cursor.execute("UPDATE orders SET total = total + 1 WHERE id = %s", (1,))
        in_transaction = conn.server_status & 1  # SERVER_STATUS_IN_TRANS
        assert in_transaction
        state = _transaction(cursor)
        mine = _checksum(cursor)
        committed = _checksum(watcher)
        _live(conn, name)
        assert conn.server_status & 1 == in_transaction
        assert _transaction(cursor) == state  # no new rows modified or locked
        assert _checksum(cursor) == mine  # the test's own write is still pending
        assert _checksum(watcher) == committed  # and nobody else saw anything
        conn.rollback()
        assert _checksum(cursor) == committed
    finally:
        conn.close()
        other.close()


def test_explain_does_not_begin_a_transaction(server: Server) -> None:
    conn = _connect(server.url, server.database, autocommit=False)
    try:
        cursor = conn.cursor()
        assert not conn.server_status & 1
        _live(conn, "eq_unindexed")
        _live(conn, "update_where")
        assert not conn.server_status & 1
        assert _transaction(cursor) == ()
    finally:
        conn.close()


def test_explain_of_a_write_fires_no_trigger_and_takes_no_row_lock(
    server: Server,
) -> None:
    conn = _connect(server.url, server.database, autocommit=True)
    other = _connect(server.url, server.database, autocommit=True)
    try:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE trig_log (n INT)")
        cursor.execute(
            "CREATE TRIGGER trig_orders BEFORE UPDATE ON orders "
            "FOR EACH ROW INSERT INTO trig_log VALUES (1)"
        )
        try:
            mysql_index.explain(
                conn, "UPDATE orders SET total = total + 1 WHERE customer_id = %s", (7,)
            )
            cursor.execute("SELECT COUNT(*) FROM trig_log")
            assert cursor.fetchone()[0] == 0
            # another connection can lock every row at once: nothing is held
            watcher = other.cursor()
            watcher.execute("SET SESSION innodb_lock_wait_timeout = 1")
            watcher.execute(
                "SELECT id FROM orders WHERE customer_id = 7 FOR UPDATE NOWAIT"
            )
            watcher.fetchall()
        finally:
            cursor.execute("DROP TRIGGER trig_orders")
            cursor.execute("DROP TABLE trig_log")
    finally:
        conn.close()
        other.close()


def test_last_insert_id_survives_the_check(server: Server) -> None:
    conn = _connect(server.url, server.database, autocommit=True)
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO articles (title, body) VALUES ('x', 'y')")
        last = conn.insert_id()
        _live(conn, "eq_unindexed")
        cursor.execute("SELECT LAST_INSERT_ID()")
        assert cursor.fetchone()[0] == last
        cursor.execute("DELETE FROM articles WHERE id = %s", (last,))
    finally:
        conn.close()


def test_a_failing_statement_is_an_explain_error_not_a_crash(
    server: Server, connection: Any
) -> None:
    with pytest.raises(mysql_index._ExplainFailed):
        mysql_index.explain(
            connection, "SELECT * FROM no_such_table WHERE a = %s", (1,)
        )
    cursor = connection.cursor()
    cursor.execute("SELECT 1")  # the connection is still fine
    assert cursor.fetchone() == (1,)


# ------------------------------------------------------------- through pytest


MODELS = """
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int | None]
    reference: Mapped[str | None]
    status: Mapped[str | None]
    total: Mapped[int | None]
"""

CONFTEST = """
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

URL = {url!r}


@pytest.fixture
def engine():
    engine = create_engine(URL{extra})
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session
        session.rollback()
"""


@pytest.fixture
def project(pytester: pytest.Pytester, server: Server) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS)
    pytester.makeconftest(CONFTEST.format(url=server.url, extra=""))
    return pytester


SCAN_TEST = """
from sqlalchemy import select

from qgmodels import Order


def test_orders_of_customer(session):
    stmt = select(Order).where(Order.customer_id == 7)
    assert len(session.scalars(stmt).all()) == 10  # QG_LINE
"""


def test_filter_without_an_index_fails_the_test(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(test_a=SCAN_TEST)
    result = project.runpytest(*ARGS)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "queryguard: missing index",
            "",
            "  orders is read in full: full table scan (type ALL) with filter orders.customer_id = ?",
            "  first at test_a.py:*in test_orders_of_customer",
            "  sql: SELECT orders.id, orders.customer_id, *orders.customer_id = ?*",
            "  fix: CREATE INDEX ix_orders_customer_id ON orders (customer_id)",
            (
                '  alembic: op.create_index("ix_orders_customer_id", "orders", '
                '["customer_id"])'
            ),
        ]
    )
    result.stdout.fnmatch_lines(
        ["*index suggestions are derived from the statement*not verified*"]
    )


def test_advisor_off_keeps_the_create_index_but_drops_alembic(
    project: pytest.Pytester,
) -> None:
    project.makeini("[pytest]\nqueryguard_index_advisor = false\n")
    project.makepyfile(test_a=SCAN_TEST)
    result = project.runpytest(*ARGS)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        ["  fix: CREATE INDEX ix_orders_customer_id ON orders (customer_id)"]
    )
    result.stdout.no_fnmatch_line("*alembic:*")


def test_check_is_off_by_default(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=SCAN_TEST)
    project.runpytest().assert_outcomes(passed=1)


def test_indexed_filters_and_primary_key_pass(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        from sqlalchemy import select, text

        from qgmodels import Order


        def test_ok(session):
            assert session.get(Order, 7) is not None
            stmt = select(Order).where(Order.reference == "ref-7")
            assert len(session.scalars(stmt).all()) == 1
            assert session.scalars(select(Order).limit(5)).all()
            ordered = select(Order).order_by(Order.total.desc()).limit(3)
            assert len(session.scalars(ordered).all()) == 3
            assert session.execute(text("SELECT COUNT(*) FROM orders")).scalar()
        """
    )
    project.runpytest(*ARGS).assert_outcomes(passed=1)


def test_a_join_without_an_index_on_the_join_column(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a="""
        from sqlalchemy import text


        def test_join(session):
            session.execute(
                text(
                    "SELECT c.name, o.total FROM customers c "
                    "JOIN orders o ON o.customer_id = c.id WHERE c.email = :email"
                ),
                {"email": "c7@x.test"},
            ).all()
        """
    )
    result = project.runpytest(*ARGS)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            (
                "  orders is read in full: full table scan (type ALL*) with join condition "
                "o.customer_id = c.id"
            ),
            "*fix: CREATE INDEX ix_orders_customer_id ON orders (customer_id)",
        ]
    )


def test_update_and_delete_are_checked_and_not_run(
    project: pytest.Pytester, server: Server
) -> None:
    project.makepyfile(
        test_a="""
        from sqlalchemy import delete, update

        from qgmodels import Order


        def test_update(session):
            session.execute(update(Order).where(Order.customer_id == 7).values(total=1))


        def test_delete(session):
            session.execute(delete(Order).where(Order.total == 5))
        """
    )
    with _connect(server.url, server.database, autocommit=True) as conn:
        cursor = conn.cursor()
        before = _checksum(cursor)
        result = project.runpytest(*ARGS)
        result.assert_outcomes(failed=2)
        result.stdout.fnmatch_lines(["*orders is read in full: *"])
        # the session rolled back, and the explained writes never ran
        assert _checksum(cursor) == before


def test_allow_missing_index_marker(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a=SCAN_TEST.replace(
            "def test_orders",
            '@pytest.mark.allow_missing_index("orders")\ndef test_orders',
        ).replace("from sqlalchemy", "import pytest\nfrom sqlalchemy", 1)
    )
    project.runpytest(*ARGS).assert_outcomes(passed=1)


def test_ignore_tables_option(project: pytest.Pytester) -> None:
    project.makeini("[pytest]\nqueryguard_index_ignore_tables = ord*\n")
    project.makepyfile(test_a=SCAN_TEST)
    project.runpytest(*ARGS).assert_outcomes(passed=1)


def test_queryguard_ignore_for_the_finding(project: pytest.Pytester) -> None:
    project.makeini("[pytest]\nqueryguard_ignore =\n    missing-index orders\n")
    project.makepyfile(test_a=SCAN_TEST)
    result = project.runpytest(*ARGS)
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_baseline_holds_the_finding_back(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=SCAN_TEST)
    args = (*ARGS, "--queryguard-baseline=qg.json")
    project.runpytest(*args, "--queryguard-update-baseline")
    assert "missing-index" in (project.path / "qg.json").read_text()
    project.runpytest(*args).assert_outcomes(passed=1)
    project.runpytest(*ARGS).assert_outcomes(failed=1)


def test_sarif_has_the_finding_and_its_fix(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=SCAN_TEST)
    project.runpytest(*ARGS, "--queryguard-sarif=qg.sarif")
    sarif: Any = json.loads((project.path / "qg.sarif").read_text())
    results = sarif["runs"][0]["results"]
    assert [r["ruleId"] for r in results] == ["queryguard/missing-index"]
    text = results[0]["message"]["text"]
    assert "CREATE INDEX ix_orders_customer_id ON orders (customer_id)" in text


def test_fail_on_can_exclude_missing_index(project: pytest.Pytester) -> None:
    project.makeini("[pytest]\nqueryguard_fail_on = relationship-load\n")
    project.makepyfile(test_a=SCAN_TEST)
    project.runpytest(*ARGS).assert_outcomes(passed=1, warnings=1)


def test_warn_mode_reports_without_failing(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=SCAN_TEST)
    result = project.runpytest(*ARGS, "--queryguard-mode=warn")
    result.assert_outcomes(passed=1, warnings=1)


def test_a_statement_is_explained_once_per_test(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a="""
        from sqlalchemy import select

        from qgmodels import Order


        def test_twice(session):
            for customer in (1, 2, 3):
                session.scalars(select(Order).where(Order.customer_id == customer)).all()
        """
    )
    result = project.runpytest(*ARGS)
    result.assert_outcomes(failed=1)
    # the report only: on CI the short summary repeats the whole message
    report = result.stdout.str().split("short test summary info")[0]
    assert report.count("orders is read in full") == 1


def test_found_rows_is_not_disturbed(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        from sqlalchemy import text


        def test_found_rows(session):
            session.execute(
                text("SELECT SQL_CALC_FOUND_ROWS id FROM orders WHERE customer_id = 7 LIMIT 3")
            ).all()
            assert session.execute(text("SELECT FOUND_ROWS()")).scalar() == 10
        """
    )
    # the statement is left alone: no finding, and FOUND_ROWS() still counts it
    project.runpytest(*ARGS).assert_outcomes(passed=1)


def test_an_unbuffered_cursor_keeps_its_rows(
    project: pytest.Pytester, server: Server
) -> None:
    project.makeconftest(
        CONFTEST.format(
            url=server.url,
            extra=", connect_args={'cursorclass': __import__('pymysql').cursors.SSCursor}",
        )
    )
    project.makepyfile(
        test_a="""
        from sqlalchemy import select

        from qgmodels import Order


        def test_streaming(session):
            stmt = select(Order).where(Order.customer_id == 7)
            rows = session.execute(stmt.execution_options(yield_per=3)).scalars()
            assert len(list(rows)) == 10
        """
    )
    # an unbuffered result would lose its rows to a second statement on the
    # connection, so the check leaves the statement alone
    project.runpytest(*ARGS).assert_outcomes(passed=1)


ASYNC_TEST = """
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from qgmodels import Order

URL = {url!r}


async def run(statement):
    engine = create_async_engine(URL)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(statement)
            return len(result.all())
    finally:
        await engine.dispose()


def test_async_{name}():
    assert asyncio.run(run({statement})) == {rows}
"""


def test_async_scan_is_a_finding(project: pytest.Pytester, server: Server) -> None:
    project.makepyfile(
        test_a=ASYNC_TEST.format(
            url=server.aiomysql_url,
            name="scan",
            statement="select(Order).where(Order.customer_id == 7)",
            rows=10,
        )
    )
    result = project.runpytest(*ARGS)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*orders is read in full: full table scan (type ALL) with filter orders.customer_id = ?",
            "*fix: CREATE INDEX ix_orders_customer_id ON orders (customer_id)",
        ]
    )


def test_async_with_an_index_passes(project: pytest.Pytester, server: Server) -> None:
    project.makepyfile(
        test_a=ASYNC_TEST.format(
            url=server.aiomysql_url,
            name="indexed",
            statement='select(Order).where(Order.reference == "ref-7")',
            rows=1,
        )
    )
    project.runpytest(*ARGS).assert_outcomes(passed=1)
