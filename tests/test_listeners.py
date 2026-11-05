from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pytest_queryguard import collector, listeners, plugin
from tests.models import Customer, Order, make_sync_session


@pytest.fixture(autouse=True)
def _isolated(request: pytest.FixtureRequest) -> Iterator[None]:
    # These tests drive the listeners by hand: switch the plugin's own
    # measurement of this session off (it is read when the test body starts).
    stash = request.config.stash
    saved = stash[plugin._settings]
    stash[plugin._settings] = plugin._Settings("off", 2, 5)
    listeners.install()
    collector._reset_for_tests()
    listeners._reset_for_tests()
    yield
    collector._reset_for_tests()
    listeners._reset_for_tests()
    stash[plugin._settings] = saved


@pytest.fixture
def session() -> Iterator[Session]:
    s = make_sync_session()
    yield s
    s.close()


@pytest.fixture
def rec() -> Iterator[collector.QueryCollector]:
    c = collector.QueryCollector()
    collector.push(c)
    yield c
    collector.pop(c)


def _orm_cursor(load_options: object, compile_options: object) -> SimpleNamespace:
    """Run the cursor listener for an ORM SELECT with chosen private attributes."""
    context = SimpleNamespace(
        execution_options=(
            {} if load_options is None else {"_sa_orm_load_options": load_options}
        ),
        invoked_statement=SimpleNamespace(
            _propagate_attrs={"compile_state_plugin": "orm"},
            _compile_options=compile_options,
        ),
    )
    listeners._on_cursor_execute(None, None, "SELECT 1", (), context, False)
    return context


def test_nothing_recorded_without_active_collector(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise RuntimeError("must not be called without a collector")

    monkeypatch.setattr(listeners, "find_callsite", boom)
    for order in session.scalars(select(Order)).all():
        _ = order.customer
    assert listeners.internal_error() is None
    assert collector.active() == ()
    monkeypatch.undo()

    c = collector.QueryCollector()
    collector.push(c)
    try:
        session.execute(select(Customer)).all()
    finally:
        collector.pop(c)
    assert len(c.statements) == 1


def test_lazy_load_recorded_with_relationship_name(
    session: Session, rec: collector.QueryCollector
) -> None:
    for order in session.scalars(select(Order)).all():
        _ = order.customer
    assert [e.relationship for e in rec.relationship_loads] == ["Order.customer"] * 3
    assert len({e.parent_key for e in rec.relationship_loads}) == 3
    assert rec.relationship_loads[0].callsite is not None


def test_statement_tagged_with_relationship(
    session: Session, rec: collector.QueryCollector
) -> None:
    order = session.scalars(select(Order)).first()
    assert order is not None
    _ = order.customer
    tagged = [s for s in rec.statements if s.relationship == "Order.customer"]
    assert len(tagged) == 1
    assert "customers" in tagged[0].sql.lower()


def test_pending_relationship_does_not_leak(
    session: Session, rec: collector.QueryCollector
) -> None:
    orders = session.scalars(select(Order).where(Order.customer_id == 1)).all()
    assert len(orders) == 2
    first_customer = orders[0].customer  # real lazy load
    assert len(rec.relationship_loads) == 1
    assert orders[1].customer is first_customer  # identity map, no load
    assert len(rec.relationship_loads) == 1
    session.execute(select(Customer))
    assert rec.statements[-1].relationship is None


def test_pop_removes_by_identity_not_equality(session: Session) -> None:
    outer, inner = collector.QueryCollector(), collector.QueryCollector()
    collector.push(outer)
    collector.push(inner)
    collector.pop(inner)
    try:
        session.execute(select(Customer)).all()
    finally:
        collector.pop(outer)
    assert len(outer.statements) == 1
    assert inner.statements == []


def test_nested_collectors_both_receive_events(session: Session) -> None:
    outer, inner = collector.QueryCollector(), collector.QueryCollector()
    collector.push(outer)
    collector.push(inner)
    try:
        session.execute(select(Customer)).all()
    finally:
        collector.pop(inner)
        collector.pop(outer)
    assert len(outer.statements) == 1
    assert len(inner.statements) == 1


@pytest.mark.filterwarnings(
    "ignore::pytest_queryguard.warnings.QueryGuardInternalWarning"
)
def test_listener_error_is_swallowed_and_disables(
    session: Session,
    rec: collector.QueryCollector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(listeners, "find_callsite", boom)
    assert listeners.internal_error() is None
    assert len(session.execute(select(Customer)).all()) == 3
    assert isinstance(listeners.internal_error(), RuntimeError)
    monkeypatch.undo()
    before = len(rec.statements)
    session.execute(select(Customer)).all()
    assert len(rec.statements) == before


def test_a_column_load_without_an_instance_is_ignored_quietly(
    rec: collector.QueryCollector,
) -> None:
    # no _refresh_state: an unknown SQLAlchemy shape
    _orm_cursor(object(), SimpleNamespace(_for_refresh_state=True))
    assert listeners.internal_error() is None
    assert rec.column_loads == []
    assert [s.column_load for s in rec.statements] == [False]


@pytest.mark.filterwarnings(
    "ignore::pytest_queryguard.warnings.QueryGuardInternalWarning"
)
def test_missing_only_load_props_is_an_internal_error_not_a_refresh(
    session: Session, rec: collector.QueryCollector
) -> None:
    from sqlalchemy import inspect

    customer = session.scalars(select(Customer)).first()
    assert customer is not None
    # the shape of a SQLAlchemy release that drops _only_load_props
    _orm_cursor(
        SimpleNamespace(_refresh_state=inspect(customer)),
        SimpleNamespace(_for_refresh_state=True),
    )
    error = listeners.internal_error()
    assert isinstance(error, AttributeError)
    assert "_only_load_props" in str(error)
    failure = listeners.internal_failure()
    assert failure is not None and "before_cursor_execute" in failure.source
    assert rec.column_loads == []  # no load was mislabeled as a refresh


def test_the_present_attribute_is_classified_as_before(
    session: Session, rec: collector.QueryCollector
) -> None:
    customer = session.scalars(select(Customer)).first()
    assert customer is not None
    session.expire(customer)
    assert customer.name
    assert listeners.internal_error() is None
    [load] = rec.column_loads
    assert load.cause == "expired"


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO t VALUES (1)",
        "insert into t values (1)",
        "  \n\tUpdate t set a = 1",
        " DELETE FROM t",
        "DELETE",
        "SELECT * FROM t",
        "  select 1",
        "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
        "INSER",
        "",
        "   ",
    ],
)
def test_is_write_matches_the_plain_definition(statement: str) -> None:
    from pytest_queryguard.listeners import _is_write

    expected = statement.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE")
    assert _is_write(statement) is expected


