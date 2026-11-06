"""The experimental static check, python -m pytest_queryguard.lint."""

from __future__ import annotations

import json
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from pytest_queryguard import lint

MODELS = """
from __future__ import annotations

from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Country(Base):
    __tablename__ = "countries"
    id: Mapped[int] = mapped_column(primary_key=True)


class Address(Base):
    __tablename__ = "addresses"
    id: Mapped[int] = mapped_column(primary_key=True)
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id"))
    country: Mapped[Country] = relationship()


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    address_id: Mapped[int] = mapped_column(ForeignKey("addresses.id"))
    address: Mapped[Address] = relationship()
    orders: Mapped[list[Order]] = relationship(back_populates="customer")


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list[Item]] = relationship()
    notes: Mapped[list[Note]] = relationship(lazy="selectin")
    audits: Mapped[list[Audit]] = relationship(lazy="raise")
    events: Mapped[list[Event]] = relationship(lazy="dynamic")
    payer: Mapped[Customer] = relationship(lazy="joined", foreign_keys=[customer_id], viewonly=True)


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    product: Mapped[Product] = relationship()


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))


class Audit(Base):
    __tablename__ = "audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
"""

MODULE = "queryguard_lint_models"


@pytest.fixture(scope="module")
def models(tmp_path_factory: pytest.TempPathFactory) -> Iterator[lint.Models]:
    root = tmp_path_factory.mktemp("models")
    (root / f"{MODULE}.py").write_text(MODELS, encoding="utf-8")
    sys.path.insert(0, str(root))
    try:
        yield lint.load_models([MODULE])
    finally:
        sys.path.remove(str(root))


HEADER = f"""\
from sqlalchemy import select
from sqlalchemy.orm import (
    Load, contains_eager, defaultload, joinedload, lazyload, load_only, raiseload,
    selectinload,
)
from {MODULE} import Customer, Order
"""


def found(models: lint.Models, code: str) -> list[str]:
    source = HEADER + textwrap.dedent(code)
    return [f.relationship for f in lint.lint_source(source, "app.py", models)]


# --- what is reported ---


@pytest.mark.parametrize(
    "query",
    [
        "session.scalars(select(Order)).all()",
        "session.scalars(select(Order))",
        "session.scalars(select(Order).where(Order.id > 1).order_by(Order.id))",
        "session.execute(select(Order)).scalars().all()",
        "session.execute(select(Order)).scalars()",
        "session.scalars(select(Order)).unique().all()",
        "session.query(Order).filter(Order.id > 1).all()",
        "session.query(Order).filter_by(id=1)",
        "db.session.query(Order).order_by(Order.id).limit(10)",
        "list(session.scalars(select(Order)))",
        "Order.query.filter_by(id=1).all()",
        "session.scalars(sa.select(Order))",
        "session.scalars(select(Order).options(load_only(Order.id)))",
    ],
)
def test_a_lazy_many_to_one_read_in_a_loop_over_a_query(
    models: lint.Models, query: str
) -> None:
    code = f"""
    def names(session, db):
        out = []
        for order in {query}:
            out.append(order.customer.name)
        return out
    """
    assert found(models, code) == ["Order.customer"]


def test_the_finding_has_the_lines_and_the_fix(models: lint.Models) -> None:
    source = HEADER + textwrap.dedent(
        """
        def names(session):
            rows = session.scalars(select(Order)).all()
            for order in rows:
                print(order.customer.name)
        """
    )
    (finding,) = lint.lint_source(source, "app.py", models)
    head = len(HEADER.splitlines())
    assert finding.line == head + 5
    assert finding.column == len("        print(order.") + 1
    assert finding.loop_line == head + 4
    assert finding.query_line == head + 3
    assert finding.function == "names"
    assert finding.fix == "select(Order).options(joinedload(Order.customer))"


def test_a_collection_gets_selectinload(models: lint.Models) -> None:
    source = HEADER + textwrap.dedent(
        """
        for customer in session.scalars(select(Customer)):
            print(len(customer.orders))
        """
    )
    (finding,) = lint.lint_source(source, "app.py", models)
    assert finding.function == "<module>"
    assert finding.fix == "select(Customer).options(selectinload(Customer.orders))"


