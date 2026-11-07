"""Smoke tests for the projects in examples/.

Each example has a failing test that shows a finding and a `_fixed` test that
passes after the fix. The tests here run them in a subprocess, check the
outcome and the key lines of the report, and check that the example's README
shows the same lines, so a README that drifts from the real output fails here.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

# pytester-style isolation is not needed: the examples run in their own process.
pytestmark = [
    pytest.mark.allow_nplusone,
    # the source distribution ships tests/ but not examples/
    pytest.mark.skipif(not EXAMPLES.is_dir(), reason="examples/ is not available"),
]


def run(
    example: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *args],
        cwd=example,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
        timeout=120,
    )


def assert_in_output_and_readme(
    example: Path, result: subprocess.CompletedProcess[str], lines: list[str]
) -> None:
    readme = (example / "README.md").read_text(encoding="utf-8")
    for line in lines:
        assert line in result.stdout, f"{line!r} is not in the output:\n{result.stdout}"
        assert line in readme, f"{line!r} is not in {example.name}/README.md"


def test_sync_example() -> None:
    example = EXAMPLES / "sync-n-plus-one"
    broken = run(example, "tests/test_orders.py")
    assert broken.returncode == 1, broken.stdout
    assert "3 failed" in broken.stdout
    assert_in_output_and_readme(
        example,
        broken,
        [
            "Order.customer was lazy-loaded 50 times",
            "first at app/orders.py:10 in list_orders",
            "the same statement ran 50 times",
            "first at app/customers.py:9 in fetch_customers",
            "queryguard: expected at most 2 queries, got 4",
            "(app/customers.py:16 in rename_customers)",
        ],
    )
    fixed = run(example, "tests/test_orders_fixed.py")
    assert fixed.returncode == 0, fixed.stdout
    assert "3 passed" in fixed.stdout


def test_async_fastapi_example() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("aiosqlite")
    pytest.importorskip("greenlet")
    example = EXAMPLES / "async-fastapi"
    broken = run(example, "tests/test_orders.py")
    assert broken.returncode == 1, broken.stdout
    assert_in_output_and_readme(
        example,
        broken,
        [
            "Order.customer was lazy-loaded 5 times",
            "first at app/main.py:37 in list_orders",
        ],
    )
    fixed = run(example, "tests/test_orders_fixed.py")
    assert fixed.returncode == 0, fixed.stdout


def test_fastapi_dependency_override_example() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("aiosqlite")
    pytest.importorskip("greenlet")
    example = EXAMPLES / "fastapi-dependency-override"
    broken = run(example, "tests/test_orders.py")
    assert broken.returncode == 1, broken.stdout
    assert "2 failed" in broken.stdout
    assert_in_output_and_readme(
        example,
        broken,
        [
            "first at app/main.py:33 in list_orders",
            "first at app/main.py:49 in list_async_orders",
        ],
    )
    recipe = (ROOT / "docs" / "recipes-fastapi.md").read_text(encoding="utf-8")
    assert "first at app/main.py:33 in list_orders" in recipe
    assert "first at app/main.py:49 in list_async_orders" in recipe
    fixed = run(example, "tests/test_orders_fixed.py")
    assert fixed.returncode == 0, fixed.stdout
    assert "2 passed" in fixed.stdout


def test_sqlite_index_example() -> None:
    example = EXAMPLES / "sqlite-index-check"
    broken = run(example, "tests/test_orders.py")
    assert broken.returncode == 1, broken.stdout
    assert_in_output_and_readme(
        example,
        broken,
        [
            "queryguard: missing index",
            "orders is read in full: SCAN with filter orders.customer_id = ?",
            "fix: CREATE INDEX ix_orders_customer_id ON orders (customer_id)",
        ],
    )
    fixed = run(example, "tests/test_orders_fixed.py")
    assert fixed.returncode == 0, fixed.stdout


def _postgres_url() -> str:
    url = os.environ.get("QUERYGUARD_PG_URL")
    if not url:
        pytest.skip("QUERYGUARD_PG_URL is not set")
    sqlalchemy = pytest.importorskip("sqlalchemy")
    pytest.importorskip("pg8000")
    engine = sqlalchemy.create_engine(url)
    try:
        with engine.connect() as connection:
            version = connection.execute(
                sqlalchemy.text("SHOW server_version_num")
            ).scalar_one()
    finally:
        engine.dispose()
    if int(version) < 160000:
        pytest.skip("the index check needs PostgreSQL 16 or newer")
    return url


def test_postgres_index_example() -> None:
    url = _postgres_url()
    example = EXAMPLES / "postgres-missing-index"
    env = {"QUERYGUARD_PG_URL": url}
    broken = run(example, "tests/test_orders.py", env=env)
    assert broken.returncode == 1, broken.stdout
    assert_in_output_and_readme(
        example,
        broken,
        [
            "queryguard: missing index",
            "orders is read in full: Seq Scan with filter (orders.customer_id = $1)",
            "fix: CREATE INDEX ON orders (customer_id)",
        ],
    )
    fixed = run(example, "tests/test_orders_fixed.py", env=env)
    assert fixed.returncode == 0, fixed.stdout


def test_postgres_example_has_a_compose_file() -> None:
    compose = (EXAMPLES / "postgres-missing-index" / "compose.yaml").read_text()
    assert "image: postgres:17" in compose
    assert "5432:5432" in compose


def test_baseline_and_sarif_example(tmp_path: Path) -> None:
    source = EXAMPLES / "baseline-sarif"
    example = tmp_path / "baseline-sarif"
    shutil.copytree(
        source, example, ignore=shutil.ignore_patterns("__pycache__", "*.sarif")
    )

    without = run(example, "tests", "-o", "queryguard_baseline=")
    assert without.returncode == 1, without.stdout
    assert "3 failed" in without.stdout

    # Recording the legacy findings reproduces the committed baseline file.
    (example / "queryguard-baseline.json").unlink()
    update = run(example, "tests/test_legacy.py", "--queryguard-update-baseline")
    assert update.returncode == 0, update.stdout
    assert "2 entries (2 added, 0 removed)" in update.stdout
    committed = json.loads((source / "queryguard-baseline.json").read_text())
    assert json.loads((example / "queryguard-baseline.json").read_text()) == committed

    gated = run(example, "tests", "--queryguard-sarif=queryguard.sarif")
    assert gated.returncode == 1, gated.stdout
    assert_in_output_and_readme(
        source,
        gated,
        [
            "Book.author was lazy-loaded 7 times",
            "first at app/books.py:27 in titles_of",
            "1 failed, 2 passed",
        ],
    )
    sarif = json.loads((example / "queryguard.sarif").read_text())
    (result,) = sarif["runs"][0]["results"]
    assert result["ruleId"] == "queryguard/relationship-load"
    location = result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert location == "app/books.py"


def test_in_app_example() -> None:
    for module in ("fastapi", "flask", "httpx", "aiosqlite", "greenlet"):
        pytest.importorskip(module)
    example = EXAMPLES / "in-app"
    environment = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    result = subprocess.run(
        [sys.executable, "-W", "ignore::DeprecationWarning", "demo.py"],
        cwd=example,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [
        "WARNING pytest_queryguard.app: queryguard: GET /orders: 6 statements",
        "Order.customer was lazy-loaded 5 times",
        "first at app/main.py:48 in list_orders",
        (
            "WARNING pytest_queryguard.app: queryguard: GET /orders/3 "
            "(route /orders/{order_id}): 2 statements"
        ),
        "queryguard: expected at most 1 query, got 2",
        "GET /orders/3 200 2 statements budget 1 []",
        "GET /orders-fixed 200 2 statements budget None []",
        "GET /orders 200 6 statements budget None ['Order.customer']",
        "first at app/flask_app.py:41 in list_orders",
    ]
    assert_in_output_and_readme(example, result, lines)
    docs = (ROOT / "docs" / "in-app.md").read_text(encoding="utf-8")
    for line in lines:
        assert line in docs, f"{line!r} is not in docs/in-app.md"


# The pages and examples whose pytest.ini quiets Starlette's test client warning.
_INI_BLOCK = re.compile(r"^```ini\n(.*?)^```", re.DOTALL | re.MULTILINE)

# conftest.py of the check: httpx2 hidden, so a Starlette 1.2+ test client warns
_IMPORTS_TESTCLIENT = """\
import sys

