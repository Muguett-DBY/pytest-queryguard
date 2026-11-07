from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.orm import InstrumentedAttribute, Session

from pytest_queryguard import eager

# pytester runs sessions in-process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone

greenlet = pytest.importorskip("greenlet")  # see test_async_and_fastapi.py

MODELS = (Path(__file__).parent / "models.py").read_text()

HEAD = """
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload, subqueryload, immediateload
from qgmodels import Customer, Order, make_sync_session
"""

ON = ("-o", "queryguard_unused_eager_load=fail")


@pytest.fixture(autouse=True)
def _fresh_feature() -> Iterator[None]:
    eager._broken = False
    eager._warned = False
    yield
    eager._broken = False
    eager._warned = False


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


def unused(result: pytest.RunResult, label: str, loader: str) -> None:
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*queryguard: unused eager load detected (experimental)*",
            f"*{label} was loaded with {loader} and never read*",
            "*first at test_app.py:*in test_*",
        ]
    )


# ------------------------------------------------------------------ scenarios


def test_used_selectinload_is_clean(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Customer).options(selectinload(Customer.orders))).all()
    assert [len(c.orders) for c in rows] == [2, 2, 2]
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_unused_selectinload_is_reported(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Customer).options(selectinload(Customer.orders))).all()
    assert [c.name for c in rows]
"""
    result = run(pytester, body, *ON)
    unused(result, "Customer.orders", "selectinload")
    result.stdout.fnmatch_lines(["*fix: remove selectinload(Customer.orders)*"])


def test_unused_joinedload_is_reported(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    stmt = select(Customer).options(joinedload(Customer.orders))
    rows = session.scalars(stmt).unique().all()
    assert [c.name for c in rows]
"""
    unused(run(pytester, body, *ON), "Customer.orders", "joinedload")


@pytest.mark.parametrize("loader", ["subqueryload"])
def test_other_eager_loaders_are_reported(
    pytester: pytest.Pytester, loader: str
) -> None:
    body = f"""
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Customer).options({loader}(Customer.orders))).all()
    assert [c.name for c in rows]
"""
    unused(run(pytester, body, *ON), "Customer.orders", loader)


def test_unused_immediateload_is_reported_next_to_its_n_plus_one(
    pytester: pytest.Pytester,
) -> None:
    # immediateload loads each parent's relationship with its own statement, so
    # the N+1 finding comes too; the unused one is added to the same report
    body = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Customer).options(immediateload(Customer.orders))).all()
    assert [c.name for c in rows]
"""
    result = run(pytester, body, *ON)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*queryguard: N+1 detected*",
            "*Customer.orders was lazy-loaded 3 times*",
            "*Customer.orders was loaded with immediateload and never read*",
        ]
    )


def test_unused_scalar_relationship_is_reported(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Order).options(joinedload(Order.customer))).all()
    assert [o.id for o in rows]
"""
    unused(run(pytester, body, *ON), "Order.customer", "joinedload")


def test_used_scalar_relationship_is_clean(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Order).options(joinedload(Order.customer))).all()
    assert {o.customer.name for o in rows}
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_nested_chain_reports_only_the_unused_child(
    pytester: pytest.Pytester,
) -> None:
    body = """
def test_it():
    session = make_sync_session()
    stmt = select(Customer).options(
        selectinload(Customer.orders).selectinload(Order.customer)
    )
    rows = session.scalars(stmt).all()
    assert [o.id for c in rows for o in c.orders]
"""
    result = run(pytester, body, *ON)
    unused(result, "Order.customer", "selectinload")
    assert "Customer.orders was loaded" not in result.stdout.str()


