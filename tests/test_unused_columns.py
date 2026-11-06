from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from sqlalchemy import event
from sqlalchemy.orm import InstrumentedAttribute, Mapper

from pytest_queryguard import eager, readhook, unused_columns

# pytester runs sessions in-process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone

MODELS = '''
from __future__ import annotations

from sqlalchemy import ForeignKey, create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    composite,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool
from dataclasses import dataclass


class Base(DeclarativeBase):
    pass


@dataclass
class Point:
    x: int
    y: int


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str] = mapped_column(default="e")
    phone: Mapped[str] = mapped_column(default="p")
    street: Mapped[str] = mapped_column(default="s")
    city: Mapped[str] = mapped_column(default="c")
    country: Mapped[str] = mapped_column(default="co")
    notes: Mapped[str] = mapped_column(default="n")
    bio: Mapped[str] = mapped_column(default="b")
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("owners.id"))
    owner: Mapped[Owner | None] = relationship(back_populates="accounts")


class Owner(Base):
    __tablename__ = "owners"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(default="o")
    accounts: Mapped[list[Account]] = relationship(back_populates="owner")


class Narrow(Base):
    __tablename__ = "narrow"

    id: Mapped[int] = mapped_column(primary_key=True)
    a: Mapped[str] = mapped_column(default="a")
    b: Mapped[str] = mapped_column(default="b")
    c: Mapped[str] = mapped_column(default="c")


class Shape(Base):
    __tablename__ = "shapes"

    id: Mapped[int] = mapped_column(primary_key=True)
    x1: Mapped[int] = mapped_column(default=0)
    y1: Mapped[int] = mapped_column(default=0)
    start: Mapped[Point] = composite("x1", "y1")
    c1: Mapped[str] = mapped_column(default="c")
    c2: Mapped[str] = mapped_column(default="c")
    c3: Mapped[str] = mapped_column(default="c")
    c4: Mapped[str] = mapped_column(default="c")
    c5: Mapped[str] = mapped_column(default="c")


def make_sync_session() -> Session:
    """In-memory SQLite: 2 owners, 3 accounts, 3 narrow rows, 2 shapes."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(engine, expire_on_commit=False)()
    session.add_all([Owner(id=1), Owner(id=2)])
    session.add_all(
        Account(id=i, name=f"account {i}", owner_id=1 + i % 2) for i in range(1, 4)
    )
    session.add_all(Narrow(id=i) for i in range(1, 4))
    session.add_all(Shape(id=i) for i in range(1, 3))
    session.commit()
    session.expunge_all()
    return session
'''

HEAD = """
from sqlalchemy import select
from sqlalchemy.orm import defer, joinedload, load_only, selectinload
from qgmodels import Account, Narrow, Owner, Shape, make_sync_session
"""

ON = ("-o", "queryguard_unused_columns=fail")


@pytest.fixture(autouse=True)
def _fresh_feature() -> Iterator[None]:
    unused_columns._broken = False
    unused_columns._warned = False
    yield
    unused_columns._broken = False
    unused_columns._warned = False


def run(
    pytester: pytest.Pytester,
    body: str,
    *args: str,
    head: str = HEAD,
    ini: str = "",
) -> pytest.RunResult:
    pytester.makepyfile(qgmodels=MODELS)
    pytester.makepyfile(test_app=head + "\n" + body)
    if ini:
        pytester.makeini(f"[pytest]\n{ini}\n")
    return pytester.runpytest_inprocess(*args)


def reported(result: pytest.RunResult, label: str = "Account") -> None:
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*queryguard: unused columns detected (experimental)*",
            f"*{label}: * columns loaded, * never read*",
            "*first at test_app.py:*in test_*",
            f"*kind: unused-columns  label: {label}*",
        ]
    )


NAME_ONLY = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Account)).all()
    assert [a.name for a in rows]
"""


# ------------------------------------------------------------------ scenarios


def test_reading_one_column_of_a_wide_row_is_reported(
    pytester: pytest.Pytester,
) -> None:
    result = run(pytester, NAME_ONLY, *ON)
    reported(result)
    out = result.stdout.str()
    # 10 columns: id and owner_id are needed, name is read
    assert "Account: 10 columns loaded, 7 never read" in out
    assert "never read: email, phone, street, city, country, notes, bio" in out
    # the foreign key stays, so Account.owner still loads without a query
    assert "fix: load only what is used, .options(load_only(Account.name, " in out
    assert "Account.owner_id))" in out


def test_reading_most_columns_is_clean(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Account)).all()
    assert [(a.name, a.email, a.phone, a.street) for a in rows]
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_half_read_is_still_reported(pytester: pytest.Pytester) -> None:
    # id and owner_id are needed, name, email and phone read: 5 of 10
    body = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Account)).all()
    assert [(a.name, a.email, a.phone) for a in rows]
"""
    reported(run(pytester, body, *ON))