def test_the_statement_and_the_result_may_be_variables(models: lint.Models) -> None:
    code = """
    def f(session):
        stmt = select(Order).where(Order.id > 0)
        result = session.execute(stmt)
        orders: list[Order] = result.scalars().all()
        for i, order in enumerate(orders):
            order.items
    """
    assert found(models, code) == ["Order.items"]


@pytest.mark.parametrize(
    "comprehension",
    [
        "[o.customer.name for o in session.scalars(select(Order))]",
        "{o.id: o.customer for o in session.scalars(select(Order))}",
        "set(o.customer for o in session.scalars(select(Order)) if o.id)",
        "[o.id for o in session.scalars(select(Order)) if o.customer.name]",
    ],
)
def test_comprehensions(models: lint.Models, comprehension: str) -> None:
    assert found(models, f"def f(session):\n    return {comprehension}\n") == [
        "Order.customer"
    ]


def test_async_sessions(models: lint.Models) -> None:
    code = """
    async def f(session):
        result = await session.scalars(select(Order))
        for order in result:
            order.customer
        for order in (await session.execute(select(Order))).scalars().all():
            order.items
        async for order in await session.stream_scalars(select(Customer)):
            await order.awaitable_attrs.orders
    """
    assert found(models, code) == ["Order.customer", "Order.items", "Customer.orders"]


def test_each_step_of_a_chain_is_reported(models: lint.Models) -> None:
    source = HEADER + textwrap.dedent(
        """
        for order in session.scalars(select(Order)):
            print(order.customer.address.country.id)
        """
    )
    findings = lint.lint_source(source, "app.py", models)
    assert [f.relationship for f in findings] == [
        "Order.customer",
        "Customer.address",
        "Address.country",
    ]
    assert findings[-1].fix == (
        "select(Order).options(joinedload(Order.customer)"
        ".joinedload(Customer.address).joinedload(Address.country))"
    )


def test_a_step_after_a_loaded_one(models: lint.Models) -> None:
    code = """
    for order in session.scalars(select(Order).options(joinedload(Order.customer))):
        print(order.customer.address)
    """
    assert found(models, code) == ["Customer.address"]


def test_a_nested_loop_over_a_collection(models: lint.Models) -> None:
    source = HEADER + textwrap.dedent(
        """
        for order in session.scalars(select(Order)):
            for item in order.items:
                print(item.product)
        """
    )
    findings = lint.lint_source(source, "app.py", models)
    assert [f.relationship for f in findings] == ["Order.items", "Item.product"]
    assert findings[1].fix == (
        "select(Order).options(selectinload(Order.items).joinedload(Item.product))"
    )
    assert findings[1].loop_line == findings[0].line


def test_a_nested_loop_over_a_loaded_collection(models: lint.Models) -> None:
    code = """
    for order in session.scalars(select(Order).options(selectinload(Order.items))):
        for item in order.items:
            print(item.product)
    """
    assert found(models, code) == ["Item.product"]


def test_lazyload_and_defaultload_keep_it_lazy(models: lint.Models) -> None:
    code = """
    stmt = select(Order).options(lazyload(Order.customer), defaultload(Order.items))
    for order in session.scalars(stmt):
        order.customer
        order.items
    """
    assert found(models, code) == ["Order.customer", "Order.items"]


def test_one_finding_per_relationship_and_loop(models: lint.Models) -> None:
    code = """
    for order in session.scalars(select(Order)):
        a = order.customer.name
        b = order.customer.id
    """
    assert found(models, code) == ["Order.customer"]


def test_a_return_in_a_nested_branch_only_skips_that_branch(
    models: lint.Models,
) -> None:
    code = """
    def f(session):
        for order in session.scalars(select(Order)):
            if order.customer.name == "x":
                return order.items
    """
    assert found(models, code) == ["Order.customer"]


# --- what is not reported ---