def test_nested_chain_reports_the_parent_once(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    stmt = select(Customer).options(
        selectinload(Customer.orders).selectinload(Order.customer)
    )
    assert [c.name for c in session.scalars(stmt).all()]
"""
    result = run(pytester, body, *ON)
    unused(result, "Customer.orders", "selectinload")
    assert "Order.customer was loaded" not in result.stdout.str()


def test_read_inside_a_comprehension_counts(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Order).options(joinedload(Order.customer))).all()
    names = {n for n in (o.customer.name for o in rows)}
    assert names
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_read_after_the_test_body_does_not_count(
    pytester: pytest.Pytester,
) -> None:
    body = """
import pytest


@pytest.fixture
def keep():
    box = []
    yield box
    assert [len(c.orders) for c in box[0]]  # teardown: outside the window


def test_it(keep):
    session = make_sync_session()
    rows = session.scalars(select(Customer).options(selectinload(Customer.orders))).all()
    assert [c.name for c in rows]
    keep.append(rows)
"""
    result = run(pytester, body, *ON)
    result.assert_outcomes(failed=1, passed=0)
    result.stdout.fnmatch_lines(["*Customer.orders was loaded with selectinload*"])


def test_eager_load_in_a_fixture_is_not_measured(pytester: pytest.Pytester) -> None:
    body = """
import pytest


@pytest.fixture
def rows():
    session = make_sync_session()
    return session.scalars(select(Customer).options(selectinload(Customer.orders))).all()


def test_it(rows):
    assert [c.name for c in rows]
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_instances_never_looked_at_give_no_finding(
    pytester: pytest.Pytester,
) -> None:
    body = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Customer).options(selectinload(Customer.orders))).all()
    assert len(rows) == 3
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_no_rows_give_no_finding(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    stmt = select(Customer).where(Customer.id > 99).options(selectinload(Customer.orders))
    assert session.scalars(stmt).all() == []
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_async_session(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(qgmodels=MODELS)
    body = """
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import StaticPool

from qgmodels import Base, Customer, Order


async def load(read):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(Customer(id=1, name="a", orders=[Order(id=1), Order(id=2)]))
            await session.commit()
            session.expunge_all()
            stmt = select(Customer).options(selectinload(Customer.orders))
            rows = (await session.scalars(stmt)).all()
            return [read(c) for c in rows]
    finally:
        await engine.dispose()


def test_used():
    assert asyncio.run(load(lambda c: len(c.orders))) == [2]


def test_unused():
    assert asyncio.run(load(lambda c: c.name)) == ["a"]
"""
    result = run(pytester, body, *ON, head="")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        [
            "*Customer.orders was loaded with selectinload and never read*",
            "*first at test_app.py:*in load*",
        ]
    )


def test_eager_loaded_in_one_place_and_read_elsewhere_is_clean(
    pytester: pytest.Pytester,
) -> None:
    body = """
def test_it():
    session = make_sync_session()
    stmt = select(Customer).options(selectinload(Customer.orders))
    rows = session.scalars(stmt).all()
    assert rows[0].name
    assert len(rows[1].orders) == 2  # one instance is enough
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


# --------------------------------------------------- false-positive traps


def test_reading_dict_directly_gives_no_finding(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Customer).options(selectinload(Customer.orders))).all()
    dumped = [{k: v for k, v in vars(c).items() if not k.startswith("_")} for c in rows]
    assert dumped[0]["orders"]
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_getattr_serialization_counts_as_a_read(pytester: pytest.Pytester) -> None:
    body = """
def dump(obj, names):
    return {n: getattr(obj, n) for n in names}


def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Customer).options(selectinload(Customer.orders))).all()
    assert dump(rows[0], ["name", "orders"])
"""
    run(pytester, body, *ON).assert_outcomes(passed=1)


LAZY_RAISE = """
from sqlalchemy import ForeignKey, create_engine, select
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship, selectinload, sessionmaker,
)


class Base(DeclarativeBase):
    pass


class Parent(Base):
    __tablename__ = "parent"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(default="p")
    kids: Mapped[list["Kid"]] = relationship(lazy="raise", cascade="all, delete-orphan")


class Kid(Base):
    __tablename__ = "kid"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("parent.id"))


def make():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine, expire_on_commit=False)()
    session.add_all([Parent(id=1, kids=[Kid(id=1), Kid(id=2)]), Parent(id=2)])
    session.commit()
    session.expunge_all()
    return session
"""


def test_lazy_raise_with_used_option_is_clean(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make()
    rows = session.scalars(select(Parent).options(selectinload(Parent.kids))).all()
    assert [len(p.kids) for p in rows] == [2, 0]
"""
    run(pytester, body, *ON, head=LAZY_RAISE).assert_outcomes(passed=1)


