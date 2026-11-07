"""Where the parents of a lazy load were loaded, and the chained loader hint.

The listeners note on each instance the call site of the query that loaded it
and its load path; a relationship-load finding prints the site and builds the
loader option from the root of that path (issue #225).
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import ForeignKey, create_engine, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    aliased,
    composite,
    joinedload,
    mapped_column,
    relationship,
    selectinload,
    subqueryload,
)
from sqlalchemy.pool import StaticPool

from pytest_queryguard import collector, listeners, plugin
from pytest_queryguard.detectors import RelationshipFinding, detect
from pytest_queryguard.events import CallSite
from pytest_queryguard.messages import format_findings


class Base(DeclarativeBase):
    pass


class Country(Base):
    __tablename__ = "ps_country"

    id: Mapped[int] = mapped_column(primary_key=True)


class Customer(Base):
    __tablename__ = "ps_customer"

    id: Mapped[int] = mapped_column(primary_key=True)
    country_id: Mapped[int] = mapped_column(ForeignKey("ps_country.id"))
    country: Mapped[Country] = relationship()
    orders: Mapped[list[Order]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "ps_order"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("ps_customer.id"))
    customer: Mapped[Customer] = relationship(back_populates="orders")


@dataclasses.dataclass
class Point:
    x: int
    y: int


class Vertex(Base):
    __tablename__ = "ps_vertex"

    id: Mapped[int] = mapped_column(primary_key=True)
    x: Mapped[int]
    y: Mapped[int]
    p: Mapped[Point] = composite("x", "y")


@pytest.fixture(autouse=True)
def _isolated(request: pytest.FixtureRequest) -> Iterator[None]:
    # The listeners are driven by hand here, as in test_listeners.py.
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


def _seed(session: Session, first: int = 1) -> None:
    for i in range(first, first + 3):
        session.add(Order(id=i, customer=Customer(id=i, country=Country(id=i))))


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed(session)
        session.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def rec() -> Iterator[collector.QueryCollector]:
    c = collector.QueryCollector()
    collector.push(c)
    yield c
    collector.pop(c)


def _finding(rec: collector.QueryCollector, label: str) -> RelationshipFinding:
    for found in detect(rec, relationship_threshold=2, repeat_threshold=99):
        if isinstance(found, RelationshipFinding) and found.relationship == label:
            return found
    raise AssertionError(f"no finding for {label}")


def _line(marker: str) -> int:
    """The line of this file that ends with ``# <marker>``."""
    with open(__file__, encoding="utf-8") as source:
        for number, text in enumerate(source, start=1):
            if text.rstrip().endswith(f"# {marker}"):
                return number
    raise AssertionError(marker)


def _hint(finding: RelationshipFinding) -> str:
    return format_findings([finding]).splitlines()[-1].strip()


def _base_query(session: Session) -> Iterator[Order]:
    statement = select(Order).options(joinedload(Order.customer))
    return iter(session.scalars(statement))  # helper-query


