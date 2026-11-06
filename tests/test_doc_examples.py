"""The Python examples in README.md and docs/ must be valid, and the projects
they spell out must run.

Two checks, both generic, so a new example is covered without touching this file:

* every ```python block parses, whether it is a whole file or a fragment;
* the blocks that a page introduces with a file name (a paragraph that starts
  with `app/main.py`, then the block) are the files of one project per page.
  The project is written to a temporary directory and pytest runs it. Each test
  has to pass or to fail with a queryguard report (the pages show broken code on
  purpose); an import error, a missing fixture or any other error is a failure
  of the documentation. Each queryguard report the page shows in an output
  block must be, line for line, in the output of that run (``OTHER_SETUPS``
  lists the reports of another setup).

A page that does not spell out every file of its project starts from a base
project in examples/ (``BASES``); its own files are laid on top.

A project that needs a package that is not installed is skipped, unless
QUERYGUARD_REQUIRE_DOC_EXAMPLES is set (the release gate sets it); only
OPTIONAL_PACKAGES may be missing then.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from importlib.util import find_spec
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]

pytestmark = pytest.mark.skipif(
    not (ROOT / "docs").is_dir(), reason="docs/ is not available"
)

# page -> project in examples/ that supplies the files the page does not show
BASES: dict[str, str] = {
    "getting-started.md": "sync-n-plus-one",
}

# Pages whose file blocks are excerpts, not a project, and why.
NOT_PROJECTS = {
    # excerpts of a project that needs a PostgreSQL server; the check itself is
    # tested in tests/test_index_check*.py and tests/test_index_advisor*.py
    "index-advisor.md",
    "index-check.md",
    # configures nplusone, the library the page migrates away from
    "migrating-from-nplusone.md",
}

# Packages that the release gate may find missing: not development dependencies
# of this project (sqlmodel needs SQLAlchemy below 2.1, so only the SQLAlchemy
# 2.0 CI jobs install it; pytest_asyncio is in the "plugins" group, whose CI
# job runs this file; nothing in CI installs litestar). Their recipe pages are
# projects like the others: where a package is missing, the test of the page is
# skipped.
OPTIONAL_PACKAGES = {"litestar", "pytest_asyncio", "sqlmodel"}

# Reports a page shows for another setup than the files it spells out, by one
# of their lines; the other reports of a page with a project must be, line for
# line, in the output of that project.
OTHER_SETUPS: dict[str, set[str]] = {
    # examples/async-fastapi, checked by tests/test_examples.py
    "getting-started.md": {"  first at app/main.py:37 in list_orders"},
    # the run on PostgreSQL (asyncpg) is in "Pitfall 2" of the page
    "recipes-pytest-asyncio.md": {
        "  sql: SELECT customer.id, customer.name FROM customer WHERE customer.id = $1::INTEGER"
    },
    # the run without the ignore pattern that the page then adds
    "recipes-factory-boy.md": {
        "  first at tests/test_orders.py:10 in test_seeded_in_the_body"
    },
}

# Pages that are history or design notes, not instructions.
NOT_CHECKED = {"design.md", "design-0.2.md", "design-0.3.md", "design-0.3-api.md"}

# Names that are the example's own packages, not installable ones.
LOCAL = {"app", "tests", "conftest"}

FENCE = re.compile(r"^```(python|py|ini)\n(.*?)^```", re.DOTALL | re.MULTILINE)
LABEL = re.compile(r"^`([\w./-]+\.(?:py|ini))`")


def _blocks(page: Path) -> list[tuple[int, str | None, str]]:
    """(line, file name or None, code) of each Python block of the page, and of
    each ini block that a paragraph introduces with a file name (`pytest.ini`)."""
    text = page.read_text(encoding="utf-8")
    found = []
    for match in FENCE.finditer(text):
        before = text[: match.start()].rstrip("\n")
        paragraph = before.rsplit("\n\n", 1)[-1]
        label = LABEL.match(paragraph)
        if match.group(1) == "ini" and not label:
            continue
        line = text[: match.start()].count("\n") + 2
        found.append((line, label.group(1) if label else None, match.group(2)))
    return found


def _ids(pages: list[Path]) -> list[str]:
    return [p.name for p in pages]


CHECKED = [p for p in PAGES if p.name not in NOT_CHECKED]
WITH_PROJECT = [
    p
    for p in CHECKED
    if p.name not in NOT_PROJECTS and any(label for _, label, _ in _blocks(p))
]


@pytest.mark.parametrize("page", CHECKED, ids=_ids(CHECKED))
def test_every_python_block_parses(page: Path) -> None:
    for line, label, code in _blocks(page):
        if label and label.endswith(".ini"):
            continue
        try:
            ast.parse(code)
        except SyntaxError as error:
            pytest.fail(f"{page.name}:{line}: {error}")


def test_every_other_setup_names_a_report_of_its_page() -> None:
    for name, lines in OTHER_SETUPS.items():
        reports = _reports((ROOT / "docs" / name).read_text(encoding="utf-8"))
        for line in lines:
            assert any(line in report for _, report in reports), (
                f"{name}: no report has {line!r}; drop it from OTHER_SETUPS"
            )


# page -> the example project whose files the page shows in full; the copies
# must not drift apart
SAME_FILES = {"recipes-fastapi.md": "fastapi-dependency-override"}


@pytest.mark.parametrize("name", sorted(SAME_FILES))
def test_a_page_shows_the_files_of_its_example(name: str) -> None:
    example = ROOT / "examples" / SAME_FILES[name]
    if not example.is_dir():
        pytest.skip("examples/ is not available")
    shown = {label: code for _, label, code in _blocks(ROOT / "docs" / name) if label}
    compared = [label for label in shown if (example / label).is_file()]
    assert len(compared) >= 6, compared
    for label in compared:
        if label == "pytest.ini":
            # the example also turns off the cache provider
            continue
        assert shown[label] == (example / label).read_text(encoding="utf-8"), (
            f"{name} shows {label} differently from examples/{SAME_FILES[name]}"
        )


def test_the_pages_with_projects_are_found() -> None:
    names = {p.name for p in WITH_PROJECT}
    assert {
        "getting-started.md",
        "recipes-factory-boy.md",
        "recipes-fastapi.md",
        "recipes-flask-sqlalchemy.md",
        "recipes-litestar.md",
        "recipes-pytest-asyncio.md",
        "recipes-sqlmodel.md",
    } <= names


def _imported_modules(sources: list[str]) -> set[str]:
    modules: set[str] = set()
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
    return {m for m in modules if m not in sys.stdlib_module_names | LOCAL}


@pytest.mark.parametrize("page", WITH_PROJECT, ids=_ids(WITH_PROJECT))
def test_the_project_of_a_page_runs(page: Path, tmp_path: Path) -> None:
    files: dict[str, str] = {}
    for _, label, code in _blocks(page):
        if label:
            assert label not in files, f"{page.name} shows {label} twice"
            files[label] = code
    missing = sorted(
        m
        for m in _imported_modules([c for n, c in files.items() if n.endswith(".py")])
        if find_spec(m) is None
    )
    if missing:
        message = f"{page.name} needs {missing}, which are not installed"
        if (
            os.environ.get("QUERYGUARD_REQUIRE_DOC_EXAMPLES")
            and not set(missing) <= OPTIONAL_PACKAGES
        ):
            pytest.fail(message)
        pytest.skip(message)

    project = tmp_path / "project"
    base = BASES.get(page.name)
    if base:
        example = ROOT / "examples" / base
        if not example.is_dir():
            pytest.skip("examples/ is not available")
        shutil.copytree(example, project, ignore=shutil.ignore_patterns("__pycache__"))
    project.mkdir(exist_ok=True)
    for name, code in files.items():
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
    # An empty __init__.py exists only where the page says so; one that the page
    # does not mention would change what pytest puts on sys.path.
    text = page.read_text(encoding="utf-8")
    for package in {Path(name).parent for name in files if name.endswith(".py")}:
        init = project / package / "__init__.py"
        if (
            package != Path(".")
            and f"`{package.as_posix()}/__init__.py`" in text
            and not init.exists()
        ):
            init.write_text("", encoding="utf-8")

    report = tmp_path / "report.xml"
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    # Not `python -m pytest`: python puts the project directory on sys.path, which
    # hides a page that forgets `pythonpath = .`, and a reader's `pytest` does not.
    launcher = (
        "import os, sys\n"
        "sys.path[:] = [p for p in sys.path if p not in ('', os.getcwd())]\n"
        "import pytest\n"
        "sys.exit(pytest.console_main())"
    )
    result = subprocess.run(
        [sys.executable, "-c", launcher, "-q", "-p", "no:cacheprovider"]
        + [f"--junitxml={report}"],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=300,
    )
    assert report.is_file(), result.stdout + result.stderr
    cases = list(ET.parse(report).getroot().iter("testcase"))
    assert cases, f"{page.name}: no tests ran\n{result.stdout}"
    problems = []
    for case in cases:
        name = f"{case.get('classname')}.{case.get('name')}"
        if case.find("error") is not None:
            problems.append(f"{name}: error\n{case.find('error').get('message')}")  # type: ignore[union-attr]
        failure = case.find("failure")
        if failure is not None:
            message = (failure.get("message") or "") + (failure.text or "")
            if "queryguard" not in message and "QueryBudgetExceeded" not in message:
                problems.append(
                    f"{name}: failed without a queryguard report\n{message}"
                )
    assert not problems, f"{page.name}:\n" + "\n".join(problems) + result.stdout
    printed = [line.rstrip() for line in result.stdout.splitlines() if line.strip()]
    stale = [
        f"{page.name}:{number}:\n" + "\n".join(report)
        for number, report in _reports(text)
        if not _contains(printed, report)
        and not OTHER_SETUPS.get(page.name, set()) & set(report)
    ]
    assert not stale, (
        "a report the page shows is not, line for line, in the output of its "
        "project:\n" + "\n\n".join(stale) + "\n\noutput:\n" + result.stdout
    )


# Lines that end a report: pytest separators and summaries, captured-output
# headers, a traceback's location line, and a budget report's timings, which
# differ from run to run.
_END = re.compile(
    r"===|___|---|(FAILED|PASSED|ERROR) |\d+ (failed|passed|error)|\S+:\d+: |"
    r"  database time:"
)


def _reports(text: str) -> list[tuple[int, list[str]]]:
    """(line number, lines) of each queryguard report in the page's output
    blocks (fences without a language): from a `queryguard: ...` line to the
    next pytest separator (`===`, `___`) or summary line, blank lines left out.
    A budget report ends before its timings, which differ from run to run."""
    found: list[tuple[int, list[str]]] = []
    fence: str | None = None
    report: list[str] | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("```"):
            fence = None if fence is not None else line[3:].strip()
            report = None
        elif fence != "":
            continue
        elif line.startswith("queryguard: "):
            report = [line.rstrip()]
            found.append((number, report))
        elif _END.match(line):
            report = None
        elif report is not None and line.strip():
            report.append(line.rstrip())
    return found


def _contains(lines: list[str], part: list[str]) -> bool:
    return any(
        lines[start : start + len(part)] == part
        for start in range(len(lines) - len(part) + 1)
    )