sys.modules["httpx2"] = None
import fastapi.testclient  # noqa: E402,F401
"""


def _testclient_filters() -> dict[tuple[str, ...], list[str]]:
    """{the filterwarnings lines: the pages and examples that have them}."""
    texts = [
        (str(path.relative_to(ROOT)), path.read_text(encoding="utf-8"))
        for path in sorted(EXAMPLES.glob("*/pytest.ini"))
    ]
    for page in sorted((ROOT / "docs").glob("*.md")):
        text = page.read_text(encoding="utf-8")
        texts += [(page.name, block) for block in _INI_BLOCK.findall(text)]
    found: dict[tuple[str, ...], list[str]] = {}
    for name, text in texts:
        if "starlette.testclient" not in text:
            continue
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(text)
        lines = parser.get("pytest", "filterwarnings").splitlines()
        filters = tuple(line.strip() for line in lines if line.strip())
        found.setdefault(filters, []).append(name)
    return found


def _run_with_filters(tmp_path: Path, filters: tuple[str, ...]) -> int:
    """Exit code of a pytest run with ``error`` and then these filters."""
    project = tmp_path / str(len(list(tmp_path.iterdir())))
    project.mkdir()
    (project / "conftest.py").write_text(_IMPORTS_TESTCLIENT, encoding="utf-8")
    (project / "test_nothing.py").write_text(
        "def test_nothing():\n    pass\n", encoding="utf-8"
    )
    lines = "".join(f"    {line}\n" for line in ("error", *filters))
    (project / "pytest.ini").write_text(
        f"[pytest]\nfilterwarnings =\n{lines}", encoding="utf-8"
    )
    return run(project, "-p", "no:cacheprovider", "-p", "no:queryguard").returncode


def test_the_documented_filter_quiets_starlettes_test_client(tmp_path: Path) -> None:
    # Starlette 1.2+ warns (StarletteDeprecationWarning) when its test client
    # finds httpx without httpx2. A reader with filterwarnings = error, then
    # the page's lines, must not fail on it.
    for module in ("fastapi", "httpx"):
        pytest.importorskip(module)
    found = _testclient_filters()
    names = {name for pages in found.values() for name in pages}
    assert {"recipes-fastapi.md", "recipes-test-transaction.md"} <= names
    if _run_with_filters(tmp_path, ()) == 0:
        pytest.skip("this Starlette does not warn about httpx")
    for filters, pages in found.items():
        assert _run_with_filters(tmp_path, filters) == 0, pages
