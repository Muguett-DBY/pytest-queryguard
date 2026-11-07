"""The patch that adds the loader option to the parent query (issue #247).

A patch is made only when the parent query line is certain; every case where
it is not gives none.
"""

from __future__ import annotations

import ast
import json
import ntpath
import os
import posixpath
import shutil
import subprocess
import sys
import textwrap
import time
import warnings
from pathlib import Path
from typing import Any

import pytest

from pytest_queryguard import fixpatch
from pytest_queryguard.detectors import RelationshipFinding
from pytest_queryguard.events import CallSite
from pytest_queryguard.findings import KIND_RELATIONSHIP, Finding, SourceFix
from pytest_queryguard.messages import format_findings

# pytester runs sessions in-process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone

_GIT = shutil.which("git")

HEADER = """\
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Customer, Order
"""


def _write(
    tmp_path: Path, body: str, header: str = HEADER, newline: str = "\n"
) -> Path:
    """The module, with the same line ends on every platform."""
    file = tmp_path / "app" / "views.py"
    file.parent.mkdir(exist_ok=True)
    text = header + "\n\n" + textwrap.dedent(body)
    file.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return file


def _line(file: Path, marker: str = "# parents") -> int:
    for number, text in enumerate(file.read_text().splitlines(), start=1):
        if text.rstrip().endswith(marker):
            return number
    raise AssertionError(marker)


def _finding(
    file: Path,
    *,
    relationship: str = "Order.customer",
    scalar: bool = True,
    root: str | None = "Order",
    path: tuple[tuple[str, bool], ...] = (),
    function: str = "load",
    line: int | None = None,
    others: int = 0,
) -> RelationshipFinding:
    site = CallSite(
        filename=str(file),
        lineno=_line(file) if line is None else line,
        function=function,
    )
    return RelationshipFinding(
        relationship,
        3,
        None,
        None,
        scalar,
        parent_site=site,
        other_parent_sites=others,
        parent_root=root,
        parent_path=path,
    )


def _make(tmp_path: Path, finding: RelationshipFinding) -> SourceFix | None:
    return fixpatch.make(finding, tmp_path, time.time() + 1)


def _patched(tmp_path: Path, fix: SourceFix) -> str:
    """The file after ``git apply`` of the fix; the patch must apply cleanly."""
    patch = tmp_path / "fix.patch"
    patch.write_bytes(fix.diff.encode("utf-8"))  # no newline translation
    return _git_apply(tmp_path, patch, fix.file)


def _git_apply(tmp_path: Path, patch: Path, file: str) -> str:
    if _GIT is None:
        pytest.skip("git is not installed")
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)}
    # Windows runners set core.autocrlf, which would rewrite the line ends
    plain = ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false"]
    for args in (["apply", "--check", str(patch)], ["apply", str(patch)]):
        subprocess.run(
            [_GIT, *plain, *args],
            cwd=tmp_path,
            check=True,
            env=env,
            capture_output=True,
        )
    return (tmp_path / file).read_text(encoding="utf-8")


def test_the_loader_and_its_import_go_on_the_select_of_the_parent_line(
    tmp_path: Path,
) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session: Session) -> list[Order]:
            orders = session.scalars(select(Order).where(Order.id > 0)).all()  # parents
            return orders
        """,
    )
    fix = _make(tmp_path, _finding(file))
    assert fix is not None
    assert fix.file == "app/views.py"
    assert fix.description == (
        "Load Order.customer with the parent query: joinedload(Order.customer)"
    )
    after = _patched(tmp_path, fix)
    assert "from sqlalchemy.orm import Session, joinedload\n" in after
    assert (
        "session.scalars(select(Order).options(joinedload(Order.customer))"
        ".where(Order.id > 0)).all()  # parents"
    ) in after
    assert fix.diff.startswith("--- a/app/views.py\n+++ b/app/views.py\n")


def test_a_collection_gets_selectinload(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.scalars(select(Customer)).all()  # parents
        """,
    )
    finding = _finding(
        file, relationship="Customer.orders", scalar=False, root="Customer"
    )
    fix = _make(tmp_path, finding)
    assert fix is not None
    after = _patched(tmp_path, fix)
    assert "select(Customer).options(selectinload(Customer.orders))" in after
    assert "from sqlalchemy.orm import Session, selectinload\n" in after


def test_existing_options_get_one_more_argument(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        from sqlalchemy.orm import selectinload


        def load(session):
            statement = None
            return session.scalars(
                select(Customer).options(selectinload(Customer.tags)).limit(3)  # parents
            ).all()
        """,
        header=HEADER.replace("import Session", "import Session, joinedload"),
    )
    fix = _make(
        tmp_path,
        _finding(file, relationship="Customer.country", root="Customer"),
    )
    assert fix is not None
    after = _patched(tmp_path, fix)
    assert (
        "select(Customer).options(selectinload(Customer.tags), "
        "joinedload(Customer.country)).limit(3)"
    ) in after
    # joinedload was imported already
    assert after.count("joinedload\n") == 1


def test_a_module_without_an_orm_import_gets_a_new_line(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            for order in session.query(Order).all():  # parents
                print(order.customer)
        """,
        header='"""Views."""\n\nfrom app.models import Customer, Order\n',
    )
    fix = _make(tmp_path, _finding(file))
    assert fix is not None
    after = _patched(tmp_path, fix)
    assert after.startswith(
        '"""Views."""\n\nfrom app.models import Customer, Order\n'
        "from sqlalchemy.orm import joinedload\n"
    )
    assert "session.query(Order).options(joinedload(Order.customer)).all()" in after


def test_a_chain_is_written_from_the_root_of_the_query(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.scalars(select(Order)).all()  # parents
        """,
    )
    finding = _finding(
        file,
        relationship="Customer.country",
        path=(("Order.customer", True),),
    )
    fix = _make(tmp_path, finding)
    assert fix is not None
    after = _patched(tmp_path, fix)
    assert (
        "select(Order).options(joinedload(Order.customer).joinedload(Customer.country))"
    ) in after


def test_a_method_of_the_result_is_not_another_query(tmp_path: Path) -> None:
    # .scalars() without an argument is Result.scalars(), not Session.scalars()
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.execute(select(Order)).scalars().all()  # parents
        """,
    )
    fix = _make(tmp_path, _finding(file))
    assert fix is not None
    after = _patched(tmp_path, fix)
    assert (
        "session.execute(select(Order).options(joinedload(Order.customer)))"
        ".scalars().all()"
    ) in after


