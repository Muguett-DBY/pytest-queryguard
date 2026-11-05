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
        "not-loaded-step",
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
