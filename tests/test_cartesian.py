"""Cartesian loads: two collections joined side by side in one SELECT (#229)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import ForeignKey, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    contains_eager,
    defaultload,
    joinedload,
    lazyload,
    mapped_column,
    relationship,
    selectinload,
    subqueryload,
)

from pytest_queryguard.cartesian import _collections, label


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "cl_customer"
    id: Mapped[int] = mapped_column(primary_key=True)
    orders: Mapped[list[Order]] = relationship(back_populates="customer")
    notes: Mapped[list[Note]] = relationship()


class Order(Base):
    __tablename__ = "cl_order"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("cl_customer.id"))
    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list[Item]] = relationship()
    tags: Mapped[list[Tag]] = relationship()


class Note(Base):
    __tablename__ = "cl_note"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("cl_customer.id"))


class Item(Base):
    __tablename__ = "cl_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("cl_order.id"))


class Tag(Base):
    __tablename__ = "cl_tag"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("cl_order.id"))


def _found(statement: Any) -> str | None:
    result = _collections(statement._with_options)
    return None if result is None else label(result[1])


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        (
            select(Customer).options(
                joinedload(Customer.orders), joinedload(Customer.notes)
            ),
            "Customer.notes + Customer.orders",
        ),
        (
            select(Customer).options(
                joinedload(Customer.orders).joinedload(Order.items),
                joinedload(Customer.orders).joinedload(Order.tags),
            ),
            "Order.items + Order.tags",
        ),
        (
            # a product inside the second SELECT, the one of the selectinload
            select(Customer).options(
                selectinload(Customer.orders).joinedload(Order.items),
                selectinload(Customer.orders).joinedload(Order.tags),
            ),
            "Order.items + Order.tags",
        ),
        (
            select(Order).options(
                joinedload(Order.customer).joinedload(Customer.notes),
                joinedload(Order.items),
            ),
            "Customer.notes + Order.items",
        ),
        (
            # each lazy load of orders joins items and tags side by side
            select(Customer).options(
                defaultload(Customer.orders).joinedload(Order.items),
                defaultload(Customer.orders).joinedload(Order.tags),
            ),
            "Order.items + Order.tags",
        ),
        (
            select(Customer).options(
                lazyload(Customer.orders).joinedload(Order.items),
                lazyload(Customer.orders).joinedload(Order.tags),
            ),
            "Order.items + Order.tags",
        ),
        (
            # the product is orders x notes; items only chain below orders
            select(Customer).options(
                joinedload(Customer.orders).joinedload(Order.items),
                joinedload(Customer.notes),
            ),
            "Customer.notes + Customer.orders",
        ),
    ],
)
def test_collections_side_by_side_are_found(statement: Any, expected: str) -> None:
    assert _found(statement) == expected


@pytest.mark.parametrize(
    "statement",
    [
        select(Customer).options(joinedload(Customer.orders).joinedload(Order.items)),
        select(Customer).options(
            joinedload(Customer.orders), selectinload(Customer.notes)
        ),
        select(Customer).options(
            joinedload(Customer.orders), subqueryload(Customer.notes)
        ),
        select(Customer).options(
            selectinload(Customer.orders).joinedload(Order.items),
            joinedload(Customer.notes),
        ),
        select(Order).options(joinedload(Order.customer)),
        select(Customer).options(
            lazyload(Customer.orders).joinedload(Order.items),
            joinedload(Customer.notes),
        ),
        select(Customer).options(joinedload(Customer.orders).joinedload("*")),
        select(Customer, Order).options(
            joinedload(Customer.notes), joinedload(Order.items)
        ),
        select(Customer)
        .join(Customer.orders)
        .join(Customer.notes)
        .options(contains_eager(Customer.orders), contains_eager(Customer.notes)),
        select(Customer),
    ],
    ids=[
        "chain",
        "joined-and-selectin",
        "joined-and-subquery",
        "different-statements",
        "scalar",
        "lazy-step-and-joined",
        "wildcard",
        "two-entities",
        "contains-eager",
        "no-options",
    ],
)
def test_no_product_is_not_reported(statement: Any) -> None:
    assert _found(statement) is None


SUITE = """
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.pool import StaticPool

from cmodels import Base, Customer, Note, Order


@pytest.fixture
def session():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Customer(id=1, orders=[Order(id=n) for n in range(20)],
                             notes=[Note(id=n) for n in range(20)]))
        session.commit()
        yield session


def test_product(session):
    statement = select(Customer).options(
        joinedload(Customer.orders), joinedload(Customer.notes)
    )
    rows = session.execute(statement).unique().scalars().all()  # product
    assert len(rows[0].orders) == 20 and len(rows[0].notes) == 20