def test_a_scalar_of_a_query_on_the_line_is_another_query(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            q = session.query(Customer.id)
            return session.scalars(select(Order).where(Order.id > q.scalar())).all()  # parents
        """,
    )
    assert _make(tmp_path, _finding(file)) is None


def test_a_statement_over_several_lines_is_found_from_any_of_them(
    tmp_path: Path,
) -> None:
    file = _write(
        tmp_path,
        """\
        async def load(session):
            result = await session.execute(  # parents
                select(Order)
                .where(Order.id > 0)
            )
            return result.scalars().all()
        """,
    )
    fix = _make(tmp_path, _finding(file))
    assert fix is not None
    after = _patched(tmp_path, fix)
    assert "        select(Order).options(joinedload(Order.customer))\n" in after


def test_columns_count_characters_and_utf16_units(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            name, orders = "\U0001f600ż", session.scalars(select(Order)).all()  # parents
            return orders
        """,
    )
    fix = _make(tmp_path, _finding(file))
    assert fix is not None
    (option,) = [e for e in fix.insertions if e.text.startswith(".options")]
    text = file.read_text(encoding="utf-8").splitlines()[option.line - 1]
    assert text[: option.column].endswith("select(Order)")
    # the emoji is one character and two UTF-16 code units
    assert option.utf16_column == option.column + 1
    _patched(tmp_path, fix)


@pytest.mark.parametrize(
    ("body", "header"),
    [
        pytest.param(
            """\
            def load(session):
                ids = select(Order.id)
                return session.scalars(select(Order).where(Order.id.in_(select(Customer.id)))).all()  # parents
            """,
            HEADER,
            id="two-selects-in-the-statement",
        ),
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(Customer)).all()  # parents
            """,
            HEADER,
            id="another-entity",
        ),
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(Order, Customer)).all()  # parents
            """,
            HEADER,
            id="two-entities",
        ),
        pytest.param(
            """\
            def load(session):
                return session.get(Order, 1)  # parents
            """,
            HEADER,
            id="session-get",
        ),
        pytest.param(
            """\
            def other(session):
                return session.scalars(select(Order)).all()  # parents
            """,
            HEADER,
            id="another-function",
        ),
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(Order).options(lazyload(Order.customer))).all()  # parents
            """,
            HEADER,
            id="an-option-for-the-relationship-already",
        ),
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(Order).options(x).options(y)).all()  # parents
            """,
            HEADER,
            id="two-options-calls",
        ),
        pytest.param(
            """\
            def load(session, Order):
                return session.scalars(select(Order)).all()  # parents
            """,
            HEADER,
            id="class-rebound-in-the-function",
        ),
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(Order)).all()  # parents
            """,
            HEADER + "from app.loaders import joinedload\n",
            id="loader-name-from-elsewhere",
        ),
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(Order)).all()  # parents
            """,
            HEADER.replace(
                "from sqlalchemy import select", "from app.db import select"
            ),
            id="select-not-from-sqlalchemy",
        ),
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(Order)).all()  # parents
            """,
            HEADER.replace("import Customer, Order", "import *"),
            id="star-import",
        ),
        pytest.param(
            """\
            def load(session, stmt, cached):
                return session.scalars(stmt if cached else select(Order)).all()  # parents
            """,
            HEADER,
            id="a-conditional-expression",
        ),
        pytest.param(
            """\
            def load(session, stmt):
                return [session.scalars(stmt).all(), select(Order)]  # parents
            """,
            HEADER,
            id="another-statement-runs-on-the-line",
        ),
        pytest.param(
            """\
            def load(session, other):
                match session.scalars(select(Order)).all():
                    case [first, *rest]:
                        return session.scalars(other).all()  # parents
            """,
            HEADER,
            id="a-line-in-a-match-case",
        ),
        pytest.param(
            """\
            def load(session):
                def select(entity):
                    return entity
                return session.scalars(select(Order)).all()  # parents
            """,
            HEADER,
            id="select-defined-in-the-function",
        ),
        pytest.param(
            """\
            def load(repo):
                return repo.query(Order)  # parents
            """,
            HEADER,
            id="query-on-something-not-a-session",
        ),
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(Order).options(*DEFAULTS)).all()  # parents
            """,
            HEADER,
            id="options-from-a-list",
        ),
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(Order)).all()  # parents
            """,
            HEADER.replace("import Customer, Order", "import Customer, Item as Order"),
            id="class-imported-under-another-name",
        ),
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(Order).options((selectinload(Order.items)))).all()  # parents
            """,
            HEADER,
            id="an-option-in-parentheses-of-its-own",
        ),
        pytest.param(
            """\
            match 1:
                case Order:
                    pass


            def load(session):
                return session.scalars(select(Order)).all()  # parents
            """,
            HEADER,
            id="class-rebound-by-a-module-level-match",
        ),
        pytest.param(
            """\
            def reset():
                global Order
                Order = None


            def load(session):
                return session.scalars(select(Order)).all()  # parents
            """,
            HEADER,
            id="class-rebound-through-a-global-statement",
        ),
        pytest.param(
            """\
            class load:
                joinedload = staticmethod(print)
                rows = session.scalars(select(Order)).all()  # parents
            """,
            HEADER,
            id="loader-bound-in-the-class-body",
        ),
        pytest.param(
            """\
            def load[Order](session):
                return session.scalars(select(Order)).all()  # parents
            """,
            HEADER,
            id="class-is-a-type-parameter",
        ),
    ],
)
def test_no_patch_when_the_line_is_not_certain(
    tmp_path: Path, body: str, header: str
) -> None:
    file = _write(tmp_path, body, header=header)
    assert _make(tmp_path, _finding(file)) is None


SQL_HEADER = (
    HEADER
    + "from sqlalchemy import exists, insert, text, union_all, update\n"
    + "from sqlalchemy.orm import Load, lazyload, noload, raiseload, selectinload\n"
    + "from app import rels\n"
    + "from app.models import Archive\n"
    + "from app.helpers import REL, defaults, paginate\n"
    + "import sqlalchemy_utils as orm\n"
)


