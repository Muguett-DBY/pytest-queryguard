"""The missing-index check on SQLite, end to end through pytest."""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path
from typing import Any

import pytest

from pytest_queryguard import catalog, sqlite_index

# pytester runs sessions in-process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone

MODELS = (Path(__file__).parent / "models.py").read_text()

ARGS = ("--queryguard-index-check",)

SCAN_TEST = """
from qgmodels import Order, make_sync_session


def test_orders_of_customer():
    session = make_sync_session()
    assert session.query(Order).filter(Order.customer_id == 1).count() == 2
"""


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS)
    return pytester


# ------------------------------------------------------------ the report


def test_filter_without_an_index_fails_the_test(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=SCAN_TEST)
    result = project.runpytest(*ARGS)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "queryguard: missing index",
            "",
            "  orders is read in full: SCAN with filter orders.customer_id = ?",
            "  first at test_a.py:*in test_orders_of_customer",
            "  sql: SELECT count(*) AS count_1 FROM (*orders.customer_id = ?*",
            "  fix: CREATE INDEX ix_orders_customer_id ON orders (customer_id)",
            (
                '  alembic: op.create_index("ix_orders_customer_id", "orders", '
                '["customer_id"])'
            ),
        ]
    )


INDEX_SCAN_TEST = """
from sqlalchemy import create_engine, text


def test_status_filter_with_an_order_by_on_another_index():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, status TEXT, created_at TEXT)"))
        conn.execute(text("CREATE INDEX ix_users_created_at ON users (created_at)"))
        conn.execute(text("INSERT INTO users (status, created_at) VALUES ('a', 'x')"))
        conn.execute(
            text("SELECT * FROM users WHERE status = :s ORDER BY created_at LIMIT 5"),
            {"s": "a"},
        ).all()
"""


def test_a_scan_of_an_existing_index_is_worded_as_such(
    project: pytest.Pytester,
) -> None:
    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("the plan differs on this SQLite")
    project.makepyfile(test_a=INDEX_SCAN_TEST)
    result = project.runpytest(*ARGS)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            (
                "  users scans the whole index ix_users_created_at: the filter "
                "status = ? does not match the start of any index"
            ),
            "  first at test_a.py:*",
            "  sql: SELECT * FROM users WHERE status = ?*",
            "  fix: CREATE INDEX ix_users_status ON users (status)",
        ]
    )
    result.stdout.no_fnmatch_line("*users is read in full*")


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
    project.runpytest("--no-queryguard-index-check").assert_outcomes(passed=1)


def test_ini_option_turns_it_on(project: pytest.Pytester) -> None:
    project.makeini("[pytest]\nqueryguard_index_check = true\n")
    project.makepyfile(test_a=SCAN_TEST)
    project.runpytest().assert_outcomes(failed=1)


def test_no_session_notice_for_sqlite(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=SCAN_TEST)
    result = project.runpytest(*ARGS)
    result.stdout.no_fnmatch_line("*statements were not checked*")


def test_warn_mode_reports_without_failing(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=SCAN_TEST)
    result = project.runpytest(*ARGS, "--queryguard-mode=warn")
    result.assert_outcomes(passed=1, warnings=1)


INDEXED = """
from sqlalchemy import Index, text
from qgmodels import Order, make_sync_session


def test_with_an_index():
    session = make_sync_session()
    session.execute(text("CREATE INDEX ix_orders_customer ON orders (customer_id)"))
    assert session.query(Order).filter(Order.customer_id == 1).count() == 2
"""


def test_index_created_by_the_test_is_seen(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=INDEXED)
    project.runpytest(*ARGS).assert_outcomes(passed=1)


def test_lookups_by_primary_key_and_full_selects_pass(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a="""
        from qgmodels import Order, make_sync_session


        def test_ok():
            session = make_sync_session()
            assert session.get(Order, 10) is not None
            assert len(session.query(Order).all()) == 6
            assert len(session.query(Order).order_by(Order.customer_id).all()) == 6
        """
    )
    project.runpytest(*ARGS).assert_outcomes(passed=1)