@pytest.mark.parametrize(
    "option",
    [
        "selectinload(Order.customer)",
        "joinedload(Order.customer, innerjoin=True)",
        "raiseload(Order.customer)",
        'raiseload("*")',
        "contains_eager(Order.customer)",
        "joinedload(Order.customer).load_only(Customer.name)",
        "selectinload(Order.customer.of_type(Customer))",
        "*options",
        "my_options()",
        "opts",
    ],
)
def test_an_option_that_loads_it_or_is_not_understood(
    models: lint.Models, option: str
) -> None:
    code = f"""
    def f(session, options, opts):
        for order in session.scalars(select(Order).options({option})):
            order.customer
    """
    assert found(models, code) == []


def test_relationships_that_are_not_lazy_select(models: lint.Models) -> None:
    code = """
    for order in session.scalars(select(Order)):
        order.notes, order.audits, order.events.all(), order.payer
        order.customer_id
    """
    assert found(models, code) == []


@pytest.mark.parametrize(
    "code",
    [
        # not a query
        "def f(orders):\n    for o in orders:\n        o.customer\n",
        "for o in session.get(Order, 1).items:\n    o.customer\n",
        # rows, more than one entity, a single object
        "for o in session.execute(select(Order)):\n    o.customer\n",
        "for o in session.scalars(select(Order, Customer)):\n    o.customer\n",
        "for o in [session.scalars(select(Order)).first()]:\n    o.customer\n",
        # the statement comes from elsewhere
        (
            "def f(session, stmt):\n    for o in session.scalars(stmt):\n"
            "        o.customer\n"
        ),
        # assigned twice: may not be the query
        (
            "rows = session.scalars(select(Order)).all()\nrows = other()\n"
            "for o in rows:\n    o.customer\n"
        ),
        (
            "rows = session.scalars(select(Order)).all()\nrows += other()\n"
            "for o in rows:\n    o.customer\n"
        ),
        # assigned after the loop
        (
            "def f(session):\n    for o in rows:\n        o.customer\n"
            "    rows = session.scalars(select(Order)).all()\n"
        ),
        # the loop variable changes in the body
        (
            "for o in session.scalars(select(Order)):\n    o = o.items[0]\n"
            "    o.customer\n"
        ),
        # written, not read; in a function that may not run per row
        "for o in session.scalars(select(Order)):\n    o.customer = None\n",
        "for o in session.scalars(select(Order)):\n    f = lambda: o.customer\n",
        # runs once: the loop ends there
        "for o in session.scalars(select(Order)):\n    print(o.customer)\n    break\n",
        (
            "def f(session):\n    for o in session.scalars(select(Order)):\n"
            "        if o.id == 1:\n            return o.customer.name\n"
        ),
        (
            "for o in session.scalars(select(Order)):\n    if o.id:\n"
            "        x = o.customer\n        raise ValueError(x)\n"
        ),
        # not a relationship
        "for o in session.scalars(select(Order)):\n    o.query\n",
        # an unknown class, an aliased entity
        "for o in session.scalars(select(Unknown)):\n    o.customer\n",
        "for o in session.scalars(select(aliased(Order))):\n    o.customer\n",
    ],
)
def test_not_reported(models: lint.Models, code: str) -> None:
    assert found(models, code) == []


def test_a_break_in_a_nested_loop_still_counts(models: lint.Models) -> None:
    code = """
    for order in session.scalars(select(Order)):
        for item in order.items:
            print(order.customer)
            break
    """
    assert found(models, code) == ["Order.items", "Order.customer"]


@pytest.mark.parametrize(
    "code",
    [
        (
            "for o in session.scalars(select(Order)):\n"
            "    o.customer  # queryguard: ignore\n"
        ),
        (
            "for o in session.scalars(select(Order)):  # queryguard: ignore\n"
            "    o.customer\n    o.items\n"
        ),
        (
            "for o in session.scalars(select(Order)):\n"
            "    o.customer.name  # queryguard:ignore[relationship]\n"
            "    o.customer.id\n"
        ),
    ],
)
def test_suppression_comment(models: lint.Models, code: str) -> None:
    assert found(models, code) == []