@pytest.mark.parametrize(
    "body",
    [
        # the select is not the statement that runs
        pytest.param(
            "return session.execute(union_all(select(Order), other)).all()",
            id="in-a-union-all",
        ),
        pytest.param(
            "return session.scalar(exists(select(Order)).select())",
            id="in-an-exists",
        ),
        pytest.param(
            "return session.scalar(select(Order).exists().select())",
            id="turned-into-an-exists",
        ),
        pytest.param(
            "return session.execute(select(Order).subquery().select()).all()",
            id="turned-into-a-subquery",
        ),
        pytest.param(
            "return session.scalars(Customer.__table__.select()"
            ".where(Customer.id.in_(select(Order).scalar_subquery()))).all()",
            id="a-scalar-subquery-in-another-statement",
        ),
        pytest.param(
            "return session.execute(update(Customer).where(Customer.id.in_("
            "select(Order).with_only_columns(Order.customer_id))))",
            id="in-an-orm-update",
        ),
        pytest.param(
            "return session.execute(select(Order).cte().select()).all()",
            id="turned-into-a-cte",
        ),
        pytest.param(
            'return session.execute(insert(Archive).from_select(["id"], select(Order)))',
            id="in-an-insert-from-select",
        ),
        pytest.param(
            "return session.scalars(paginate(select(Order))).all()",
            id="passed-to-a-helper",
        ),
        pytest.param(
            "return session.execute(select(Order).with_only_columns(Order.id)).all()",
            id="other-columns",
        ),
        pytest.param(
            "return session.execute(select(Order).add_columns(Customer.name)).all()",
            id="more-columns",
        ),
        pytest.param(
            "return session.scalars(select(Order).union(other)).all()",
            id="a-union-method",
        ),
        pytest.param(
            'return session.scalars(select(Order).from_statement(text("SELECT 1"))).all()',
            id="from-a-textual-statement",
        ),
        pytest.param(
            "return repository.scalars(select(Order)).all()",
            id="run-by-something-not-a-session",
        ),
        pytest.param(
            "return session.scalars(statement=select(Order)).all()",
            id="a-keyword-argument",
        ),
        pytest.param(
            "return session.query(Order).with_entities(Order.id).all()",
            id="query-with-other-entities",
        ),
        pytest.param(
            'return session.query(Order).from_statement(text("SELECT 1")).all()',
            id="query-from-a-textual-statement",
        ),
        pytest.param(
            "return session.execute(session.query(Order).statement).scalars().all()",
            id="the-statement-of-a-query",
        ),
        pytest.param(
            "return session.query(Order).union_all(others).all()",
            id="query-union",
        ),
        pytest.param(
            "return session.query(Customer).filter("
            "Customer.id.in_(session.query(Order).subquery()))",
            id="query-subquery",
        ),
        pytest.param(
            "return paginate(session.query(Order))",
            id="query-passed-to-a-helper",
        ),
        pytest.param(
            "return session.query(Order).enable_eagerloads(False).all()",
            id="query-without-eager-loads",
        ),
        # an option of the statement may already load the relationship
        pytest.param(
            "return session.scalars(select(Order).options(noload(REL))).all()",
            id="a-loader-of-a-variable",
        ),
        pytest.param(
            'return session.scalars(select(Order).options(noload("customer"))).all()',
            id="a-loader-of-a-string",
        ),
        pytest.param(
            "return session.scalars(select(Order).options(defaults())).all()",
            id="options-from-a-helper",
        ),
        pytest.param(
            "return session.scalars(select(Order).options(lazyload(rels.CUSTOMER))).all()",
            id="a-loader-of-a-module-constant",
        ),
        pytest.param(
            "return session.scalars(select(Order).options("
            "selectinload(getattr(Order, name)))).all()",
            id="a-loader-of-a-computed-attribute",
        ),
        # a joined load the statement does not take
        pytest.param(
            "return session.scalars(select(Order).with_for_update()).all()",
            id="joined-for-update",
        ),
        pytest.param(
            "return session.scalars(select(Order).group_by(Order.id)).all()",
            id="joined-group-by",
        ),
        pytest.param(
            "return session.scalars(select(Order).distinct()).all()",
            id="joined-distinct",
        ),
        pytest.param(
            "return session.query(Order).with_for_update().all()",
            id="query-joined-for-update",
        ),
        # already safe: no select of the class runs
        pytest.param(
            "return session.execute(update(Order).where(Order.id > 0).values(n=1))",
            id="an-orm-update",
        ),
        pytest.param(
            "return session.scalars(select(Order.id)).all()",
            id="a-column-select",
        ),
        pytest.param(
            "return session.scalars(select(aliased(Order))).all()",
            id="an-aliased-class",
        ),
        pytest.param(
            "return session.scalars(select(Order).options("
            "selectinload(Order.items).raiseload(Order.customer))).all()",
            id="a-chained-loader-for-the-relationship",
        ),
        pytest.param(
            "return session.scalars(select(Order).options(orm.load_all(Order.tags))).all()",
            id="a-loader-of-a-module-not-sqlalchemy",
        ),
    ],
)
def test_no_patch_unless_the_select_is_the_statement_that_runs(
    tmp_path: Path, body: str
) -> None:
    file = _write(
        tmp_path,
        f"def load(session, other, others, repository, name):\n    {body}  # parents\n",
        header=SQL_HEADER,
    )
    assert _make(tmp_path, _finding(file)) is None


SESSION_HEADER = (
    HEADER
    + "from typing import Annotated\n"
    + "from fastapi import Depends\n"
    + "from sqlalchemy import orm\n"
    + "from sqlalchemy.engine import Connection\n"
    + "from sqlalchemy.ext.asyncio import AsyncSession\n"
)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            """\
            def load(db):
                return db.scalars(select(Order)).all()  # parents
            """,
            id="named-db",
        ),
        pytest.param(
            """\
            def load(db_session):
                return db_session.execute(select(Order)).scalars().all()  # parents
            """,
            id="named-db-session",
        ),
        pytest.param(
            """\
            class Repository:
                def load(self):
                    return self.db.scalars(select(Order)).all()  # parents
            """,
            id="self-db",
        ),
        pytest.param(
            """\
            def load(conn: Session):
                return conn.scalars(select(Order)).all()  # parents
            """,
            id="annotated-session",
        ),
        pytest.param(
            """\
            def load(s: orm.Session, n: int = 1):
                return list(s.scalars(select(Order)))  # parents
            """,
            id="annotated-through-a-module",
        ),
        pytest.param(
            """\
            def load(conn: Annotated[Session, Depends(get_db)]):
                return conn.scalars(select(Order)).all()  # parents
            """,
            id="annotated-with-depends",
        ),
        pytest.param(
            """\
            async def load(conn: "AsyncSession"):
                return (await conn.scalars(select(Order))).all()  # parents
            """,
            id="string-annotation",
        ),
        pytest.param(
            """\
            def load(*, conn: Session):
                return conn.query(Order).all()  # parents
            """,
            id="legacy-query-on-an-annotated-session",
        ),
        pytest.param(
            """\
            def load(db):
                return db.query(Order).all()  # parents
            """,
            id="legacy-query-on-db",
        ),
        pytest.param(
            """\
            def load(db: Session):
                statement = select(Order)  # built
                return db.scalars(statement).all()  # parents
            """,
            id="a-variable-run-by-db",
        ),
        pytest.param(
            """\
            def load(conn: Annotated[Session, Depends(get_db)]):
                statement = select(Order).where(Order.id > 0)  # built
                return conn.execute(statement).scalars().all()  # parents
            """,
            id="a-variable-run-by-an-annotated-session",
        ),
    ],
)
def test_a_session_by_name_or_annotation(tmp_path: Path, body: str) -> None:
    file = _write(tmp_path, body, header=SESSION_HEADER)
    fix = _make(tmp_path, _finding(file))
    assert fix is not None
    after = _patched(tmp_path, fix)
    assert "(Order).options(joinedload(Order.customer))" in after
    compile(after, "views.py", "exec")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            """\
            def load(repository):
                return repository.scalars(select(Order)).all()  # parents
            """,
            id="a-repository",
        ),
        pytest.param(
            """\
            def load(conn: Connection):
                return conn.scalars(select(Order)).all()  # parents
            """,
            id="annotated-another-type",
        ),
        pytest.param(
            """\
            def load(conn: SessionFactory):
                return conn.scalars(select(Order)).all()  # parents
            """,
            id="annotated-a-type-not-ending-in-session",
        ),
        pytest.param(
            """\
            def load(conn: list[Session]):
                return conn.scalars(select(Order)).all()  # parents
            """,
            id="annotated-a-container",
        ),
        pytest.param(
            """\
            def load(conn: Session, other):
                conn = other
                return conn.scalars(select(Order)).all()  # parents
            """,
            id="annotated-parameter-rebound",
        ),
        pytest.param(
            """\
            def outer(conn: Session):
                def load():
                    return conn.scalars(select(Order)).all()  # parents
                return load
            """,
            id="annotated-in-the-enclosing-function",
        ),
        pytest.param(
            """\
            def load(conn: Session):
                return [conn.scalars(select(Order)).all() for conn in pool]  # parents
            """,
            id="shadowed-in-a-comprehension",
        ),
        pytest.param(
            """\
            def load(conn: Session, repository):
                statement = select(Order)
                return repository.scalars(statement).all()  # parents
            """,
            id="a-variable-run-by-a-repository",
        ),
        pytest.param(
            """\
            def load(conn):
                return conn.query(Order).all()  # parents
            """,
            id="legacy-query-on-an-unknown-receiver",
        ),
    ],
)
def test_no_patch_for_a_receiver_not_known_as_a_session(
    tmp_path: Path, body: str
) -> None:
    file = _write(tmp_path, body, header=SESSION_HEADER)
    assert _make(tmp_path, _finding(file)) is None