def test_lazy_raise_with_unused_option_is_reported(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make()
    rows = session.scalars(select(Parent).options(selectinload(Parent.kids))).all()
    assert [p.name for p in rows]
"""
    result = run(pytester, body, *ON, head=LAZY_RAISE)
    unused(result, "Parent.kids", "selectinload")


def test_relationship_loaded_for_a_cascading_delete_is_clean(
    pytester: pytest.Pytester,
) -> None:
    body = """
def test_it():
    session = make()
    parent = session.scalars(
        select(Parent).where(Parent.id == 1).options(selectinload(Parent.kids))
    ).one()
    assert parent.name
    session.delete(parent)
    session.flush()
"""
    run(pytester, body, *ON, head=LAZY_RAISE).assert_outcomes(passed=1)


def test_flush_alone_gives_no_verdict(pytester: pytest.Pytester) -> None:
    body = """
def test_it():
    session = make()
    rows = session.scalars(select(Parent).options(selectinload(Parent.kids))).all()
    rows[0].name = "changed"
    session.flush()
"""
    # nothing read the relationship, not even a dirty flush: still unused, but
    # no attribute was read through the instance either, so there is no verdict
    run(pytester, body, *ON, head=LAZY_RAISE).assert_outcomes(passed=1)


def test_flush_after_reading_another_attribute_is_reported(
    pytester: pytest.Pytester,
) -> None:
    body = """
def test_it():
    session = make()
    rows = session.scalars(select(Parent).options(selectinload(Parent.kids))).all()
    rows[0].name = rows[0].name + "!"
    session.flush()
"""
    unused(run(pytester, body, *ON, head=LAZY_RAISE), "Parent.kids", "selectinload")


def test_contains_eager_and_other_options_are_not_eager_loads() -> None:
    from sqlalchemy.orm import contains_eager, defaultload, defer, load_only, raiseload

    from tests.models import Customer

    options = [
        contains_eager(Customer.orders),
        defer(Customer.name),
        load_only(Customer.id),
        raiseload(Customer.orders),
        defaultload(Customer.orders),
    ]
    assert eager._elements(options) == []


def test_chain_elements_keep_their_parent() -> None:
    from sqlalchemy.orm import selectinload

    from tests.models import Customer, Order

    loads = eager._elements([selectinload(Customer.orders).joinedload(Order.customer)])
    assert [(e.label, e.loader, e.parent) for e in loads] == [
        ("Customer.orders", "selectinload", None),
        ("Order.customer", "joinedload", (Customer, "orders")),
    ]


# -------------------------------------------------------------- the switch


UNUSED = """
def test_it():
    session = make_sync_session()
    rows = session.scalars(select(Customer).options(selectinload(Customer.orders))).all()
    assert [c.name for c in rows]
"""


def test_off_by_default(pytester: pytest.Pytester) -> None:
    run(pytester, UNUSED).assert_outcomes(passed=1)


def test_off_installs_nothing(pytester: pytest.Pytester) -> None:
    before = InstrumentedAttribute.__get__
    run(pytester, UNUSED).assert_outcomes(passed=1)
    assert not eager.is_enabled()
    assert InstrumentedAttribute.__get__ is before
    assert not event.contains(Session, "do_orm_execute", eager._on_orm_execute)
    assert not event.contains(Session, "before_flush", eager._on_before_flush)


def test_enable_and_disable_restore_sqlalchemy() -> None:
    original = InstrumentedAttribute.__get__
    eager.enable()
    eager.enable()
    try:
        assert InstrumentedAttribute.__get__ is not original
        assert event.contains(Session, "do_orm_execute", eager._on_orm_execute)
        eager.disable()
        assert eager.is_enabled()  # still one enable() outstanding
    finally:
        eager.disable()
    assert InstrumentedAttribute.__get__ is original
    assert not event.contains(Session, "do_orm_execute", eager._on_orm_execute)
    assert not eager.is_enabled()
    eager.disable()  # extra calls do nothing


def test_ini_option_and_warn_mode(pytester: pytest.Pytester) -> None:
    result = run(pytester, UNUSED, ini="queryguard_unused_eager_load = warn")
    result.assert_outcomes(passed=1, warnings=1)
    result.stdout.fnmatch_lines(["*Customer.orders was loaded with selectinload*"])


def test_warn_does_not_fail_even_when_mode_is_fail(
    pytester: pytest.Pytester,
) -> None:
    result = run(
        pytester,
        UNUSED,
        "--queryguard-mode=fail",
        "-o",
        "queryguard_unused_eager_load=warn",
    )
    result.assert_outcomes(passed=1, warnings=1)


def test_mode_off_turns_the_check_off(pytester: pytest.Pytester) -> None:
    run(pytester, UNUSED, "--queryguard-mode=off", *ON).assert_outcomes(passed=1)


def test_bad_value_is_a_usage_error(pytester: pytest.Pytester) -> None:
    result = run(pytester, UNUSED, "-o", "queryguard_unused_eager_load=maybe")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*queryguard_unused_eager_load must be one of*"])


def test_fail_on_can_leave_it_a_warning(pytester: pytest.Pytester) -> None:
    result = run(
        pytester,
        UNUSED,
        *ON,
        ini="queryguard_fail_on =\n    repeated-statement",
    )
    result.assert_outcomes(passed=1, warnings=1)


# ----------------------------------------------------------------- opting out


def test_allow_nplusone_with_the_kind(pytester: pytest.Pytester) -> None:
    body = (
        "import pytest\n\n@pytest.mark.allow_nplusone(kind='unused-eager-load')"
        + UNUSED
    )
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_allow_nplusone_with_the_label(pytester: pytest.Pytester) -> None:
    body = "import pytest\n\n@pytest.mark.allow_nplusone('Customer.orders')" + UNUSED
    run(pytester, body, *ON).assert_outcomes(passed=1)


def test_allow_nplusone_for_another_label_still_reports(
    pytester: pytest.Pytester,
) -> None:
    body = "import pytest\n\n@pytest.mark.allow_nplusone('Order.customer')" + UNUSED
    run(pytester, body, *ON).assert_outcomes(failed=1)


def test_queryguard_ignore(pytester: pytest.Pytester) -> None:
    result = run(
        pytester,
        UNUSED,
        *ON,
        ini="queryguard_ignore =\n    unused-eager-load Customer.orders function=test_it",
    )
    result.assert_outcomes(passed=1)


# ------------------------------------------------------------------ fail open


def test_internal_error_disables_only_this_check(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(options: object) -> list[object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(eager, "_elements", boom)
    result = run(pytester, UNUSED, *ON)
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        ["*disabled the unused eager-load check*the do_orm_execute listener*boom*"]
    )
    assert eager._broken


def test_findings_never_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(tracker: object) -> list[object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(eager, "unused", boom)
    with pytest.warns(UserWarning, match="disabled the unused eager-load check"):
        assert eager.findings(eager.Tracker()) == []


def test_backref_handling_does_not_count_as_a_read(
    pytester: pytest.Pytester,
) -> None:
    body = """
def test_it():
    session = make_sync_session()
    customers = session.scalars(
        select(Customer).options(selectinload(Customer.orders))
    ).all()
    order = session.get(Order, 10)
    assert customers[0].name
    order.customer = customers[1]  # SQLAlchemy updates Customer.orders itself
    session.flush()
"""
    unused(run(pytester, body, *ON), "Customer.orders", "selectinload")


def test_every_ignorable_kind_has_a_sarif_rule() -> None:
    from pytest_queryguard import ignores, sarif

    assert set(ignores.KINDS) == set(sarif.KINDS)
    assert sarif.rule_id("unused-eager-load") in {
        sarif.rule_id(kind) for kind in sarif.KINDS
    }


INHERITANCE_HEAD = """
from sqlalchemy import ForeignKey, create_engine, select
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, Session, mapped_column, relationship, selectinload,
    with_polymorphic,
)