def test_a_column_load_is_read_from_the_options_of_its_select(
    session: Session, rec: collector.QueryCollector
) -> None:
    """The refresh state and the compile options are all the listener needs.

    Joined-inheritance subclass columns load through ``from_statement()``,
    for which ``ORMExecuteState`` reports no column load before SQLAlchemy
    2.0.30; the SELECT carries the same options either way.
    """
    from sqlalchemy import inspect

    customer = session.scalars(select(Customer)).first()
    assert customer is not None
    _orm_cursor(
        SimpleNamespace(_refresh_state=inspect(customer)),
        SimpleNamespace(_for_refresh_state=True, _only_load_props=frozenset({"name"})),
    )
    assert listeners.internal_error() is None
    assert [load.owner for load in rec.column_loads] == ["Customer"]


@pytest.mark.parametrize(
    "load_options, compile_options",
    [
        # SQLAlchemy drops the refresh flag: an ordinary statement again.
        (None, None),
        (None, object()),
        # The flag is there but the load options are not: nothing to attribute.
        (None, SimpleNamespace(_for_refresh_state=True)),
        (object(), SimpleNamespace(_for_refresh_state=True)),
    ],
)
def test_a_load_without_the_private_attributes_fails_open(
    rec: collector.QueryCollector,
    load_options: object,
    compile_options: object,
) -> None:
    """Missing private attributes record nothing and raise nothing."""
    _orm_cursor(load_options, compile_options)
    assert listeners.internal_error() is None
    assert rec.column_loads == []
    assert rec.relationship_loads == []
    assert [s.relationship for s in rec.statements] == [None]


def test_sample_sql_of_a_relationship_finding_is_the_lazy_load(
    session: Session, rec: collector.QueryCollector
) -> None:
    from sqlalchemy.orm import selectinload

    from pytest_queryguard.detectors import RelationshipFinding, detect

    # eager: one IN query for the first order, labelled "Order.customer" too
    session.scalars(
        select(Order).options(selectinload(Order.customer)).order_by(Order.id).limit(1)
    ).all()
    # lazy: one query per customer not loaded yet (the N+1)
    for order in session.scalars(select(Order).order_by(Order.id).offset(2)).all():
        _ = order.customer
    tagged = [s for s in rec.statements if s.relationship == "Order.customer"]
    assert [s.lazy_load for s in tagged] == [False, True, True]
    found = [
        f
        for f in detect(rec, relationship_threshold=2, repeat_threshold=50)
        if isinstance(f, RelationshipFinding)
    ]
    assert len(found) == 1
    sample = found[0].sample_sql
    assert sample is not None and " IN (" not in sample.upper()


@pytest.mark.filterwarnings(
    "ignore::pytest_queryguard.warnings.QueryGuardInternalWarning"
)
def test_after_begin_error_is_swallowed_and_disables(
    session: Session,
    rec: collector.QueryCollector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Any error, not only the AttributeError and TypeError of an object that
    # cannot carry the scope tag: the user's transaction must still begin.
    def boom(owner: object) -> int:
        raise RuntimeError("scope broke")

    monkeypatch.setattr(listeners, "scope_of_session", boom)
    assert len(session.execute(select(Customer)).all()) == 3
    error = listeners.internal_error()
    assert isinstance(error, RuntimeError)
    failure = listeners.internal_failure()
    assert failure is not None
    assert failure.source == "after_begin listener (session scopes)"


def test_after_begin_does_nothing_after_an_internal_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(listeners, "scope_of_session", calls.append)
    listeners.record_error(RuntimeError("earlier"))
    session.execute(select(Customer)).all()
    assert calls == []