def test_a_joined_load_of_the_selectin_query_takes_for_update(
    tmp_path: Path,
) -> None:
    file = _write(
        tmp_path,
        "def load(session):\n"
        "    return session.scalars(select(Order).with_for_update()).all()  # parents\n",
        header=HEADER.replace("import Customer, Order", "import Customer, Item, Order"),
    )
    finding = _finding(
        file, relationship="Item.product", path=(("Order.items", False),)
    )
    fix = _make(tmp_path, finding)
    assert fix is not None
    assert (
        "select(Order).options(selectinload(Order.items).joinedload(Item.product))"
        ".with_for_update()"
    ) in _patched(tmp_path, fix)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            "return session.scalars(select(Order).group_by(Order.id)).all()",
            "select(Order).options(selectinload(Order.items)).group_by(Order.id)",
            id="selectin-group-by",
        ),
        pytest.param(
            "return session.scalars(select(Order).with_for_update()).all()",
            "select(Order).options(selectinload(Order.items)).with_for_update()",
            id="selectin-for-update",
        ),
        pytest.param(
            "return session.scalars(select(Order).options("
            "Load(Order).raiseload('*'), selectinload(models.Order.tags))).all()",
            "Load(Order).raiseload('*'), selectinload(models.Order.tags), "
            "selectinload(Order.items))",
            id="loaders-of-other-relationships",
        ),
        pytest.param(
            "return session.query(Order).filter(Order.id > 0).first()",
            "session.query(Order).options(selectinload(Order.items)).filter(",
            id="query-first",
        ),
        pytest.param(
            "return session.exec(select(Order)).all()",
            "session.exec(select(Order).options(selectinload(Order.items)))",
            id="sqlmodel-exec",
        ),
        pytest.param(
            "return sorted(session.query(Order).limit(3))",
            "sorted(session.query(Order).options(selectinload(Order.items)).limit(3))",
            id="query-sorted",
        ),
    ],
)
def test_a_select_that_runs_as_it_is_is_patched(
    tmp_path: Path, body: str, expected: str
) -> None:
    file = _write(
        tmp_path,
        f"def load(session):\n    {body}  # parents\n",
        header=SQL_HEADER + "from app import models\n",
    )
    finding = _finding(file, relationship="Order.items", scalar=False)
    fix = _make(tmp_path, finding)
    assert fix is not None
    after = _patched(tmp_path, fix)
    assert expected in after
    compile(after, "views.py", "exec")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            """\
            def load(session):
                statement = select(Order).where(Order.id > 0)  # built
                return session.scalars(statement).all()  # parents
            """,
            id="assigned",
        ),
        pytest.param(
            """\
            def load(session):
                statement: Any = select(Order)  # built
                rows = session.execute(statement).scalars().all()  # parents
                return rows
            """,
            id="annotated",
        ),
        pytest.param(
            """\
            async def load(session):
                statement = (
                    select(Order)  # built
                    .where(Order.id > 0)
                )
                n = 1
                for order in (await session.execute(statement)).scalars():  # parents
                    print(order)
            """,
            id="over-several-lines-and-awaited",
        ),
    ],
)
def test_a_statement_built_on_an_earlier_line_is_patched_there(
    tmp_path: Path, body: str
) -> None:
    file = _write(tmp_path, body)
    fix = _make(tmp_path, _finding(file))
    assert fix is not None
    (option,) = [e for e in fix.insertions if e.text.startswith(".options")]
    assert option.line == _line(file, "# built")
    after = _patched(tmp_path, fix)
    assert "select(Order).options(joinedload(Order.customer))" in after
    assert "from sqlalchemy.orm import Session, joinedload\n" in after
    compile(after, "views.py", "exec")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            """\
            def load(session):
                statement = select(Order)
                statement = statement.limit(3)
                return session.scalars(statement).all()  # parents
            """,
            id="reassigned",
        ),
        pytest.param(
            """\
            def load(session, limit):
                statement = select(Order)
                if limit:
                    statement += limit
                return session.scalars(statement).all()  # parents
            """,
            id="changed-under-a-condition",
        ),
        pytest.param(
            """\
            def load(session):
                statement = select(Order)
                total = count(session, statement)
                return session.scalars(statement).all()  # parents
            """,
            id="read-elsewhere-too",
        ),
        pytest.param(
            """\
            def load(session, statement=None):
                statement = select(Order)
                return session.scalars(statement).all()  # parents
            """,
            id="a-parameter",
        ),
        pytest.param(
            """\
            statement = select(Order)


            def load(session):
                return session.scalars(statement).all()  # parents
            """,
            id="a-module-level-statement",
        ),
        pytest.param(
            """\
            def load(session):
                statement = select(Order)

                def reset():
                    nonlocal statement
                    statement = None

                return session.scalars(statement).all()  # parents
            """,
            id="rebound-by-a-closure",
        ),
        pytest.param(
            """\
            def outer(session):
                statement = select(Order)

                def load():
                    return session.scalars(statement).all()  # parents

                return load
            """,
            id="assigned-in-the-enclosing-function",
        ),
        pytest.param(
            """\
            def load(session):
                for n in range(2):
                    if n:
                        rows = session.scalars(statement).all()  # parents
                    statement = select(Order)
            """,
            id="assigned-after-the-line",
        ),
        pytest.param(
            """\
            def load(session):
                statement, n = select(Order), 1
                return session.scalars(statement).all()  # parents
            """,
            id="assigned-in-a-tuple",
        ),
        pytest.param(
            """\
            def load(session):
                if (statement := select(Order)) is not None:
                    return session.scalars(statement).all()  # parents
            """,
            id="assigned-by-a-walrus",
        ),
        pytest.param(
            """\
            def load(session, cached):
                statement = cached if cached else select(Order)
                return session.scalars(statement).all()  # parents
            """,
            id="a-conditional-value",
        ),
        pytest.param(
            """\
            def load(session):
                statement = paginate(select(Order))
                return session.scalars(statement).all()  # parents
            """,
            id="wrapped-in-a-call",
        ),
        pytest.param(
            """\
            def load(session):
                statement = select(Order).subquery()
                return session.scalars(statement).all()  # parents
            """,
            id="a-subquery",
        ),
        pytest.param(
            """\
            def load(session):
                statement = select(Order).where(Order.id.in_(session.scalars(ids)))
                return session.scalars(statement).all()  # parents
            """,
            id="a-query-runs-in-the-assignment",
        ),
        pytest.param(
            """\
            def load(session):
                query = session.query(Order)
                return session.execute(query).all()  # parents
            """,
            id="a-legacy-query-in-a-variable",
        ),
        pytest.param(
            """\
            def load(session):
                query = session.query(Order)
                return query.all()  # parents
            """,
            id="a-legacy-query-run-by-its-own-method",
        ),
        pytest.param(
            """\
            def load(session, other):
                statement = select(Order)
                return session.scalars(statement).all() + session.scalars(other).all()  # parents
            """,
            id="another-statement-runs-on-the-line",
        ),
        pytest.param(
            """\
            def load(session):
                statement = select(Order)
                return session.scalars(statement.limit(3)).all()  # parents
            """,
            id="a-method-on-the-variable",
        ),
        pytest.param(
            """\
            def load(session):
                statement = select(Order)
                return session.merge(statement)  # parents
            """,
            id="not-a-method-that-runs-a-statement",
        ),
        pytest.param(
            """\
            def load(session, other):
                statement = select(Order).union(other)
                return session.execute(statement).all()  # parents
            """,
            id="a-union",
        ),
        pytest.param(
            """\
            def load(session):
                statement = select(Order).with_only_columns(Order.id)
                return session.execute(statement).all()  # parents
            """,
            id="other-columns",
        ),
        pytest.param(
            """\
            def load(session, paginator):
                statement = select(Order)
                return paginator.scalars(statement).all()  # parents
            """,
            id="run-by-something-not-a-session",
        ),
        pytest.param(
            """\
            class Repository:
                statement = select(Order)
                rows = session.scalars(statement).all()  # parents
            """,
            id="in-a-class-body",
        ),
    ],
)
def test_no_patch_for_a_statement_variable_that_is_not_certain(
    tmp_path: Path, body: str
) -> None:
    file = _write(tmp_path, body)
    assert _make(tmp_path, _finding(file)) is None