def test_a_suppression_on_another_relationship_does_not_count(
    models: lint.Models,
) -> None:
    code = """
    for o in session.scalars(select(Order)):
        o.customer  # queryguard: ignore
        o.items
    """
    assert found(models, code) == ["Order.items"]


def test_a_class_name_used_twice_is_resolved_by_its_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("dup_a", "dup_b"):
        lazy = "select" if name == "dup_a" else "selectin"
        (tmp_path / f"{name}.py").write_text(
            textwrap.dedent(
                f"""
                from sqlalchemy import ForeignKey
                from sqlalchemy.orm import DeclarativeBase, Mapped
                from sqlalchemy.orm import mapped_column, relationship

                class Base(DeclarativeBase):
                    pass

                class Parent(Base):
                    __tablename__ = "parent"
                    id: Mapped[int] = mapped_column(primary_key=True)

                class Child(Base):
                    __tablename__ = "child"
                    id: Mapped[int] = mapped_column(primary_key=True)
                    parent_id: Mapped[int] = mapped_column(ForeignKey("parent.id"))
                    parent: Mapped[Parent] = relationship(lazy="{lazy}")
                """
            ),
            encoding="utf-8",
        )
    monkeypatch.syspath_prepend(str(tmp_path))
    models = lint.load_models(["dup_a", "dup_b"])
    loop = "for c in session.scalars(select(Child)):\n    c.parent\n"
    found_a = lint.lint_source(f"from dup_a import Child\n{loop}", "x.py", models)
    found_b = lint.lint_source(f"from dup_b import Child\n{loop}", "x.py", models)
    unknown = lint.lint_source(loop, "x.py", models)
    assert [f.relationship for f in found_a] == ["Child.parent"]
    assert found_b == unknown == []


# --- the command line ---


