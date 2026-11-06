"""The checks of the release gate that are not a plain pytest run.

    uv build --out-dir dist
    uv run python scripts/release_gate.py consistency
    uv run python scripts/release_gate.py accuracy
    uv run python scripts/release_gate.py contents dist
    uv run python scripts/release_gate.py install dist
    uv run python scripts/release_gate.py upgrade dist
    uv run python scripts/release_gate.py bench
    uv run python scripts/release_gate.py all dist      # everything above

Each check prints what it verified and exits 1 with the list of problems when
something is wrong. .github/workflows/release-gate.yml runs them (next to the
test matrix); nothing here tags, uploads or publishes anything. See
"Releasing" in CONTRIBUTING.md.

``install`` and ``upgrade`` create virtual environments in a temporary
directory and need network access to PyPI.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import venv
import zipfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
PACKAGE = "pytest_queryguard"
REPO_URL = "https://github.com/MaciejZiel/pytest-queryguard"

# PEP 440, the part of it that releases of this project use.
VERSION = r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?"


class Problems(list[str]):
    def check(self, condition: object, message: str) -> bool:
        if not condition:
            self.append(message)
        return bool(condition)


# -- 8: version, changelog date and links -----------------------------------


def package_version() -> str:
    source = (ROOT / "src" / PACKAGE / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', source, re.MULTILINE)
    if not match:
        raise SystemExit("src/pytest_queryguard/__init__.py has no __version__")
    return match.group(1)


# Where the documentation names the release it describes: file, pattern with
# one group for the version.
DOC_VERSIONS: dict[str, str] = {
    "docs/index.md": rf"These pages\s+describe\s+({VERSION})\b",
    "docs/getting-started.md": rf"`pytest-queryguard-({VERSION})`",
    "llms.txt": rf"This file describes ({VERSION})\b",
}


def check_doc_versions(version: str, texts: dict[str, str] | None = None) -> list[str]:
    """The version that docs/index.md, getting-started.md and llms.txt describe.

    ``texts`` maps the paths of ``DOC_VERSIONS`` to their content; by default
    the files of the repository are read.
    """
    problems = Problems()
    for path, pattern in DOC_VERSIONS.items():
        text = (
            texts[path]
            if texts is not None
            else (ROOT / path).read_text(encoding="utf-8")
        )
        found = re.findall(pattern, text)
        if problems.check(found, f"{path} does not say which version it describes"):
            for named in sorted(set(found) - {version}):
                problems.append(f"{path} describes {named}, __version__ is {version}")
    return problems


def check_consistency(
    changelog: str | None = None, version: str | None = None
) -> list[str]:
    """The version, the top changelog entry, its date and the link references.

    Without ``changelog`` the repository is checked, and then also the version
    its documentation describes (``check_doc_versions``).
    """
    problems = Problems()
    version = version or package_version()
    if changelog is None:
        problems.extend(check_doc_versions(version))
    text = (
        changelog
        if changelog is not None
        else (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    )
    problems.check(
        re.fullmatch(VERSION, version), f"version {version!r} is not PEP 440"
    )
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    problems.check(
        re.search(r'^dynamic = \["version"\]', pyproject, re.MULTILINE)
        and "[tool.hatch.version]" in pyproject,
        "pyproject.toml must take the version from __init__.py (dynamic version)",
    )

    headings = re.findall(
        r"^## \[([^\]]+)\](?: - (\S+))?\s*$", text, flags=re.MULTILINE
    )
    names = [name for name, _ in headings]
    problems.check(
        names[:1] == ["Unreleased"], "the changelog must start with [Unreleased]"
    )
    released = [(name, date) for name, date in headings if name != "Unreleased"]
    if not problems.check(released, "the changelog has no released entry"):
        return problems
    top = released[0][0]
    problems.check(
        top == version, f"top changelog entry is [{top}], __version__ is {version}"
    )
    dates = []
    for name, entry_date in released:
        try:
            dates.append(datetime.date.fromisoformat(entry_date or ""))
        except ValueError:
            problems.append(
                f"entry [{name}] has no valid YYYY-MM-DD date: {entry_date!r}"
            )
    if dates:
        problems.check(
            dates[0] <= datetime.datetime.now(datetime.timezone.utc).date(),
            f"the date of [{top}] ({dates[0]}) is in the future",
        )
        problems.check(
            dates == sorted(dates, reverse=True),
            "changelog dates must not increase from the top entry down",
        )
    problems.check(len(set(names)) == len(names), "a changelog heading is repeated")

    links = dict(re.findall(r"^\[([^\]]+)\]: (\S+)\s*$", text, flags=re.MULTILINE))
    for name in names:
        problems.check(name in links, f"[{name}] has no link reference at the bottom")
    problems.check(
        set(links) <= set(names),
        f"link references without a heading: {sorted(set(links) - set(names))}",
    )
    # [Unreleased] compares the newest tag with HEAD; each entry compares the
    # one below it with itself; the oldest points at its tag.
    problems.check(
        links.get("Unreleased") == f"{REPO_URL}/compare/v{top}...HEAD",
        f"[Unreleased] must be {REPO_URL}/compare/v{top}...HEAD, is {links.get('Unreleased')}",
    )
    for index, (name, _) in enumerate(released):
        if index + 1 < len(released):
            wanted = f"{REPO_URL}/compare/v{released[index + 1][0]}...v{name}"
        else:
            wanted = f"{REPO_URL}/tree/v{name}"
        problems.check(
            links.get(name) == wanted,
            f"[{name}] must link to {wanted}, is {links.get(name)}",
        )
    problems.check(
        bool(
            re.search(
                r"^### ",
                text.split(f"## [{top}]", 1)[-1].split("\n## [", 1)[0],
                re.MULTILINE,
            )
        ),
        f"the entry [{top}] has no ### section",
    )
    return problems


# -- 2: accuracy corpus thresholds ------------------------------------------


def check_accuracy() -> list[str]:
    sys.path.insert(0, str(ROOT))
    from tests.corpus import runner
    from tests.corpus.metrics import score

    thresholds = json.loads((SCRIPTS / "accuracy_thresholds.json").read_text())
    problems = Problems()
    scenarios = runner.load()
    with tempfile.TemporaryDirectory() as tmp:
        ini, _ = runner.CONFIGS["default"]
        names = [s.name for s in scenarios]
        results = runner.run_subprocess(Path(tmp) / "default", names, ini=ini)
    result = score("default", scenarios, results)
    problems.check(
        len(scenarios) >= thresholds["min_scenarios"],
        f"only {len(scenarios)} corpus scenarios ran, at least {thresholds['min_scenarios']} are required"
        f" (skipped: {runner.SKIPPED})",
    )
    for kind, wanted in thresholds["default"].items():
        tally = result.kinds.get(kind)
        if tally is None:
            problems.append(f"{kind}: not measured")
            continue
        for metric in ("precision", "recall"):
            value = getattr(tally, metric)
            shown = "n/a" if value is None else f"{value:.3f}"
            print(f"{kind} {metric}: {shown} (minimum {wanted[metric]})")
            problems.check(
                value is not None and value + 1e-9 >= wanted[metric],
                f"{kind} {metric} {shown} is below the recorded minimum {wanted[metric]}",
            )
    unexplained = [m.scenario for m in result.mismatches if not m.known]
    problems.check(not unexplained, f"unexplained corpus mismatches: {unexplained}")
    return problems


# -- 7: contents of the wheel and the sdist ---------------------------------

SCRATCH = re.compile(
    r"(^|/)(__pycache__|\.git|\.github|\.venv|\.idea|\.vscode|\.playwright-cli|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.coverage|\.env|\.DS_Store)(/|$)"
    r"|\.(pyc|pyo|orig|rej|swp|bak|log)$|~$"
)


def find_dist(dist: Path, version: str) -> tuple[Path, Path]:
    wheel = dist / f"pytest_queryguard-{version}-py3-none-any.whl"
    sdist = dist / f"pytest_queryguard-{version}.tar.gz"
    for path in (wheel, sdist):
        if not path.is_file():
            raise SystemExit(
                f"{path} does not exist; build with: uv build --out-dir {dist}"
            )
    return wheel, sdist


def check_contents(wheel: Path, sdist: Path, version: str) -> list[str]:
    problems = Problems()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        info = f"{PACKAGE}-{version}.dist-info"
        problems.check(f"{PACKAGE}/py.typed" in names, "the wheel has no py.typed")
        problems.check(f"{PACKAGE}/__init__.py" in names, "the wheel has no package")
        problems.check(
            any(
                n.startswith(f"{info}/licenses/") and n.endswith("LICENSE")
                for n in names
            ),
            "the wheel has no LICENSE in dist-info/licenses",
        )
        outside = [n for n in names if not n.startswith((f"{PACKAGE}/", info + "/"))]
        problems.check(
            not outside, f"files outside the package in the wheel: {outside}"
        )
        unwanted = [
            n
            for n in names
            if SCRATCH.search(n)
            or re.search(r"(^|/)(tests?|docs|scripts|examples)/", n)
        ]
        problems.check(not unwanted, f"tests or scratch files in the wheel: {unwanted}")
        entry_points = archive.read(f"{info}/entry_points.txt").decode()
        problems.check(
            re.search(rf"\[pytest11\]\s+queryguard = {PACKAGE}\.plugin", entry_points),
            "the wheel does not register the pytest11 entry point queryguard",
        )
        metadata = archive.read(f"{info}/METADATA").decode()
        problems.check(
            f"\nVersion: {version}\n" in metadata, "wheel METADATA has another version"
        )
        problems.check(
            "\nLicense-Expression: MIT\n" in metadata,
            "wheel METADATA lacks License-Expression: MIT",
        )
        problems.check(
            "Requires-Python: >=3.10" in metadata,
            "wheel METADATA lacks Requires-Python",
        )
        problems.check(
            len(metadata.split("\n\n", 1)[-1].strip()) > 500,
            "wheel METADATA has no long description",
        )
        sources = sorted(
            Path(n).name
            for n in names
            if n.startswith(f"{PACKAGE}/") and n.endswith(".py")
        )
        print(f"wheel: {len(names)} files, modules {', '.join(sources)}")
    root = f"pytest_queryguard-{version}"
    with tarfile.open(sdist) as archive:
        names = [m.name for m in archive.getmembers()]
    problems.check(
        all(n.startswith(root) for n in names),
        "sdist members outside the root directory",
    )
    relative = [n.removeprefix(root + "/") for n in names]
    for required in (
        "LICENSE",
        "README.md",
        "CHANGELOG.md",
        "pyproject.toml",
        "3rdparty_licenses.md",
        f"src/{PACKAGE}/py.typed",
        f"src/{PACKAGE}/__init__.py",
        "PKG-INFO",
    ):
        problems.check(required in relative, f"the sdist has no {required}")
    scratch = [n for n in relative if SCRATCH.search(n)]
    problems.check(not scratch, f"scratch files in the sdist: {scratch}")
    print(f"sdist: {len(names)} members")
    return problems


# -- 4 and 5: clean virtual environments ------------------------------------

TINY_TEST = """\
from sqlalchemy import ForeignKey, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customer"
    id: Mapped[int] = mapped_column(primary_key=True)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    customer: Mapped[Customer] = relationship()