MODULE_HEADER = """\
from sqlalchemy import select

import app.models
from app import models
from app.models import Customer
"""


@pytest.mark.parametrize(
    ("query", "relationship", "path", "option"),
    [
        pytest.param(
            "select(models.Order)",
            "Order.customer",
            (),
            "joinedload(models.Order.customer)",
            id="from-import-of-the-module",
        ),
        pytest.param(
            "session.query(app.models.Order)",
            "Order.customer",
            (),
            "joinedload(app.models.Order.customer)",
            id="dotted-import-of-the-module",
        ),
        pytest.param(
            "select(models.Order)",
            "Customer.country",
            (("Order.customer", True),),
            "joinedload(models.Order.customer).joinedload(Customer.country)",
            id="a-chain-through-an-imported-class",
        ),
    ],
)
def test_a_class_named_through_its_module(
    tmp_path: Path,
    query: str,
    relationship: str,
    path: tuple[tuple[str, bool], ...],
    option: str,
) -> None:
    run = f"session.scalars({query})" if query.startswith("select") else query
    file = _write(
        tmp_path,
        f"""\
        def load(session):
            return list({run})  # parents
        """,
        header=MODULE_HEADER,
    )
    fix = _make(tmp_path, _finding(file, relationship=relationship, path=path))
    assert fix is not None
    assert fix.description.endswith(f": {option}")
    after = _patched(tmp_path, fix)
    assert f"{query}.options({option})" in after
    assert (
        "\nfrom app.models import Customer\nfrom sqlalchemy.orm import joinedload\n"
        in after
    )
    compile(after, "views.py", "exec")


@pytest.mark.parametrize(
    ("body", "header"),
    [
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(models.Order)).all()  # parents
            """,
            MODULE_HEADER + "models = None\n",
            id="module-name-assigned",
        ),
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(models.Order)).all()  # parents
            """,
            MODULE_HEADER + "from app import helpers as models\n",
            id="module-imported-under-another-name",
        ),
        pytest.param(
            """\
            def load(session, models):
                return session.scalars(select(models.Order)).all()  # parents
            """,
            MODULE_HEADER,
            id="module-name-is-a-parameter",
        ),
        pytest.param(
            """\
            class Shop:
                pass


            def load(session):
                return session.scalars(select(Shop.Order)).all()  # parents
            """,
            MODULE_HEADER,
            id="a-class-attribute",
        ),
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(registry().Order)).all()  # parents
            """,
            MODULE_HEADER,
            id="not-a-name-at-the-start",
        ),
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(models.Item)).all()  # parents
            """,
            MODULE_HEADER,
            id="another-class",
        ),
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(models.Order)).all()  # parents
            """,
            MODULE_HEADER.replace("from app.models import Customer\n", ""),
            id="a-chain-class-the-module-does-not-import",
        ),
    ],
)
def test_no_patch_for_a_module_attribute_that_is_not_certain(
    tmp_path: Path, body: str, header: str
) -> None:
    file = _write(tmp_path, body, header=header)
    finding = _finding(
        file, relationship="Customer.country", path=(("Order.customer", True),)
    )
    assert _make(tmp_path, finding) is None


def test_options_over_several_lines_with_a_comment(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.scalars(select(Order).options(  # parents
                selectinload(Order.items),  # the items
            )).all()
        """,
        header=HEADER.replace("import Session", "import Session, selectinload"),
    )
    fix = _make(tmp_path, _finding(file))
    assert fix is not None
    after = _patched(tmp_path, fix)
    assert (
        "        selectinload(Order.items), joinedload(Order.customer),  # the items\n"
    ) in after