# ------------------------------------------------------------ ignoring


def test_allow_missing_index_marker(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a=SCAN_TEST.replace(
            "def test_orders",
            '@pytest.mark.allow_missing_index("orders")\ndef test_orders',
        ).replace("from qgmodels", "import pytest\nfrom qgmodels", 1)
    )
    project.runpytest(*ARGS).assert_outcomes(passed=1)


def test_allow_missing_index_for_another_table_does_not_help(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a=SCAN_TEST.replace(
            "def test_orders",
            '@pytest.mark.allow_missing_index("customers")\ndef test_orders',
        ).replace("from qgmodels", "import pytest\nfrom qgmodels", 1)
    )
    project.runpytest(*ARGS).assert_outcomes(failed=1)


def _marked(*markers: str) -> str:
    """SCAN_TEST with the given marker lines on the test."""
    return SCAN_TEST.replace(
        "def test_orders",
        "\n".join(markers) + "\ndef test_orders",
    ).replace("from qgmodels", "import pytest\nfrom qgmodels", 1)


def test_allow_nplusone_with_the_missing_index_kind_silences_the_finding(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a=_marked(
            '@pytest.mark.allow_nplusone("orders", kind="missing-index")',
        )
    )
    result = project.runpytest(*ARGS)
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_allow_nplusone_missing_index_kind_without_a_label(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a=_marked('@pytest.mark.allow_nplusone(kind="missing-index")')
    )
    result = project.runpytest(*ARGS)
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*stale ignores*")


def test_allow_nplusone_for_another_table_does_not_help_and_is_stale(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a=_marked('@pytest.mark.allow_nplusone("customers", kind="missing-index")')
    )
    result = project.runpytest(*ARGS)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*stale ignores*",
            "*test_a.py::test_orders_of_customer: missing-index customers*",
        ]
    )