def test_narrow_rows_are_not_reported(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    assert [n.a for n in session.scalars(select(Narrow)).all()]
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_load_only_query_is_clean(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    stmt = select(Account).options(load_only(Account.name))
    assert [a.name for a in session.scalars(stmt).all()]
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_deferred_columns_are_not_counted_as_loaded(
    pytester: pytest.Pytester,
) -> None:
    # 7 columns left: below the width that is reported
    body = """
def test_it():
    session = make_sync_session()
    stmt = select(Account).options(
        defer(Account.notes), defer(Account.bio), defer(Account.country)
    )
    assert [a.name for a in session.scalars(stmt).all()]
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_instances_never_looked_at_give_no_finding(
    pytester: pytest.Pytester,
) -> None:
    body = """
def test_it():
    session = make_sync_session()
    assert len(session.scalars(select(Account)).all()) == 3
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_objects_created_by_the_test_are_ignored(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    account = Account(id=9, name="new")
    session.add(account)
    session.flush()
    assert account.name == "new"
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_relationship_loads_are_not_counted(pytester: pytest.Pytester) -> None:
    # the accounts come from selectinload, not from the query's own entity
    body = """
def test_it():
    session = make_sync_session()
    stmt = select(Owner).options(selectinload(Owner.accounts))
    assert [a.name for o in session.scalars(stmt).all() for a in o.accounts]
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_reading_a_relationship_needs_its_foreign_key(
    pytester: pytest.Pytester,
) -> None:
    body = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Account).options(joinedload(Account.owner))).all()
    assert [a.owner.label for a in rows]
"""
    result = run(pytester, body, *ON)
    reported(result)
    result.stdout.fnmatch_lines(
        ["*Account: 10 columns loaded, 8 never read*", "*load_only(Account.owner_id)*"]
    )


def test_read_after_the_test_body_does_not_count(
    pytester: pytest.Pytester,
) -> None:
    body = """
import pytest


@pytest.fixture
def keep():
    box = []
    yield box
    assert [(a.email, a.phone, a.street, a.city) for a in box[0]]  # teardown


def test_it(keep):
    session = make_sync_session()
    rows = session.scalars(select(Account)).all()
    assert [a.name for a in rows]
    keep.append(rows)
"""
    reported(run(pytester, body, *ON))


def test_query_in_a_fixture_is_not_measured(pytester: pytest.Pytester) -> None:
    body = """
import pytest


@pytest.fixture
def rows():
    return make_sync_session().scalars(select(Account)).all()


def test_it(rows):
    assert [a.name for a in rows]
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_groups_are_per_function_of_the_query(pytester: pytest.Pytester) -> None:
    body = """
def names(session):
    return [a.name for a in session.scalars(select(Account)).all()]


def everything(session):
    rows = session.scalars(select(Account).where(Account.id > 1)).all()
    return [(a.email, a.phone, a.street, a.city, a.country) for a in rows]


def test_it():
    session = make_sync_session()
    assert names(session)
    session.expunge_all()
    assert everything(session)
"""
    result = run(pytester, body, *ON)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*first at test_app.py:*in names*"])
    assert "in everything" not in result.stdout.str()


def test_composite_columns_count_as_used(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Shape)).all()
    # x1 and y1 are read through Shape.start, which the plugin cannot see
    assert [(s.c1, s.c2) for s in rows]
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_expired_rows_queried_again_count(pytester: pytest.Pytester) -> None:
    # the session has the instances, expired: the query fires refresh, not load
    body = """
def test_it():
    session = make_sync_session()
    session.scalars(select(Account)).all()
    session.expire_all()
    rows = session.scalars(select(Account)).all()
    assert [a.name for a in rows]
"""
    reported(run(pytester, body, *ON))


def test_columns_an_instance_had_before_do_not_count(
    pytester: pytest.Pytester,
) -> None:
    # the query populates only Account.name: the other 9 columns were set by
    # the test, and the query with load_only() is not to blame for them
    body = """
def test_it():
    session = make_sync_session()
    account = Account(id=9, name="new")
    session.add(account)
    session.flush()
    session.expire(account, ["name"])
    stmt = select(Account).where(Account.id == 9).options(load_only(Account.name))
    (found,) = session.scalars(stmt).all()
    assert found.name == "new"
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_async_session(pytester: pytest.Pytester) -> None:
    pytest.importorskip("greenlet")
    pytester.makepyfile(qgmodels=MODELS)
    body = """
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from qgmodels import Account, Base


async def load(read):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(Account(id=1, name="a"))
            await session.commit()
            session.expunge_all()
            rows = (await session.scalars(select(Account))).all()
            return [read(a) for a in rows]
    finally:
        await engine.dispose()


def test_used():
    assert asyncio.run(load(lambda a: (a.name, a.email, a.phone, a.street)))


def test_unused():
    assert asyncio.run(load(lambda a: a.name)) == ["a"]
"""
    result = run(pytester, body, *ON, head="")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        [
            "*Account: 10 columns loaded, 7 never read*",
            "*first at test_app.py:*in load*",
        ]
    )


# -------------------------------------------------------------- the switch


def test_off_by_default(pytester: pytest.Pytester) -> None:
    run(pytester, NAME_ONLY).assert_outcomes(passed=1)


def test_off_installs_nothing(pytester: pytest.Pytester) -> None:
    before = InstrumentedAttribute.__get__
    run(pytester, NAME_ONLY).assert_outcomes(passed=1)
    assert not unused_columns.is_enabled()
    assert InstrumentedAttribute.__get__ is before
    assert not event.contains(Mapper, "load", unused_columns._on_load)
    assert readhook.instances is None


def test_enable_and_disable_restore_sqlalchemy() -> None:
    original = InstrumentedAttribute.__get__
    unused_columns.enable()
    unused_columns.enable()
    try:
        assert InstrumentedAttribute.__get__ is not original
        assert event.contains(Mapper, "load", unused_columns._on_load)
        unused_columns.disable()
        assert unused_columns.is_enabled()  # still one enable() outstanding
    finally:
        unused_columns.disable()
    assert InstrumentedAttribute.__get__ is original
    assert not event.contains(Mapper, "load", unused_columns._on_load)
    assert not event.contains(Mapper, "refresh", unused_columns._on_refresh)
    assert not unused_columns.is_enabled()
    unused_columns.disable()  # extra calls do nothing


@pytest.mark.parametrize("order", ["fifo", "lifo"])
def test_both_read_checks_share_one_wrapper(order: str) -> None:
    original = InstrumentedAttribute.__get__
    eager.enable()
    unused_columns.enable()
    try:
        assert InstrumentedAttribute.__get__ is readhook._tracked_get
    finally:
        if order == "fifo":
            eager.disable()
            assert InstrumentedAttribute.__get__ is readhook._tracked_get
            unused_columns.disable()
        else:
            unused_columns.disable()
            eager.disable()
    assert InstrumentedAttribute.__get__ is original


def test_both_checks_together(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Account).options(selectinload(Account.owner))).all()
    assert [a.name for a in rows]
"""
    result = run(pytester, body, *ON, "-o", "queryguard_unused_eager_load=fail")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*queryguard: unused eager loads and columns detected (experimental)*",
            "*Account.owner was loaded with selectinload and never read*",
            "*Account: 10 columns loaded, 7 never read*",
        ]
    )


def test_ini_option_and_warn_mode(pytester: pytest.Pytester) -> None:
    result = run(pytester, NAME_ONLY, ini="queryguard_unused_columns = warn")
    result.assert_outcomes(passed=1, warnings=1)
    result.stdout.fnmatch_lines(["*Account: 10 columns loaded, 7 never read*"])


def test_mode_off_turns_the_check_off(pytester: pytest.Pytester) -> None:
    run(pytester, NAME_ONLY, "--queryguard-mode=off", *ON).assert_outcomes(passed=1)


def test_bad_value_is_a_usage_error(pytester: pytest.Pytester) -> None:
    result = run(pytester, NAME_ONLY, "-o", "queryguard_unused_columns=maybe")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*queryguard_unused_columns must be one of*"])


# ----------------------------------------------------------------- opting out


def test_allow_nplusone_with_the_kind(pytester: pytest.Pytester) -> None:
    body = (
        "import pytest\n\n@pytest.mark.allow_nplusone(kind='unused-columns')"
        + NAME_ONLY
    )
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_allow_nplusone_with_the_class(pytester: pytest.Pytester) -> None:
    body = "import pytest\n\n@pytest.mark.allow_nplusone('Account')" + NAME_ONLY
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_queryguard_ignore(pytester: pytest.Pytester) -> None:
    result = run(
        pytester,
        NAME_ONLY,
        *ON,
        ini="queryguard_ignore =\n    unused-columns Account function=test_it",
    )
    result.assert_outcomes(passed=1)


def test_ignore_block(pytester: pytest.Pytester) -> None:
    body = """
from pytest_queryguard import ignore


def test_it():
    session = make_sync_session()
    with ignore("unused-columns"):
        rows = session.scalars(select(Account)).all()
    assert [a.name for a in rows]
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


# ------------------------------------------------------------------ fail open


def test_internal_error_disables_only_this_check(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(unused_columns, "_group", boom)
    result = run(pytester, NAME_ONLY, *ON)
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        ["*disabled the unused-columns check*the load listener*boom*"]
    )
    assert unused_columns._broken
    assert readhook.instances is None


def test_findings_never_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(tracker: object, sieve: object = None) -> list[object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(unused_columns, "unused", boom)
    with pytest.warns(UserWarning, match="disabled the unused-columns check"):
        assert unused_columns.findings(unused_columns.Tracker()) == []


# ------------------------------------------------------- SARIF, baseline, xdist


@pytest.mark.parametrize(("mode", "level"), [("warn", "warning"), ("fail", "error")])
def test_the_sarif_level_follows_the_mode(
    pytester: pytest.Pytester, mode: str, level: str
) -> None:
    result = run(
        pytester,
        NAME_ONLY,
        "--queryguard-sarif=out.sarif",
        "-W",
        "default",
        ini=f"queryguard_unused_columns = {mode}",
    )
    assert result.ret == (0 if mode == "warn" else 1)
    document = json.loads((pytester.path / "out.sarif").read_text())
    results = document["runs"][0]["results"]
    assert [r["ruleId"] for r in results] == ["queryguard/unused-columns"]
    assert results[0]["level"] == level
    rules = document["runs"][0]["tool"]["driver"]["rules"]
    (rule,) = [r for r in rules if r["id"] == "queryguard/unused-columns"]
    assert rule["helpUri"].endswith("/unused-columns.html")


def test_baseline_takes_the_finding(pytester: pytest.Pytester) -> None:
    update = run(
        pytester,
        NAME_ONLY,
        *ON,
        "--queryguard-baseline=qg.json",
        "--queryguard-update-baseline",
    )
    update.assert_outcomes(passed=1)
    assert "unused-columns" in (pytester.path / "qg.json").read_text()
    again = pytester.runpytest_inprocess(*ON, "--queryguard-baseline=qg.json")
    again.assert_outcomes(passed=1)


def test_xdist_workers_report_it(pytester: pytest.Pytester) -> None:
    pytest.importorskip("xdist")
    pytester.makepyfile(qgmodels=MODELS)
    pytester.makepyfile(test_app=HEAD + "\n" + NAME_ONLY)
    result = pytester.runpytest_subprocess(
        *ON, "-n", "2", "--queryguard-sarif=out.sarif"
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Account: 10 columns loaded, 7 never read*"])
    document = json.loads((pytester.path / "out.sarif").read_text())
    results = document["runs"][0]["results"]
    assert [r["ruleId"] for r in results] == ["queryguard/unused-columns"]


# ------------------------------------------------------------- the read hook


def _account() -> object:
    from tests.models import Customer

    return Customer(id=1, name="x")


def test_a_reused_id_is_not_taken_for_the_instance() -> None:
    unused_columns.enable()
    tracker = unused_columns.start()
    try:
        customer = _account()
        names: set[str] = set()
        # an entry for this id whose state is another object's
        tracker.instances[id(customer)] = (object(), names)
        assert customer.name == "x"  # type: ignore[attr-defined]
        assert names == set()
        state = customer._sa_instance_state  # type: ignore[attr-defined]
        tracker.instances[id(customer)] = (state, names)
        assert customer.name == "x"  # type: ignore[attr-defined]
        assert names == {"name"}
    finally:
        unused_columns.stop(tracker)
        unused_columns.disable()


def test_an_error_in_the_read_hook_turns_only_the_check_off() -> None:
    class Broken(dict[int, object]):
        def get(self, key: object, default: object = None) -> object:
            raise RuntimeError("boom")

    unused_columns.enable()
    tracker = unused_columns.start()
    try:
        readhook.instances = Broken()  # type: ignore[assignment]
        customer = _account()
        with pytest.warns(UserWarning, match="unused-columns check.*read hook"):
            assert customer.name == "x"  # type: ignore[attr-defined]
        assert unused_columns._broken
        assert readhook.instances is None
        assert customer.name == "x"  # type: ignore[attr-defined]
    finally:
        unused_columns.stop(tracker)
        unused_columns.disable()


def test_mixed_unused_kinds_have_their_own_heading() -> None:
    from pytest_queryguard.findings import Finding
    from pytest_queryguard.messages import format_findings

    made = [
        Finding(kind=kind, label="X", file=None, function=None, message="  m")
        for kind in ("unused-eager-load", "unused-columns")
    ]
    assert format_findings(made).splitlines()[0] == (
        "queryguard: unused eager loads and columns detected (experimental)"
    )