@pytest.mark.skipif(
    sys.platform == "win32" and sys.version_info < (3, 12),
    reason="the parser of Python < 3.12 on Windows overflows the C stack (crash)",
)
def test_code_too_deep_for_ast_gives_no_patch(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.scalars(select(Order)).all()  # parents
        """,
        header=HEADER + "X = " + " + ".join(["1"] * 100_000) + "\n",
    )
    try:
        ast.parse(file.read_text())
    except (RecursionError, MemoryError):
        assert _make(tmp_path, _finding(file)) is None
    else:  # pragma: no cover - a parser that takes it
        pytest.skip("this Python parses it")


def test_warnings_of_the_parsed_code_are_not_shown(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        PATTERN = "\\d+"


        def load(session):
            return session.scalars(select(Order)).all()  # parents
        """,
    )
    assert '"\\d+"' in file.read_text()  # an invalid escape
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fix = _make(tmp_path, _finding(file))
    assert fix is not None
    assert caught == []


def test_no_patch_for_a_chain_class_the_module_does_not_name(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.scalars(select(Order)).all()  # parents
        """,
        header=HEADER.replace("import Customer, Order", "import Order"),
    )
    finding = _finding(
        file, relationship="Customer.country", path=(("Order.customer", True),)
    )
    assert _make(tmp_path, finding) is None


def test_no_patch_without_a_single_known_parent_query(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.scalars(select(Order)).all()  # parents
        """,
    )
    assert _make(tmp_path, _finding(file, others=1)) is None
    assert _make(tmp_path, _finding(file, root=None)) is None
    assert _make(tmp_path, _finding(file, line=1)) is None


def test_no_patch_for_a_file_changed_after_the_session_started(
    tmp_path: Path,
) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.scalars(select(Order)).all()  # parents
        """,
    )
    started = file.stat().st_mtime - 10
    assert fixpatch.make(_finding(file), tmp_path, started) is None


def test_no_patch_outside_the_rootdir(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.scalars(select(Order)).all()  # parents
        """,
    )
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert fixpatch.make(_finding(file), other, time.time() + 1) is None


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(HEADER, id="added-to-the-orm-import"),
        pytest.param(
            "from sqlalchemy import select\n\nfrom app.models import Order\n",
            id="a-new-import-line",
        ),
    ],
)
@pytest.mark.parametrize(
    ("body", "option"),
    [
        pytest.param(
            """\
            def load(session):
                return session.scalars(select(Order)).all()  # parents
            """,
            b"select(Order).options(joinedload(Order.customer))",
            id="on-the-line",
        ),
        pytest.param(
            """\
            def load(session):
                statement = select(models.Order)
                return session.scalars(statement).all()  # parents
            """,
            b"select(models.Order).options(joinedload(models.Order.customer))",
            id="a-module-attribute-built-on-an-earlier-line",
        ),
    ],
)
def test_windows_line_ends_are_kept(
    tmp_path: Path, header: str, body: str, option: bytes
) -> None:
    file = _write(
        tmp_path,
        body,
        header=header + "from app import models\n",
        newline="\r\n",
    )
    fix = _make(tmp_path, _finding(file))
    assert fix is not None
    assert all(e.text.count("\n") == e.text.count("\r\n") for e in fix.insertions)
    _patched(tmp_path, fix)
    data = file.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n")
    assert option in data
    assert b"joinedload\r\n" in data


def test_no_patch_for_a_lone_carriage_return(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.scalars(select(Order)).all()  # parents
        """,
    )
    file.write_bytes(file.read_bytes().replace(b"\n\n", b"\n\r", 1))
    assert _make(tmp_path, _finding(file)) is None


@pytest.mark.parametrize(
    ("path", "root", "expected"),
    [
        (r"C:\Repo\app\views.py", r"c:\repo", "app/views.py"),
        ("C:/Repo/app/views.py", r"C:\Repo", "app/views.py"),
        (r"C:\Repo\views.py", r"C:\Repo\app", None),
        (r"D:\Repo\app\views.py", r"C:\Repo", None),
        (r"C:\Repository\views.py", r"C:\Repo", None),
    ],
)
def test_windows_paths_compare_as_windows_does(
    path: str, root: str, expected: str | None
) -> None:
    assert fixpatch.inside(path, root, ntpath) == expected


def test_posix_paths_compare_exactly() -> None:
    assert fixpatch.inside("/repo/app/v.py", "/repo", posixpath) == "app/v.py"
    assert fixpatch.inside("/Repo/app/v.py", "/repo", posixpath) is None
    assert fixpatch.inside("/repo", "/repo", posixpath) is None


def test_fixes_of_one_file_combine_into_one_patch(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.scalars(select(Customer)).all()  # parents
        """,
        header='"""Views."""\n\nfrom sqlalchemy import select\n\n'
        "from app.models import Customer\n",
    )
    country = _make(
        tmp_path, _finding(file, relationship="Customer.country", root="Customer")
    )
    orders = _make(
        tmp_path,
        _finding(file, relationship="Customer.orders", scalar=False, root="Customer"),
    )
    assert country is not None and orders is not None
    text, files, changed = fixpatch.combine(
        [country, orders, country], tmp_path, time.time() + 1
    )
    assert (files, changed) == (1, [])
    # a file modified after the session started is left out and named
    assert fixpatch.combine([country], tmp_path, 0.0) == ("", 0, ["app/views.py"])
    patch = tmp_path / "all.patch"
    patch.write_bytes(text.encode("utf-8"))  # no newline translation
    after = _git_apply(tmp_path, patch, "app/views.py")
    assert (
        "from app.models import Customer\n"
        "from sqlalchemy.orm import joinedload\n"
        "from sqlalchemy.orm import selectinload\n"
    ) in after
    assert (
        "select(Customer).options(joinedload(Customer.country))"
        ".options(selectinload(Customer.orders))"
    ) in after
    compile(after, "views.py", "exec")


def test_an_option_that_a_chain_of_the_same_query_extends_is_left_out(
    tmp_path: Path,
) -> None:
    # Order.customer, and Customer.country of the customers reached through
    # it: the chain loads Order.customer too, so one .options() is enough
    file = _write(
        tmp_path,
        """\
        def load(session: Session) -> list[Order]:
            return session.scalars(select(Order)).all()  # parents
        """,
    )
    customer = _make(tmp_path, _finding(file))
    country = _make(
        tmp_path,
        _finding(
            file,
            relationship="Customer.country",
            path=(("Order.customer", True),),
        ),
    )
    assert customer is not None and country is not None
    text, files, _ = fixpatch.combine([customer, country], tmp_path, time.time() + 1)
    assert files == 1
    patch = tmp_path / "all.patch"
    patch.write_bytes(text.encode("utf-8"))
    after = _git_apply(tmp_path, patch, "app/views.py")
    assert (
        "select(Order).options(joinedload(Order.customer)"
        ".joinedload(Customer.country))).all()"
    ) in after
    assert after.count(".options(") == 1
    compile(after, "views.py", "exec")