class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = "employee"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str]
    name: Mapped[str]
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("manager.id"))
    __mapper_args__ = {"polymorphic_on": "type", "polymorphic_identity": "employee"}


class Manager(Employee):
    __tablename__ = "manager"
    id: Mapped[int] = mapped_column(ForeignKey("employee.id"), primary_key=True)
    reports: Mapped[list[Employee]] = relationship(
        foreign_keys=[Employee.manager_id],
        primaryjoin="Employee.manager_id == Manager.id",
    )
    __mapper_args__ = {
        "polymorphic_identity": "manager",
        "inherit_condition": id == Employee.id,
    }


def staff_session(managers):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add_all(Employee(name=f"e{i}") for i in range(4))
    session.add_all(Manager(name=f"m{i}") for i in range(managers))
    session.commit()
    return session


def org_chart(session, read_reports):
    staff = with_polymorphic(Employee, [Manager])
    stmt = select(staff).options(selectinload(staff.Manager.reports))
    chart = {}
    for e in session.scalars(stmt):
        reports = e.reports if read_reports and isinstance(e, Manager) else []
        chart[e.name] = [r.name for r in reports]
    return chart
"""


def test_a_subclass_relationship_without_subclass_rows_is_no_finding(
    pytester: pytest.Pytester,
) -> None:
    # The base class's rows were read, but no instance had Manager.reports
    # to read: like a query that returns no rows (nplusone #36).
    body = """
def test_it():
    assert len(org_chart(staff_session(managers=0), read_reports=True)) == 4
"""
    run(pytester, body, *ON, head=INHERITANCE_HEAD).assert_outcomes(passed=1)


def test_a_subclass_relationship_read_by_no_subclass_row_is_reported(
    pytester: pytest.Pytester,
) -> None:
    body = """
def test_it():
    assert len(org_chart(staff_session(managers=2), read_reports=False)) == 6
"""
    result = run(pytester, body, *ON, head=INHERITANCE_HEAD)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*Manager.reports was loaded with selectinload and never read*",
            "*first at test_app.py:*in org_chart*",
        ]
    )