def test_a_bare_allow_nplusone_still_leaves_the_index_check_alone(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(test_a=_marked("@pytest.mark.allow_nplusone"))
    project.runpytest(*ARGS).assert_outcomes(failed=1)


def test_marker_patterns_for_both_kinds_are_not_stale(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a="""
        import pytest
        from qgmodels import Order, make_sync_session


        @pytest.mark.allow_nplusone("Order.customer")
        @pytest.mark.allow_nplusone("orders", kind="missing-index")
        def test_both():
            session = make_sync_session()
            for order in session.query(Order).all():
                _ = order.customer.name
            assert session.query(Order).filter(Order.customer_id == 1).count() == 2
        """
    )
    result = project.runpytest(*ARGS)
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*stale ignores*")


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
    data = json.loads((project.path / "qg.json").read_text())
    assert "missing-index" in json.dumps(data)
    assert "orders" in json.dumps(data)
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


# ------------------------------------------------------------ statements


def test_update_and_delete_are_checked(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        from sqlalchemy import delete, update
        from qgmodels import Customer, Order, make_sync_session


        def test_update():
            session = make_sync_session()
            session.execute(update(Order).where(Order.customer_id == 1).values(customer_id=5))


        def test_delete():
            session = make_sync_session()
            session.execute(delete(Customer).where(Customer.name == "x"))


        def test_by_key():
            session = make_sync_session()
            session.execute(delete(Customer).where(Customer.id == 1))
        """
    )
    result = project.runpytest(*ARGS)
    result.assert_outcomes(passed=1, failed=2)
    result.stdout.fnmatch_lines(
        [
            "*fix: CREATE INDEX ix_orders_customer_id ON orders (customer_id)",
        ]
    )
    result.stdout.fnmatch_lines(
        ["*fix: CREATE INDEX ix_customers_name ON customers (name)"]
    )


def test_core_text_and_core_select(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        from sqlalchemy import select, text
        from qgmodels import Order, make_sync_session


        def test_text():
            session = make_sync_session()
            session.execute(text("SELECT * FROM orders WHERE customer_id = :c"), {"c": 1})


        def test_core():
            session = make_sync_session()
            session.execute(select(Order.__table__).where(Order.customer_id == 2))
        """
    )
    result = project.runpytest(*ARGS)
    result.assert_outcomes(failed=2)
    # With xdist the summary repeats the messages; count the failure section.
    report = result.stdout.str().split("short test summary info")[0]
    assert report.count("CREATE INDEX ix_orders_customer_id") == 2


def test_one_explain_per_statement_shape(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = sqlite_index.explain

    def counting(connection: Any, statement: str, parameters: Any, *more: Any) -> Any:
        calls.append(statement)
        return real(connection, statement, parameters, *more)

    monkeypatch.setattr(sqlite_index, "explain", counting)
    project.makepyfile(
        test_a="""
        import pytest
        from qgmodels import Order, make_sync_session


        @pytest.mark.allow_nplusone
        def test_loop():
            session = make_sync_session()
            for i in range(20):
                session.query(Order).filter(Order.id == i).all()
            session.query(Order).filter(Order.id.in_([1, 2])).all()
            session.query(Order).filter(Order.id.in_([1, 2, 3, 4])).all()
        """
    )
    # Earlier in-process tests leave connections with DDL in an open
    # transaction (test_index_created_by_the_test_is_seen, among others).
    # When the garbage collector frees one mid-run, catalog._lost clears the
    # cache and every statement is explained again. Hide them from this run.
    monkeypatch.setattr(catalog, "_pending", {})
    project.runpytest(*ARGS).assert_outcomes(passed=1)
    mine = [c for c in calls if "orders" in c and "WHERE" in c]
    # 1 for the id lookups, 1 for both IN lists (they collapse to one shape)
    assert len(mine) == 2, mine


def test_executemany_is_not_explained(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = sqlite_index.explain
    monkeypatch.setattr(
        sqlite_index,
        "explain",
        lambda c, s, p, *m: calls.append(s) or real(c, s, p, *m),
    )
    project.makepyfile(
        test_a="""
        from sqlalchemy import text
        from qgmodels import make_sync_session


        def test_many():
            session = make_sync_session()
            session.execute(
                text("UPDATE orders SET id = id WHERE customer_id = :c"),
                [{"c": 1}, {"c": 2}],
            )
        """
    )
    project.runpytest(*ARGS).assert_outcomes(passed=1)
    assert not [c for c in calls if "UPDATE orders" in c]


def test_statements_outside_the_test_body_are_not_checked(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a="""
        import pytest
        from qgmodels import Order, make_sync_session


        @pytest.fixture
        def session():
            s = make_sync_session()
            s.query(Order).filter(Order.customer_id == 1).all()  # set-up
            return s


        def test_ok(session):
            assert session.get(Order, 10) is not None
        """
    )
    project.runpytest(*ARGS).assert_outcomes(passed=1)


# ------------------------------------------------------------ the database is untouched


def test_explain_runs_no_statement_and_opens_no_transaction(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a="""
        import sqlite3

        import pytest
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool


        def make(**options):
            raw = sqlite3.connect(":memory:", check_same_thread=False, **options)
            raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            raw.executemany("INSERT INTO t (v) VALUES (?)", [("a",), ("b",), ("c",)])
            raw.commit()
            return raw


        @pytest.mark.allow_missing_index("t")
        @pytest.mark.parametrize("isolation", ["", "DEFERRED", "IMMEDIATE", None])
        def test_legacy_modes(isolation):
            raw = make(isolation_level=isolation)
            engine = create_engine(
                "sqlite://", creator=lambda: raw, poolclass=StaticPool
            )
            rows = lambda: raw.execute("SELECT v FROM t ORDER BY id").fetchall()
            with engine.connect() as conn:
                # a SELECT starts no transaction in the legacy modes
                conn.exec_driver_sql("SELECT * FROM t WHERE v = ?", ("a",))
                assert raw.in_transaction is False
                # a DML statement starts one (except with isolation_level=None)
                conn.exec_driver_sql("UPDATE t SET v = 'x' WHERE v = ?", ("a",))
                opened = raw.in_transaction
                assert opened is (isolation is not None)
                conn.exec_driver_sql("DELETE FROM t WHERE v = ?", ("b",))
                assert raw.in_transaction is opened
                assert [r[0] for r in rows()] == ["x", "c"]
                conn.rollback()
            if isolation is not None:
                assert [r[0] for r in rows()] == ["a", "b", "c"]


        @pytest.mark.skipif(
            not hasattr(sqlite3.Connection, "autocommit"), reason="Python 3.12+"
        )
        @pytest.mark.allow_missing_index("t")
        @pytest.mark.parametrize("autocommit", [True, False])
        def test_autocommit_attribute(autocommit):
            raw = make(autocommit=autocommit)
            engine = create_engine(
                "sqlite://", creator=lambda: raw, poolclass=StaticPool
            )
            with engine.connect() as conn:
                conn.exec_driver_sql("UPDATE t SET v = 'x' WHERE v = ?", ("a",))
                conn.exec_driver_sql("SELECT * FROM t WHERE v = ?", ("b",))
                assert [r[0] for r in raw.execute("SELECT v FROM t ORDER BY id")] == [
                    "x", "b", "c"
                ]
                conn.rollback()
            if not autocommit:
                assert [r[0] for r in raw.execute("SELECT v FROM t ORDER BY id")] == [
                    "a", "b", "c"
                ]
        """
    )
    # allow_missing_index silences the findings; the next test proves that the
    # check does run on the same statements.
    result = project.runpytest(*ARGS)
    if hasattr(sqlite3.Connection, "autocommit"):
        result.assert_outcomes(passed=6)
    else:
        result.assert_outcomes(passed=4, skipped=2)


def test_check_does_not_change_in_transaction_or_data(
    project: pytest.Pytester, tmp_path: Path
) -> None:
    # The same statements with and without the check must leave the same state.
    script = textwrap.dedent(
        """
        import sqlite3, sys
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        def run(isolation):
            raw = sqlite3.connect(":memory:", isolation_level=isolation)
            raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            raw.executemany("INSERT INTO t (v) VALUES (?)", [("a",), ("b",)])
            raw.commit()
            engine = create_engine("sqlite://", creator=lambda: raw, poolclass=StaticPool)
            states = []
            with engine.connect() as conn:
                for sql in (
                    "SELECT * FROM t WHERE v = 'a'",
                    "UPDATE t SET v = 'z' WHERE v = 'a'",
                    "DELETE FROM t WHERE v = 'b'",
                    "SELECT * FROM t WHERE v = 'z'",
                ):
                    conn.exec_driver_sql(sql)
                    states.append(raw.in_transaction)
                data = raw.execute("SELECT id, v FROM t").fetchall()
                conn.rollback()
            return states, data, raw.execute("SELECT id, v FROM t").fetchall()

        print(repr([run(i) for i in ("", "DEFERRED", None)]))
        """
    )
    from pytest_queryguard.collector import QueryCollector, pop, push
    from pytest_queryguard.listeners import install, install_index_check

    def run(with_check: bool) -> str:
        namespace: dict[str, Any] = {}
        collector = QueryCollector(index_check=with_check)
        install()
        install_index_check()
        push(collector)
        try:
            import contextlib
            import io

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                exec(compile(script, "<script>", "exec"), namespace)  # noqa: S102
        finally:
            pop(collector)
        if with_check:
            assert collector.scans, "the check did not run"
        else:
            assert not collector.scans
        return out.getvalue()

    assert run(True) == run(False)