def test_options_of_different_relationships_are_all_kept(tmp_path: Path) -> None:
    # joinedload(Order.customer) is no prefix of joinedload(Order.customer_items)
    file = _write(
        tmp_path,
        """\
        def load(session: Session) -> list[Order]:
            return session.scalars(select(Order).options(raiseload(Order.notes))).all()  # parents
        """,
        header=HEADER.replace("import Session", "import Session, raiseload"),
    )
    customer = _make(tmp_path, _finding(file))
    items = _make(tmp_path, _finding(file, relationship="Order.customer_items"))
    assert customer is not None and items is not None
    text, _, _ = fixpatch.combine([customer, items], tmp_path, time.time() + 1)
    assert "joinedload(Order.customer)" in text
    assert "joinedload(Order.customer_items)" in text


def test_the_wire_form_round_trips(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.scalars(select(Order)).all()  # parents
        """,
    )
    fix = _make(tmp_path, _finding(file))
    assert fix is not None
    wire: Any = json.loads(json.dumps(fixpatch.to_wire(fix)))
    assert fixpatch.from_wire(wire) == fix
    assert fixpatch.to_wire(None) is None
    assert fixpatch.from_wire(None) is None


def test_the_report_shows_the_diff_only_when_asked(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.scalars(select(Order)).all()  # parents
        """,
    )
    fix = _make(tmp_path, _finding(file))
    assert fix is not None
    finding = Finding(
        kind=KIND_RELATIONSHIP,
        label="Order.customer",
        file="app/views.py",
        function="load",
        message="  Order.customer was lazy-loaded 3 times",
        fix=fix,
    )
    assert "patch" not in format_findings([finding])
    shown = format_findings([finding], patches=True)
    assert "  patch (git apply from the rootdir" in shown
    assert "    +++ b/app/views.py" in shown


