"""The patch that adds the loader option to the parent query (issue #247).

A patch is made only when the parent query line is certain; every case where
it is not gives none.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import shutil
import subprocess
import textwrap
import time
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
    patch.write_text(fix.diff, encoding="utf-8")
    return _git_apply(tmp_path, patch, fix.file)


def _git_apply(tmp_path: Path, patch: Path, file: str) -> str:
    if _GIT is None:
        pytest.skip("git is not installed")
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)}
    for args in (["apply", "--check", str(patch)], ["apply", str(patch)]):
        subprocess.run(
            [_GIT, *args], cwd=tmp_path, check=True, env=env, capture_output=True
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
                statement = select(Order)
                return session.scalars(statement).all()  # parents
            """,
            HEADER,
            id="query-built-on-another-line",
        ),
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
    ],
)
def test_no_patch_when_the_line_is_not_certain(
    tmp_path: Path, body: str, header: str
) -> None:
    file = _write(tmp_path, body, header=header)
    assert _make(tmp_path, _finding(file)) is None


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
def test_windows_line_ends_are_kept(tmp_path: Path, header: str) -> None:
    file = _write(
        tmp_path,
        """\
        def load(session):
            return session.scalars(select(Order)).all()  # parents
        """,
        header=header,
        newline="\r\n",
    )
    fix = _make(tmp_path, _finding(file))
    assert fix is not None
    assert all(e.text.count("\n") == e.text.count("\r\n") for e in fix.insertions)
    _patched(tmp_path, fix)
    data = file.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n")
    assert b".options(joinedload(Order.customer))" in data
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
    patch.write_text(text, encoding="utf-8")
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
    shown = project.runpytest("-k", "test_orders", "--queryguard-patch")
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
    project.makeini("[pytest]\nqueryguard_patch = true\n")
    result = project.runpytest("-k", "test_orders")
    result.stdout.fnmatch_lines(["  patch (git apply from the rootdir*"])


def test_the_written_patch_applies_and_fixes_the_n_plus_one(
    project: pytest.Pytester,
) -> None:
    result = project.runpytest("--queryguard-write-patch=out/fix.patch")
    result.assert_outcomes(failed=3)
    result.stdout.fnmatch_lines(
        [
            "*queryguard: patch written*",
            "*out/fix.patch: changes 1 file; apply it from * with git apply",
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
    assert texts == [
        "\nfrom sqlalchemy.orm import selectinload",
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