def make_session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add_all(Customer(id=i) for i in range(1, 8))
    session.add_all(Order(id=i, customer_id=i) for i in range(1, 8))
    session.commit()
    session.expunge_all()
    return session


def test_one_query():
    session = make_session()
    assert len(session.scalars(select(Order)).all()) == 7


def test_n_plus_one():
    session = make_session()
    assert [o.customer.id for o in session.scalars(select(Order))] == list(range(1, 8))
"""


def run(
    command: list[str | Path], cwd: Path | None = None, timeout: int = 900
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONNOUSERSITE": "1"}
    for name in (
        "PYTEST_ADDOPTS",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "PIP_REQUIRE_VIRTUALENV",
    ):
        env.pop(name, None)
    return subprocess.run(
        [str(c) for c in command],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def make_venv(path: Path) -> Path:
    venv.EnvBuilder(with_pip=True, clear=True).create(path)
    bin_dir = "Scripts" if sys.platform == "win32" else "bin"
    return path / bin_dir / ("python.exe" if sys.platform == "win32" else "python")


def exercise(python: Path, project: Path, version: str, label: str) -> list[str]:
    """pip check, import, the plugin registered, collection and a run of a tiny project."""
    problems = Problems()
    pip_check = run([python, "-m", "pip", "check"])
    problems.check(
        pip_check.returncode == 0,
        f"{label}: pip check: {pip_check.stdout}{pip_check.stderr}",
    )
    probe = textwrap.dedent(
        f"""
        import importlib.metadata as m, pytest_queryguard
        assert pytest_queryguard.__version__ == {version!r}, pytest_queryguard.__version__
        assert m.version("pytest-queryguard") == {version!r}
        points = [e for e in m.entry_points(group="pytest11") if e.name == "queryguard"]
        assert [e.value for e in points] == ["pytest_queryguard.plugin"], points
        import pytest_queryguard.plugin
        print("import ok")
        """
    )
    imported = run([python, "-I", "-c", probe])
    problems.check(
        imported.returncode == 0, f"{label}: import/entry point: {imported.stderr}"
    )
    (project / "test_tiny.py").write_text(TINY_TEST, encoding="utf-8")
    base: list[str | Path] = [python, "-m", "pytest", "-p", "no:cacheprovider"]
    plugins = run([*base, "--trace-config", "--co", "-q"], cwd=project)
    problems.check(
        plugins.returncode == 0 and "test_tiny.py::test_n_plus_one" in plugins.stdout,
        f"{label}: pytest --co failed:\n{plugins.stdout}{plugins.stderr}",
    )
    problems.check(
        "pytest_queryguard" in plugins.stdout
        and "queryguard" in plugins.stdout.lower(),
        f"{label}: the plugin is not registered with pytest",
    )
    full = run([*base, "-q"], cwd=project)
    problems.check(
        full.returncode == 1
        and "1 failed, 1 passed" in full.stdout
        and "queryguard: N+1 detected" in full.stdout,
        f"{label}: the tiny project did not report its N+1:\n{full.stdout}{full.stderr}",
    )
    off = run([*base, "-q", "-p", "no:queryguard"], cwd=project)
    problems.check(
        off.returncode == 0,
        f"{label}: the tiny project fails without the plugin:\n{off.stdout}",
    )
    print(f"{label}: pip check, import, plugin, collection and a run are ok")
    return problems


def check_install(wheel: Path, sdist: Path, version: str) -> list[str]:
    problems = Problems()
    for artifact in (wheel, sdist):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            python = make_venv(work / "venv")
            project = work / "project"
            project.mkdir()
            installed = run(
                [python, "-m", "pip", "install", "--quiet", artifact.resolve()]
            )
            if not problems.check(
                installed.returncode == 0,
                f"installing {artifact.name}:\n{installed.stderr}",
            ):
                continue
            problems.extend(
                exercise(python, project, version, artifact.suffix.lstrip("."))
            )
    return problems


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def previous_release(version: str) -> str | None:
    """The newest final release on PyPI below ``version`` (None when there is none)."""
    with tempfile.TemporaryDirectory() as tmp:
        python = make_venv(Path(tmp) / "venv")
        found = run([python, "-m", "pip", "index", "versions", "pytest-queryguard"])
    match = re.search(r"Available versions: ([^\n]+)", found.stdout)
    if not match:
        return None
    key = lambda v: tuple(int(p) for p in v.split("."))
    finals = [
        v.strip()
        for v in match.group(1).split(",")
        if re.fullmatch(r"\d+\.\d+\.\d+", v.strip())
    ]
    base = version.split("rc")[0].split("a")[0].split("b")[0]
    older = [
        v
        for v in finals
        if _key(v) < _key(base) or (_key(v) == _key(base) and v != version)
    ]
    return max(older, key=key) if older else None


def check_upgrade(wheel: Path, version: str) -> list[str]:
    problems = Problems()
    previous = previous_release(version)
    if previous is None:
        problems.append("no earlier release of pytest-queryguard found on PyPI")
        return problems
    print(f"upgrading from {previous} to {version}")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        python = make_venv(work / "venv")
        project = work / "project"
        project.mkdir()
        old = run(
            [
                python,
                "-m",
                "pip",
                "install",
                "--quiet",
                f"pytest-queryguard=={previous}",
            ]
        )
        if not problems.check(
            old.returncode == 0, f"installing {previous}:\n{old.stderr}"
        ):
            return problems
        problems.extend(exercise(python, project, previous, f"before ({previous})"))
        new = run([python, "-m", "pip", "install", "--quiet", wheel.resolve()])
        if not problems.check(
            new.returncode == 0, f"upgrading to the wheel:\n{new.stderr}"
        ):
            return problems
        problems.extend(exercise(python, project, version, f"after ({version})"))
        problems.check(
            run([python, "-m", "pip", "list", "--format=freeze"]).stdout.count(
                "pytest-queryguard=="
            )
            == 1,
            "more than one pytest-queryguard after the upgrade",
        )
    return problems


# -- 6: overhead budget ------------------------------------------------------


def check_bench() -> list[str]:
    """scripts/bench_gate.py with the release margin: 25 % over the baseline, no extra slack.

    The ratios are relative (time with the measurement over time without),
    timed off and on interleaved in each of 7 processes; a result over the
    limit is measured again, up to 3 times in all, and the best counts.
    """
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "bench_gate.py"),
            "--factor",
            "1.25",
            "--slack",
            "0.0",
            "--processes",
            "7",
            "--rounds",
            "20",
            "--attempts",
            "3",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    print(result.stdout, end="")
    return (
        []
        if result.returncode == 0
        else [f"overhead budget: {result.stdout}{result.stderr}"]
    )


CHECKS: dict[str, str] = {
    "consistency": "version, changelog entry, date, links and docs version agree",
    "accuracy": "accuracy corpus precision and recall are at least the recorded minimums",
    "contents": "wheel and sdist contain what they should and nothing else",
    "install": "wheel and sdist install into clean environments and work",
    "upgrade": "the previous PyPI release upgrades to the wheel and works",
    "bench": "per-statement overhead is within 25 % of the baseline",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("check", choices=[*CHECKS, "all"])
    parser.add_argument("dist", nargs="?", type=Path, default=ROOT / "dist")
    args = parser.parse_args(argv)
    version = package_version()
    wanted = list(CHECKS) if args.check == "all" else [args.check]
    steps: dict[str, Callable[[], list[str]]] = {
        "consistency": check_consistency,
        "accuracy": check_accuracy,
        "contents": lambda: check_contents(*find_dist(args.dist, version), version),
        "install": lambda: check_install(*find_dist(args.dist, version), version),
        "upgrade": lambda: check_upgrade(find_dist(args.dist, version)[0], version),
        "bench": check_bench,
    }
    failed = False
    for name in wanted:
        print(f"== {name}: {CHECKS[name]}")
        problems = steps[name]()
        for problem in problems:
            print(f"FAIL {name}: {problem}")
        print(f"-- {name}: {'FAILED' if problems else 'ok'}")
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