def test_a_relative_import_of_the_class_is_accepted(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.scalars(select(Order)).all()  # parents
        """,
        header=HEADER.replace("from app.models", "from .models"),
    )
    assert _make(tmp_path, _finding(file)) is not None


def test_a_class_rebound_under_an_if_gives_no_patch(tmp_path: Path) -> None:
    file = _write(
        tmp_path,
        """\
        if True:
            from app.legacy import Order


        def load(session):
            return session.scalars(select(Order)).all()  # parents
        """,
    )
    assert _make(tmp_path, _finding(file)) is None


# --- through pytest ---

MODELS = (Path(__file__).parent / "models.py").read_text()

LOOPS = """\
import pytest
from qgmodels import Customer, Order, make_sync_session


def test_orders():
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name


def test_customers():
    session = make_sync_session()
    customers = session.query(Customer).filter(Customer.id > 0).all()
    for customer in customers:
        _ = customer.orders


def test_built_elsewhere():
    session = make_sync_session()
    query = session.query(Order)
    for order in query.all():
        _ = order.customer.name
"""


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS, test_loops=LOOPS)
    return pytester


def test_the_report_prints_the_patch_only_when_asked(project: pytest.Pytester) -> None:
    plain = project.runpytest("-k", "test_orders")
    plain.assert_outcomes(failed=1)
    assert "patch (git apply" not in plain.stdout.str()
    shown = project.runpytest("-k", "test_orders", "--queryguard-show-patch")
    shown.assert_outcomes(failed=1)
    shown.stdout.fnmatch_lines(
        [
            "  patch (git apply from the rootdir*",
            "    --- a/test_loops.py",
            "    +++ b/test_loops.py",
            "*+from sqlalchemy.orm import joinedload",
            (
                "*+    for order in session.query(Order)"
                ".options(joinedload(Order.customer)).all():"
            ),
        ]
    )


def test_the_ini_option_prints_the_patch(project: pytest.Pytester) -> None:
    project.makeini("[pytest]\nqueryguard_show_patch = true\n")
    result = project.runpytest("-k", "test_orders")
    result.stdout.fnmatch_lines(["  patch (git apply from the rootdir*"])


def test_no_flag_turns_the_patch_off_for_one_run(project: pytest.Pytester) -> None:
    project.makeini("[pytest]\nqueryguard_show_patch = true\n")
    result = project.runpytest("-k", "test_orders", "--no-queryguard-show-patch")
    result.assert_outcomes(failed=1)
    assert "patch (git apply" not in result.stdout.str()
    # the last of the two flags wins
    result = project.runpytest(
        "-k", "test_orders", "--no-queryguard-show-patch", "--queryguard-show-patch"
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["  patch (git apply from the rootdir*"])


def test_the_written_patch_applies_and_fixes_the_n_plus_one(
    project: pytest.Pytester,
) -> None:
    result = project.runpytest("--queryguard-write-patch=out/fix.patch")
    result.assert_outcomes(failed=3)
    result.stdout.fnmatch_lines(
        [
            "*queryguard: patch written*",
            "*out/fix.patch: changes 1 file; apply it from the rootdir (*) with git apply *fix.patch",
        ]
    )
    patch = project.path / "out" / "fix.patch"
    after = _git_apply(project.path, patch, "test_loops.py")
    assert "session.query(Order).options(joinedload(Order.customer)).all()" in after
    assert (
        "session.query(Customer).options(selectinload(Customer.orders))"
        ".filter(Customer.id > 0).all()"
    ) in after
    # the query built on another line is left alone
    assert "    query = session.query(Order)\n" in after
    rerun = project.runpytest()
    rerun.assert_outcomes(passed=2, failed=1)
    assert "test_built_elsewhere" in rerun.stdout.str()


def test_an_empty_patch_file_when_no_fix_is_certain(project: pytest.Pytester) -> None:
    result = project.runpytest(
        "-k", "test_built_elsewhere", "--queryguard-write-patch=fix.patch"
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*fix.patch: empty, no finding had a parent*"])
    assert (project.path / "fix.patch").read_text() == ""


def test_baselined_findings_get_no_patch(project: pytest.Pytester) -> None:
    project.runpytest(
        "--queryguard-baseline=b.json", "--queryguard-update-baseline"
    ).assert_outcomes(passed=3)
    project.runpytest(
        "--queryguard-baseline=b.json", "--queryguard-write-patch=fix.patch"
    ).assert_outcomes(passed=3)
    assert (project.path / "fix.patch").read_text() == ""


def test_sarif_results_carry_the_fix(project: pytest.Pytester) -> None:
    project.runpytest("--queryguard-sarif=qg.sarif").assert_outcomes(failed=3)
    document = json.loads((project.path / "qg.sarif").read_text())
    results = {
        (r["properties"]["label"], r["locations"][0]["logicalLocations"][0]["name"]): r
        for r in document["runs"][0]["results"]
    }
    (fix,) = results["Customer.orders", "test_customers"]["fixes"]
    assert fix["description"]["text"] == (
        "Load Customer.orders with the parent query: selectinload(Customer.orders)"
    )
    (change,) = fix["artifactChanges"]
    assert change["artifactLocation"]["uri"] == "test_loops.py"
    lines = (project.path / "test_loops.py").read_text().splitlines()
    for replacement in change["replacements"]:
        region = replacement["deletedRegion"]
        assert region["startLine"] == region["endLine"]
        assert region["startColumn"] == region["endColumn"]
    texts = sorted(r["insertedContent"]["text"] for r in change["replacements"])
    # pytester writes the file with the platform's line ends, which are kept
    newline = (
        "\r\n" if b"\r\n" in (project.path / "test_loops.py").read_bytes() else "\n"
    )
    assert texts == [
        f"{newline}from sqlalchemy.orm import selectinload",
        ".options(selectinload(Customer.orders))",
    ]
    option = next(
        r["deletedRegion"]
        for r in change["replacements"]
        if r["insertedContent"]["text"].startswith(".options")
    )
    line = lines[option["startLine"] - 1]
    assert line[: option["startColumn"] - 1].endswith("session.query(Customer)")
    assert len(results["Order.customer", "test_orders"]["fixes"]) == 1
    # the parent query built on another line gets no fix
    assert "fixes" not in results["Order.customer", "test_built_elsewhere"]


def test_xdist_workers_send_their_fixes(project: pytest.Pytester) -> None:
    pytest.importorskip("xdist")
    result = project.runpytest(
        "-p", "xdist", "-n", "2", "--queryguard-write-patch=fix.patch"
    )
    result.assert_outcomes(failed=3)
    after = _git_apply(project.path, project.path / "fix.patch", "test_loops.py")
    assert "options(joinedload(Order.customer))" in after
    assert "options(selectinload(Customer.orders))" in after


def test_a_written_patch_for_windows_line_ends_applies(
    project: pytest.Pytester,
) -> None:
    source = project.path / "test_loops.py"
    data = source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    source.write_bytes(data)
    project.runpytest("--queryguard-write-patch=fix.patch").assert_outcomes(failed=3)
    _git_apply(project.path, project.path / "fix.patch", "test_loops.py")
    after = source.read_bytes()
    assert after.count(b"\n") == after.count(b"\r\n")
    assert b"from sqlalchemy.orm import joinedload\r\n" in after
    project.runpytest().assert_outcomes(passed=2, failed=1)


BUILT = """\
from sqlalchemy import select

import qgmodels


def test_statement():
    session = qgmodels.make_sync_session()
    statement = select(qgmodels.Customer).where(qgmodels.Customer.id > 0)
    for customer in session.scalars(statement):
        _ = customer.orders


def test_result_scalars():
    session = qgmodels.make_sync_session()
    for order in session.execute(select(qgmodels.Order)).scalars():
        _ = order.customer.name
"""


def test_a_statement_variable_and_a_module_attribute_are_patched(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(qgmodels=MODELS, test_built=BUILT)
    result = pytester.runpytest("--queryguard-write-patch=fix.patch")
    result.assert_outcomes(failed=2)
    after = _git_apply(pytester.path, pytester.path / "fix.patch", "test_built.py")
    assert (
        "    statement = select(qgmodels.Customer)"
        ".options(selectinload(qgmodels.Customer.orders))"
        ".where(qgmodels.Customer.id > 0)\n"
    ) in after
    assert (
        "session.execute(select(qgmodels.Order)"
        ".options(joinedload(qgmodels.Order.customer))).scalars()"
    ) in after
    assert "\nfrom sqlalchemy.orm import joinedload\n" in after
    pytester.runpytest().assert_outcomes(passed=2)


# --- a file name that is not valid UTF-8 ---


def _non_utf8_dir(root: Path) -> Path:
    """``root/caf\\xe9``: the name reaches Python as a lone surrogate."""
    try:
        os.makedirs(os.fsencode(root) + b"/caf\xe9")
    except (OSError, ValueError):  # pragma: no cover - the filesystem refuses it
        pytest.skip("cannot create a non-UTF-8 directory name here")
    return Path(os.fsdecode(os.fsencode(root) + b"/caf\xe9"))


def test_git_quotes_a_name_as_git_does() -> None:
    assert fixpatch.git_path("a/", "app/views.py") == "a/app/views.py"
    assert fixpatch.git_path("a/", "app/żółw.py") == "a/app/żółw.py"
    # on Windows os.fsencode writes the surrogate as its three UTF-8 bytes
    byte = "\\355\\263\\251" if sys.platform == "win32" else "\\351"
    assert fixpatch.git_path("b/", "caf\udce9/v.py") == f'"b/caf{byte}/v.py"'
    # once quoted, every byte that is not printable ASCII is escaped
    assert fixpatch.git_path("a/", "ż\udce9.py") == f'"a/\\305\\274{byte}.py"'
    assert fixpatch.git_path("a/", 'q"\\\t\n.py') == '"a/q\\"\\\\\\t\\n.py"'


def test_a_file_name_that_is_not_utf8_gets_a_patch_git_applies(
    tmp_path: Path,
) -> None:
    directory = _non_utf8_dir(tmp_path)
    file = directory / "views.py"
    file.write_bytes(
        (
            HEADER
            + "\n\ndef load(session):\n"
            + "    return session.scalars(select(Order)).all()  # parents\n"
        ).encode("utf-8")
    )
    fix = _make(tmp_path, _finding(file))
    assert fix is not None
    assert fix.file == "caf\udce9/views.py"
    # the file name as git writes it: quoted, the byte as an octal escape
    assert fix.diff.startswith('--- "a/caf\\351/views.py"\n+++ "b/caf\\351/views.py"\n')
    combined = fixpatch.combine([fix], tmp_path, time.time() + 1)
    assert combined == (fix.diff, 1, [])
    after = _patched(tmp_path, fix)  # strict UTF-8, then git apply
    assert "select(Order).options(joinedload(Order.customer))" in after


def test_the_written_and_shown_patch_of_a_non_utf8_file_name(
    project: pytest.Pytester,
) -> None:
    # not under pytest-xdist: execnet cannot send such a test id at collection
    sub = _non_utf8_dir(project.path)
    (project.path / "test_loops.py").rename(sub / "test_loops.py")
    project.syspathinsert()
    result = project.runpytest(
        "-k",
        "test_orders",
        "-p",
        "no:cacheprovider",
        "--queryguard-show-patch",
        "--queryguard-write-patch=fix.patch",
    )
    result.assert_outcomes(failed=1)
    output = result.stdout.str() + result.stderr.str()
    assert "INTERNALERROR" not in output
    assert "could not write" not in output
    result.stdout.fnmatch_lines(
        [
            '    --- "a/caf\\351/test_loops.py"',
            "*fix.patch: changes 1 file; apply it from the rootdir (*) with git apply *fix.patch",
        ]
    )
    patch = project.path / "fix.patch"
    patch.read_bytes().decode("utf-8")  # strict: no lone surrogates
    after = _git_apply(project.path, patch, "caf\udce9/test_loops.py")
    assert "session.query(Order).options(joinedload(Order.customer)).all()" in after