def test_a_query_built_in_a_helper_is_named_with_the_chained_loader(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    with Session(engine) as session:
        for order in _base_query(session):
            _ = order.customer.country
    finding = _finding(rec, "Customer.country")
    assert finding.parent_site is not None
    assert finding.parent_site.function == "_base_query"
    assert finding.parent_site.lineno == _line("helper-query")
    assert finding.other_parent_sites == 0
    assert _hint(finding) == (
        "select(Order).options(joinedload(Order.customer).joinedload(Customer.country))"
    )
    message = format_findings([finding], cwd=Path(__file__).parent)
    where = f"test_parent_site.py:{_line('helper-query')} in _base_query"
    assert f"  parents loaded at {where}\n" in message
    assert listeners.internal_error() is None


def test_the_execute_line_is_named_not_the_line_that_reads_the_rows(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    with Session(engine) as session:
        result = session.execute(select(Order))  # executed
        session.execute(select(Customer)).all()
        for order in result.scalars():
            _ = order.customer
    finding = _finding(rec, "Order.customer")
    assert finding.parent_site is not None
    assert finding.parent_site.lineno == _line("executed")
    assert _hint(finding) == "select(Order).options(joinedload(Order.customer))"


@pytest.mark.parametrize("loader", [selectinload, subqueryload, joinedload])
def test_parents_loaded_by_an_eager_loader_have_the_outer_query_site(
    engine: Engine, rec: collector.QueryCollector, loader: object
) -> None:
    with Session(engine) as session:
        statement = select(Order).options(loader(Order.customer))  # type: ignore[operator]
        for order in session.scalars(statement).all():  # outer
            _ = order.customer.country
    finding = _finding(rec, "Customer.country")
    assert finding.parent_site is not None
    assert finding.parent_site.lineno == _line("outer")
    assert finding.parent_root == "Order"
    assert finding.parent_path == (("Order.customer", True),)


def test_parents_loaded_lazily_point_to_the_query_at_the_start_of_the_chain(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    with Session(engine) as session:
        orders = session.scalars(select(Order)).all()  # root
        for order in orders:
            _ = order.customer.country
    nested = _finding(rec, "Customer.country")
    assert nested.parent_site is not None
    assert nested.parent_site.lineno == _line("root")
    assert _hint(nested) == (
        "select(Order).options(joinedload(Order.customer).joinedload(Customer.country))"
    )
    # The lazy load of Order.customer itself is a finding of its own.
    assert _hint(_finding(rec, "Order.customer")) == (
        "select(Order).options(joinedload(Order.customer))"
    )


def test_session_get_parents_name_the_get_call(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    with Session(engine) as session:
        for key in (1, 2, 3):
            _ = session.get(Order, key).customer  # type: ignore[union-attr]  # get
    finding = _finding(rec, "Order.customer")
    assert finding.parent_site is not None
    assert finding.parent_site.lineno == _line("get")
    assert _hint(finding) == "select(Order).options(joinedload(Order.customer))"


def test_a_parent_from_the_identity_map_keeps_the_query_that_loaded_it(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    with Session(engine) as session:
        orders = session.scalars(select(Order)).all()  # first-load
        # session.get() finds them in the identity map: no statement, no event.
        again = [session.get(Order, order.id) for order in orders]
        for order in again:
            _ = order.customer  # type: ignore[union-attr]
    finding = _finding(rec, "Order.customer")
    assert finding.parent_site is not None
    assert finding.parent_site.lineno == _line("first-load")


def test_objects_created_in_the_test_and_reread_by_a_query_get_its_site(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    with Session(engine) as session:
        orders = [
            Order(id=10 + i, customer=Customer(id=10 + i, country_id=i + 1))
            for i in range(3)
        ]
        session.add_all(orders)
        session.flush()
        session.expire_all()
        statement = (
            select(Order).where(Order.id >= 10).options(joinedload(Order.customer))
        )
        for order in session.scalars(statement).unique():  # reread
            _ = order.customer.country
        session.rollback()
    finding = _finding(rec, "Customer.country")
    assert finding.parent_site is not None
    assert finding.parent_site.lineno == _line("reread")
    assert finding.parent_path == (("Order.customer", True),)


def test_objects_created_in_the_test_and_never_loaded_fall_back(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    with Session(engine) as session:
        orders = [
            Order(id=10 + i, customer=Customer(id=10 + i, country_id=i + 1))
            for i in range(3)
        ]
        session.add_all(orders)
        session.flush()
        # Neither expired nor loaded: the query hands back the same objects
        # without reading them, so nothing says where they came from.
        for order in session.scalars(select(Order).where(Order.id >= 10)):
            _ = order.customer.country
        session.rollback()
    finding = _finding(rec, "Customer.country")
    assert finding.parent_site is None
    assert finding.parent_root is None
    message = format_findings([finding])
    assert "parents loaded at" not in message
    assert _hint(finding) == "select(Customer).options(joinedload(Customer.country))"


def test_parents_loaded_outside_the_measurement_fall_back(engine: Engine) -> None:
    with Session(engine) as session:
        orders = session.scalars(select(Order)).all()
        rec = collector.QueryCollector()
        collector.push(rec)
        try:
            for order in orders:
                _ = order.customer
        finally:
            collector.pop(rec)
    finding = _finding(rec, "Order.customer")
    assert finding.parent_site is None
    assert finding.parent_root is None


def test_an_aliased_root_gives_no_chain(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    alias = aliased(Order)
    with Session(engine) as session:
        for order in session.scalars(select(alias)).all():  # aliased
            _ = order.customer
    finding = _finding(rec, "Order.customer")
    assert finding.parent_site is not None
    assert finding.parent_site.lineno == _line("aliased")
    assert finding.parent_root is None
    assert _hint(finding) == "select(Order).options(joinedload(Order.customer))"


def test_parents_from_two_queries_name_the_first_and_count_the_other(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    with Session(engine) as session:
        _seed(session, first=4)
        session.commit()
        first = session.scalars(select(Order).where(Order.id < 3)).all()  # two-1
        second = session.scalars(select(Order).where(Order.id > 3)).all()
        for order in [*first, *second]:
            _ = order.customer
    finding = _finding(rec, "Order.customer")
    assert finding.count == 5
    assert finding.parent_site is not None
    assert finding.parent_site.lineno == _line("two-1")
    assert finding.other_parent_sites == 1
    assert "and 1 other place\n" in format_findings([finding]) + "\n"


def _relationship_findings(rec: collector.QueryCollector) -> list[RelationshipFinding]:
    found = detect(rec, relationship_threshold=2, repeat_threshold=99)
    return [f for f in found if isinstance(f, RelationshipFinding)]


def test_two_single_object_lookups_are_not_an_n_plus_one(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    # #307: one parent per query, no loop over rows
    with Session(engine) as session:
        left = session.get(Order, 1)
        right = session.scalars(select(Order).where(Order.id == 2)).one()
        assert left is not None
        _ = left.customer, right.customer
    assert rec.relationship_load_count == 2
    assert _relationship_findings(rec) == []


def test_a_loop_next_to_a_single_lookup_counts_the_loop_only(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    with Session(engine) as session:
        single = session.scalars(select(Order).where(Order.id == 1)).one()
        _ = single.customer
        for order in session.scalars(select(Order).where(Order.id > 1)):  # loop
            _ = order.customer
    finding = _finding(rec, "Order.customer")
    assert finding.count == 2
    assert finding.parent_site is not None
    assert finding.parent_site.lineno == _line("loop")
    assert finding.other_parent_sites == 0


def test_a_parent_of_unknown_origin_counts_with_every_other_parent(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    # One order the test made (no query read its row) and one loaded by a
    # query: which query the made one belongs to cannot be told, so the two
    # count together, as the loop over them is an N+1.
    with Session(engine) as session:
        made = Order(id=10, customer_id=2)  # kept: the identity map is weak
        session.add(made)
        session.flush()
        loaded = session.get(Order, 1)
        for order in (made, loaded):
            _ = order.customer
        session.rollback()
    finding = _finding(rec, "Order.customer")
    assert finding.count == 2
    assert finding.parent_site is None


def test_objects_made_in_the_test_and_looked_up_one_by_one_are_reported(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    # session.get() finds them in the identity map: no query, no origin
    with Session(engine) as session:
        made = [Order(id=20 + i, customer_id=i + 1) for i in range(3)]
        session.add_all(made)
        session.flush()
        first = session.get(Order, 20)
        second = session.get(Order, 21)
        assert first is not None and second is not None
        _ = first.customer, second.customer
        session.rollback()
    finding = _finding(rec, "Order.customer")
    assert finding.count == 2
    assert finding.parent_site is None


def test_async_session_names_the_awaiting_line() -> None:
    pytest.importorskip("aiosqlite")
    pytest.importorskip("greenlet")
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    async def scenario() -> collector.QueryCollector:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as session:
            _seed(session)
            await session.commit()
        rec = collector.QueryCollector()
        collector.push(rec)
        try:
            async with AsyncSession(engine) as session:
                statement = select(Order).options(joinedload(Order.customer))
                orders = (await session.scalars(statement)).all()  # awaited
                for order in orders:
                    await session.run_sync(lambda _, o=order: o.customer.country)
        finally:
            collector.pop(rec)
            await engine.dispose()
        return rec

    rec = asyncio.run(scenario())
    finding = _finding(rec, "Customer.country")
    assert finding.parent_site is not None
    assert finding.parent_site.lineno == _line("awaited")
    assert _hint(finding) == (
        "select(Order).options(joinedload(Order.customer).joinedload(Customer.country))"
    )


def test_a_failing_origin_lookup_stops_measurement_and_keeps_the_test(
    engine: Engine, rec: collector.QueryCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(context: object) -> CallSite | None:
        raise RuntimeError("boom")

    monkeypatch.setattr(listeners, "_query_site", boom)
    with Session(engine) as session:
        assert len(session.scalars(select(Order)).all()) == 3
    failure = listeners.internal_failure()
    assert failure is not None
    assert failure.source == "load listener (parent sites)"


def test_a_bulk_update_that_synchronizes_the_session_is_not_an_origin(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    # The ORM fires the refresh event without a query context for it.
    with Session(engine) as session:
        orders = session.scalars(select(Order)).all()  # before-update
        session.execute(update(Order).values(customer_id=Order.customer_id))
        for order in orders:
            _ = order.customer
    assert listeners.internal_error() is None
    finding = _finding(rec, "Order.customer")
    assert finding.parent_site is not None
    assert finding.parent_site.lineno == _line("before-update")


def test_a_merged_new_object_does_not_stop_the_measurement(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    # Session.merge() fires the load event without a query context.
    with Session(engine) as session:
        session.merge(Country(id=99))
    assert listeners.internal_error() is None


def test_a_composite_attribute_does_not_stop_the_measurement(
    rec: collector.QueryCollector,
) -> None:
    # Reading a composite after a flush fires refresh with a marker object.
    cengine = create_engine("sqlite://")
    Base.metadata.create_all(cengine)
    with Session(cengine) as session:
        vertex = Vertex(id=1, p=Point(1, 2))
        session.add(vertex)
        session.flush()
        vertex.p = Point(7, 8)
        session.flush()
        assert vertex.p == Point(7, 8)
    cengine.dispose()
    assert listeners.internal_error() is None


def test_parents_that_two_queries_read_have_no_known_origin(
    engine: Engine, rec: collector.QueryCollector
) -> None:
    with Session(engine) as session:
        orders = session.scalars(select(Order)).all()
        # Reads the same rows again; the lazy relationship is unloaded, so the
        # ORM fires refresh. Which result the loop uses cannot be told.
        session.scalars(select(Order).where(Order.id > 0)).all()
        for order in orders:
            _ = order.customer
    finding = _finding(rec, "Order.customer")
    assert finding.parent_site is None
    assert finding.parent_root is None


def test_an_origin_noted_in_an_earlier_measurement_is_not_used(
    engine: Engine,
) -> None:
    with Session(engine) as session:
        first = collector.QueryCollector()
        collector.push(first)
        try:
            orders = session.scalars(select(Order)).all()
        finally:
            collector.pop(first)
        rec = collector.QueryCollector()
        collector.push(rec)
        try:
            for order in orders:
                _ = order.customer
        finally:
            collector.pop(rec)
    finding = _finding(rec, "Order.customer")
    assert finding.parent_site is None


def test_a_read_noted_in_an_earlier_measurement_does_not_name_the_parents(
    engine: Engine,
) -> None:
    # The same statement object ran in an earlier measurement; this time its
    # result was made before the measurement and its rows become instances in
    # it. The earlier run's line is not where these parents came from.
    statement = select(Order)
    with Session(engine) as session:
        first = collector.QueryCollector()
        collector.push(first)
        try:
            session.scalars(statement).all()
        finally:
            collector.pop(first)
        session.expunge_all()
        pending = session.scalars(statement)  # not measured
        rec = collector.QueryCollector()
        collector.push(rec)
        try:
            for order in pending:
                _ = order.customer
        finally:
            collector.pop(rec)
    finding = _finding(rec, "Order.customer")
    assert finding.parent_site is None


def test_a_new_select_per_iteration_keeps_the_site_once_the_map_is_full(
    engine: Engine, rec: collector.QueryCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without the stack walk only the noted reads can name the site.
    monkeypatch.setattr(listeners, "_CHUNKS_CODE", None)
    with Session(engine) as session:
        for _ in range(listeners._MAX_READS + 10):
            session.scalars(select(Order).where(Order.id == 1)).all()
            session.expunge_all()
        for order in session.scalars(select(Order)).all():  # past-the-cap
            _ = order.customer
    assert len(listeners._reads) == listeners._MAX_READS
    finding = _finding(rec, "Order.customer")
    assert finding.parent_site is not None
    assert finding.parent_site.lineno == _line("past-the-cap")