@pytest.fixture
def project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, models: lint.Models
) -> Path:
    del models  # imported once, by the fixture
    app = tmp_path / "app"
    app.mkdir()
    (app / "orders.py").write_text(
        HEADER + "\ndef f(session):\n    for o in session.scalars(select(Order)):\n"
        "        print(o.customer.name)\n",
        encoding="utf-8",
    )
    (app / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (app / "broken.py").write_text("def (:\n", encoding="utf-8")
    hidden = app / ".venv"
    hidden.mkdir()
    (hidden / "skipped.py").write_text(
        HEADER + "for o in session.scalars(select(Order)):\n    o.items\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_text_output_and_exit_status(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lint.main(["--models", MODULE, "app"]) == 1
    out, err = capsys.readouterr()
    assert out.startswith("app/orders.py:10:")
    assert "Order.customer is lazy-loaded once per row (N+1)" in out
    assert "rule: queryguard/static-relationship-load  label: Order.customer" in out
    assert "select(Order).options(joinedload(Order.customer))" in out
    assert out.endswith("1 finding in 3 files\n")
    assert "skipped app/broken.py" in err
    assert lint.main(["--models", MODULE, "app/ok.py"]) == 0
    assert "0 findings in 1 file\n" in capsys.readouterr().out


def test_json_output(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_file = project / "lint.json"
    args = ["--models", MODULE, "--format", "json", "--output", str(out_file), "app"]
    assert lint.main(args) == 1
    out = capsys.readouterr().out
    assert out == out_file.read_text(encoding="utf-8")
    data = json.loads(out)
    assert data["format"] == 1
    assert data["files"] == 3
    assert data["errors"][0].startswith("app/broken.py: ")
    assert data["findings"] == [
        {
            "rule": "queryguard/static-relationship-load",
            "file": "app/orders.py",
            "line": 10,
            "column": 17,
            "loop_line": 9,
            "query_line": 9,
            "function": "f",
            "relationship": "Order.customer",
            "path": ["Order.customer"],
            "query_class": "Order",
            "fix": "select(Order).options(joinedload(Order.customer))",
        }
    ]


def test_sarif_output(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_WORKSPACE", str(project.parent))
    assert lint.main(["--models", MODULE, "--format", "sarif", "app"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["version"] == "2.1.0"
    run = doc["runs"][0]
    assert [r["id"] for r in run["tool"]["driver"]["rules"]] == [lint.RULE_ID]
    (result,) = run["results"]
    assert result["ruleId"] == lint.RULE_ID
    assert result["level"] == "warning"
    location = result["locations"][0]["physicalLocation"]
    assert location["artifactLocation"]["uri"] == f"{project.name}/app/orders.py"
    assert location["region"] == {"startLine": 10, "startColumn": 17}
    assert "joinedload(Order.customer)" in result["message"]["text"]
    assert len(result["partialFingerprints"]["queryguardIdentity/v1"]) == 64


def test_errors(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert lint.main(["--models", "no_such_module_here", "app"]) == 2
    assert "cannot import no_such_module_here" in capsys.readouterr().err
    assert lint.main(["--models", "json", "app"]) == 2
    assert "no mapped classes found in json" in capsys.readouterr().err
    assert lint.main(["--models", MODULE, "missing"]) == 2
    assert "missing does not exist" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        lint.main(["app"])  # --models is required


def test_a_many_to_one_whose_targets_were_loaded_first(models: lint.Models) -> None:
    # the identity map answers order.customer; order.items still runs queries
    code = """
    def f(session):
        customers = session.scalars(select(Customer)).all()
        for order in session.scalars(select(Order)):
            order.customer, order.items
    """
    assert found(models, code) == ["Order.items"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "session.query(Order).options(joinedload(Order.customer))",
            ["Customer.address"],
        ),
        (
            "session.query(Order).options(lazyload(Order.customer))",
            ["Order.customer", "Customer.address"],
        ),
        (
            "session.scalars(select(Order).options(Load(Order).joinedload(Order.customer)))",
            ["Customer.address"],
        ),
        (
            (
                "session.scalars(select(Order).options(defaultload(Order.customer)"
                ".joinedload(Customer.address)))"
            ),
            ["Order.customer"],
        ),
        (
            (
                "session.scalars(select(Order).options(joinedload(Order.customer)"
                ".options(joinedload(Customer.address), load_only(Customer.id))))"
            ),
            [],
        ),
        (
            (
                "session.scalars(select(Order).options(joinedload(Order.customer)"
                ".options(lazyload(Customer.address))))"
            ),
            ["Customer.address"],
        ),
        (
            (
                "session.scalars(select(Order).options(joinedload(Order.customer)"
                ".options(helper())))"
            ),
            [],
        ),
        (
            (
                "session.scalars(select(Order).options("
                'defaultload(Order.customer).raiseload("*")))'
            ),
            ["Order.customer"],
        ),
        ("session.scalars(select(m.Order))", ["Order.customer", "Customer.address"]),
    ],
)
def test_loader_option_paths(
    models: lint.Models, query: str, expected: list[str]
) -> None:
    code = f"""
    import {MODULE} as m
    for order in {query}:
        order.customer.address
    """
    assert found(models, code) == expected


def test_a_nested_loop_through_a_many_to_one(models: lint.Models) -> None:
    code = """
    for order in session.scalars(select(Order).options(joinedload(Order.customer))):
        for other in order.customer.orders:
            other.items
        for x in order.customer.address.country:  # not a collection
            x.y
    """
    assert found(models, code) == [
        "Customer.orders",
        "Order.items",
        "Customer.address",
        "Address.country",
    ]


@pytest.mark.parametrize(
    "code",
    [
        "def f(*args, **kwargs):\n    for o in args:\n        o.customer\n",
        "def f(session):\n    global rows\n    for o in rows:\n        o.customer\n",
        "try:\n    pass\nexcept E as rows:\n    pass\nfor o in rows:\n    o.customer\n",
        "for (a, o) in zip(x, session.scalars(select(Order))):\n    o.customer\n",
        "for o in session.query(Order, Customer):\n    o.customer\n",
        (
            "for o in session.scalars(select(Order).union(select(Order))):\n"
            "    o.customer\n"
        ),
        "for o in session.scalars(select(Order)).first().items:\n    o.customer\n",
    ],
)
def test_more_that_is_not_reported(models: lint.Models, code: str) -> None:
    assert found(models, code) == []