def test_split(session):
    statement = select(Customer).options(
        joinedload(Customer.orders), selectinload(Customer.notes)
    )
    session.execute(statement).unique().scalars().all()
"""


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    source = Path(__file__).read_text(encoding="utf-8")
    models = source[source.index("class Base(") : source.index("def _found(")]
    header = (
        "from __future__ import annotations\n"
        "from sqlalchemy import ForeignKey\n"
        "from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship\n\n"
    )
    pytester.makepyfile(cmodels=header + models, test_app=SUITE)
    return pytester


def test_it_warns_by_default_with_the_fix(project: pytest.Pytester) -> None:
    result = project.runpytest_subprocess("-p", "no:cacheprovider", "-W", "default")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(
        [
            "*queryguard: cartesian join detected*",
            "*Customer.notes and Customer.orders were joined into one query side by side, once*",
            "*first at test_app.py:* in test_product*",
            "*kind: cartesian-load  label: Customer.notes + Customer.orders*",
            "*select(Customer).options(joinedload(Customer.notes), selectinload(Customer.orders))*",
        ]
    )
    assert result.stdout.str().count("cartesian join detected") == 1


def test_fail_fails_the_test(project: pytest.Pytester) -> None:
    project.makeini("[pytest]\nqueryguard_cartesian_load = fail\n")
    result = project.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["FAILED test_app.py::test_product*"])


def test_off_and_ignore_silence_it(project: pytest.Pytester) -> None:
    for ini in (
        "queryguard_cartesian_load = off",
        (
            "queryguard_cartesian_load = fail\n"
            "queryguard_ignore = cartesian-load Customer.notes + Customer.orders"
        ),
    ):
        project.makeini(f"[pytest]\n{ini}\n")
        result = project.runpytest_subprocess("-p", "no:cacheprovider", "-W", "default")
        result.assert_outcomes(passed=2)
        assert "cartesian" not in result.stdout.str()


def test_the_fix_names_the_loaders_on_the_way() -> None:
    from pytest_queryguard.cartesian import _option

    found = _collections(
        select(Customer)
        .options(
            selectinload(Customer.orders).joinedload(Order.items),
            selectinload(Customer.orders).joinedload(Order.tags),
        )
        ._with_options
    )
    assert found is not None
    keep, split = found[1]
    assert _option(keep, "joinedload") == (
        "selectinload(Customer.orders).joinedload(Order.items)"
    )
    assert _option(split, "selectinload") == (
        "selectinload(Customer.orders).selectinload(Order.tags)"
    )


BROKEN = """
from sqlalchemy.orm import joinedload
from qgmodels import Order, make_sync_session


def test_wildcard_and_nplusone():
    session = make_sync_session()
    session.query(Order).options(joinedload("*")).all()
    session.expunge_all()
    for order in session.query(Order).all():
        _ = order.customer.name
"""


def test_a_wildcard_does_not_stop_the_measurement(pytester: pytest.Pytester) -> None:
    models = (Path(__file__).parent / "models.py").read_text()
    pytester.makepyfile(qgmodels=models, test_w=BROKEN)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Order.customer was lazy-loaded*"])
    assert "QueryGuardInternalWarning" not in result.stdout.str()


def test_an_error_in_the_check_turns_off_only_the_check(
    pytester: pytest.Pytester,
) -> None:
    models = (Path(__file__).parent / "models.py").read_text()
    pytester.makepyfile(qgmodels=models, test_w=BROKEN)
    pytester.makeconftest(
        "import pytest_queryguard.cartesian as c\n"
        "def _boom(options):\n"
        "    raise RuntimeError('parser')\n"
        "c._collections = _boom\n"
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-W", "default")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*Order.customer was lazy-loaded*",
            "*disabled the cartesian-load check after an internal error*",
        ]
    )
    assert "measurement stopped" not in result.stdout.str()


# The bare marker keeps the outer run from reporting these loads and the block
# as stale.
@pytest.mark.allow_nplusone
def test_a_load_in_an_ignore_block_carries_the_block() -> None:
    # Recorded as the SELECT runs: the ignore() block active there is on the
    # event, as on the statement.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from pytest_queryguard import cartesian, ignore, listeners, record_queries

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    listeners.install()
    cartesian.enable()
    try:
        with Session(engine) as session, record_queries() as log:
            stmt = select(Customer).options(
                joinedload(Customer.orders), joinedload(Customer.notes)
            )
            session.scalars(stmt).unique().all()
            with ignore("cartesian-load") as block:
                session.scalars(stmt).unique().all()
    finally:
        cartesian.disable()
    outside, inside = log._collector.cartesian_loads
    assert outside.ignores == ()
    assert inside.ignores == (block,)
    assert log._collector.statements[-1].ignores == (block,)
