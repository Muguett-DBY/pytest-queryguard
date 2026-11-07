"""scripts/release_gate.py must pass on this repository and fail on a bad release."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_gate.py"

pytestmark = pytest.mark.skipif(
    not SCRIPT.is_file(), reason="scripts/ is not in the source distribution"
)

REPO = "https://github.com/MaciejZiel/pytest-queryguard"
GOOD = f"""\
# Changelog

## [Unreleased]

## [1.2.0] - 2026-02-03

### Added

- Something.

## [1.1.0] - 2026-01-02

### Fixed

- Something else.

[Unreleased]: {REPO}/compare/v1.2.0...HEAD
[1.2.0]: {REPO}/compare/v1.1.0...v1.2.0
[1.1.0]: {REPO}/tree/v1.1.0
"""


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_gate", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_gate"] = module
    spec.loader.exec_module(module)
    return module


def test_the_repository_is_consistent(gate: ModuleType) -> None:
    assert gate.check_consistency() == []


def test_a_good_changelog_passes(gate: ModuleType) -> None:
    assert gate.check_consistency(GOOD, "1.2.0") == []


@pytest.mark.parametrize(
    ("old", "new", "version", "expected"),
    [
        ("## [1.2.0] - 2026-02-03", "## [1.2.0] - soon", "1.2.0", "valid"),
        ("## [1.2.0] - 2026-02-03", "## [1.2.0] - 2099-02-03", "1.2.0", "future"),
        ("## [1.1.0] - 2026-01-02", "## [1.1.0] - 2026-03-02", "1.2.0", "increase"),
        ("[1.2.0]: " + REPO + "/compare/v1.1.0...v1.2.0\n", "", "1.2.0", "no link"),
        ("compare/v1.2.0...HEAD", "compare/v1.1.0...HEAD", "1.2.0", "Unreleased"),
        ("compare/v1.1.0...v1.2.0", "compare/v1.0.0...v1.2.0", "1.2.0", "must link"),
        ("## [Unreleased]\n\n", "", "1.2.0", "start with"),
        ("### Added", "Added", "1.2.0", "no ### section"),
        ("GOOD", "GOOD", "1.3.0", "__version__"),
        ("GOOD", "GOOD", "1.2", "PEP 440"),
    ],
)
def test_a_bad_changelog_fails(
    gate: ModuleType, old: str, new: str, version: str, expected: str
) -> None:
    text = GOOD.replace(old, new) if old != "GOOD" else GOOD
    problems = gate.check_consistency(text, version)
    assert any(expected in problem for problem in problems), problems


DOCS = {
    "docs/index.md": (
        "Nothing to configure. These pages\ndescribe 1.2.0, see Install. Parts\n"
        'marked "since 1.3.0" describe the next release; they are on `main` but '
        "not on\nPyPI yet."
    ),
    "docs/getting-started.md": "`pytest -VV` lists it\n(`pytest-queryguard-1.2.0`).",
    "llms.txt": (
        "This file sums up the documentation.\nThis file describes 1.2.0; parts "
        'marked "since 1.3.0" are on `main` but not on PyPI yet. Needs'
    ),
}


def test_the_docs_describe_the_version(gate: ModuleType) -> None:
    assert gate.check_doc_versions("1.2.0", DOCS) == []
    assert gate.check_doc_versions(gate.package_version()) == []


@pytest.mark.parametrize("path", sorted(DOCS))
def test_docs_that_describe_another_version_fail(gate: ModuleType, path: str) -> None:
    texts = {**DOCS, path: DOCS[path].replace("1.2.0", "1.1.0")}
    problems = gate.check_doc_versions("1.2.0", texts)
    assert problems == [f"{path} describes 1.1.0, __version__ is 1.2.0"]


@pytest.mark.parametrize("path", sorted(DOCS))
def test_docs_without_a_version_fail(gate: ModuleType, path: str) -> None:
    texts = {**DOCS, path: DOCS[path].replace("1.2.0", "the next release")}
    problems = gate.check_doc_versions("1.2.0", texts)
    assert problems == [f"{path} does not say which version it describes"]


def test_a_missing_file_is_a_problem(gate: ModuleType) -> None:
    texts = {k: v for k, v in DOCS.items() if k != "llms.txt"}
    problems = gate.check_doc_versions("1.2.0", texts)
    assert problems == ["llms.txt does not say which version it describes"]


def test_a_pre_release_may_describe_the_last_final_release(gate: ModuleType) -> None:
    # pip installs the last final release, so the docs stay on it during an rc
    assert gate.check_doc_versions("1.3.0rc1", DOCS) == []
    itself = {k: v.replace("1.2.0", "1.3.0rc1") for k, v in DOCS.items()}
    assert gate.check_doc_versions("1.3.0rc1", itself) == []
    unreleased = {k: v.replace("1.2.0", "1.3.0") for k, v in DOCS.items()}
    problems = gate.check_doc_versions("1.3.0rc1", unreleased)
    assert len(problems) == 3
    assert all("describes 1.3.0, __version__ is 1.3.0rc1" in p for p in problems)


@pytest.mark.parametrize("path", ["docs/index.md", "llms.txt"])
def test_a_released_version_must_not_be_pending(gate: ModuleType, path: str) -> None:
    # the release bumped the version but kept the "not on PyPI yet" note
    texts = {k: v.replace("1.2.0", "1.3.0") for k, v in DOCS.items()}
    problems = gate.check_doc_versions("1.3.0", texts)
    assert f"{path} says 1.3.0 is not on PyPI yet, __version__ is 1.3.0" in problems
    assert len(problems) == 2


def _ci(tag: str) -> str:
    return (
        f"      - uses: MaciejZiel/pytest-queryguard/action@{tag}   # a release tag\n"
    )


def test_the_action_pin_is_the_release(gate: ModuleType) -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert gate.check_action_pin(gate.package_version(), changelog) == []
    assert gate.check_action_pin("1.2.0", GOOD, _ci("v1.2.0")) == []
    # a pre-release keeps the newest final release
    rc = GOOD.replace(
        "## [Unreleased]\n", "## [Unreleased]\n\n## [1.3.0rc1] - 2026-03-01\n"
    )
    assert gate.check_action_pin("1.3.0rc1", rc, _ci("v1.2.0")) == []
    assert gate.check_action_pin("1.3.0rc1", rc, _ci("v1.3.0rc1")) != []


@pytest.mark.parametrize(
    ("version", "ci", "message"),
    [
        ("1.2.0", "v1.1.0", "pins the action to v1.1.0; it must be v1.2.0"),
        ("1.2.0", "v1.3.0", "pins the action to v1.3.0; it must be v1.2.0"),
        ("1.2.0", "COMMIT_SHA", "does not pin the action to a release tag"),
    ],
)
def test_a_stale_action_pin_fails(
    gate: ModuleType, version: str, ci: str, message: str
) -> None:
    problems = gate.check_action_pin(version, GOOD, _ci(ci))
    assert any(message in problem for problem in problems), problems


def test_a_pre_release_without_a_final_release_says_so(gate: ModuleType) -> None:
    only_rc = "## [Unreleased]\n\n## [1.0.0rc1] - 2026-03-01\n"
    (problem,) = gate.check_action_pin("1.0.0rc1", only_rc, _ci("v0.9.0"))
    assert "no final release" in problem


def test_every_action_pin_is_checked(gate: ModuleType) -> None:
    text = _ci("v1.2.0") + _ci("v1.1.0")
    assert gate.check_action_pin("1.2.0", GOOD, text) == [
        "docs/ci.md pins the action to v1.1.0; it must be v1.2.0 (__version__ is 1.2.0)"
    ]


def test_the_accuracy_thresholds_are_recorded_in_the_docs() -> None:
    thresholds = json.loads(
        (ROOT / "scripts" / "accuracy_thresholds.json").read_text(encoding="utf-8")
    )
    page = (ROOT / "docs" / "accuracy.md").read_text(encoding="utf-8")
    for kind, wanted in thresholds["default"].items():
        row = f"| `{kind}` | {100 * wanted['precision']:.0f}% | {100 * wanted['recall']:.0f}% |"
        assert row in page, row
    assert str(thresholds["min_scenarios"]) in page


# -- wheel METADATA ---------------------------------------------------------

CI_MATRIX = """\
jobs:
  test:
    strategy:
      matrix:
        python-version: ["3.10", "3.11"]
        include:
          - python-version: "3.12"
  free-threaded:
    steps:
      - uses: astral-sh/setup-uv@v1
        with:
          python-version: "3.12t"
"""
GATE_PINS = 'run: uv pip install "sqlalchemy==2.0.13" "pytest==8.0.*"\n'


def _metadata(
    version: str = "0.9.0",
    status: str = "4 - Beta",
    pythons: tuple[str, ...] = ("3.10", "3.11", "3.12"),
    requires_python: str = ">=3.10",
    requires: tuple[str, ...] = ("pytest>=8", "sqlalchemy>=2.0.13"),
    extra: tuple[str, ...] = ("Framework :: Pytest", "Typing :: Typed"),
) -> str:
    lines = [
        "Metadata-Version: 2.5",
        "Name: pytest-queryguard",
        f"Version: {version}",
        "Project-URL: Homepage, https://example.org",
        "Project-URL: Documentation, https://example.org/docs",
        "Project-URL: Changelog, https://example.org/changes",
        "Project-URL: Issues, https://example.org/issues",
        "License-Expression: MIT",
        "License-File: LICENSE",
        f"Classifier: Development Status :: {status}",
        *(f"Classifier: {c}" for c in extra),
        *(f"Classifier: Programming Language :: Python :: {p}" for p in pythons),
        f"Requires-Python: {requires_python}",
        *(f"Requires-Dist: {r}" for r in requires),
        "Description-Content-Type: text/markdown",
    ]
    return "\n".join(lines) + "\n\n" + "# pytest-queryguard\n\n" + "words " * 100


def _check(gate: ModuleType, metadata: str) -> list[str]:
    version = re.search(r"^Version: (\S+)$", metadata, re.MULTILINE)
    assert version
    problems: list[str] = gate.check_metadata(
        metadata, version.group(1), CI_MATRIX, GATE_PINS
    )
    return problems


def test_the_ci_versions_and_oldest_pins_are_read(gate: ModuleType) -> None:
    assert gate.ci_python_versions(CI_MATRIX) == ["3.10", "3.11", "3.12"]
    assert gate.oldest_tested(GATE_PINS) == {"sqlalchemy": "2.0.13", "pytest": "8.0"}
    # the repository's own workflows
    assert gate.ci_python_versions()[0] == "3.10"
    assert set(gate.oldest_tested()) == {"sqlalchemy", "pytest"}


def test_the_python_classifiers_are_the_ci_matrix(gate: ModuleType) -> None:
    # Without building: the classifiers in pyproject.toml are the versions
    # ci.yml tests, so a new CI version cannot ship unlisted (0.8.0 lacked 3.15).
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.findall(r'"Programming Language :: Python :: (3\.\d+)"', pyproject)
    assert declared == gate.ci_python_versions()
    assert f'requires-python = ">={declared[0]}"' in pyproject


def test_good_metadata_passes(gate: ModuleType) -> None:
    assert _check(gate, _metadata()) == []
    assert _check(gate, _metadata("1.0.0rc1")) == []
    assert _check(gate, _metadata("0.1.0", status="3 - Alpha")) == []
    stable = "5 - Production/Stable"
    assert _check(gate, _metadata("1.0.0", status=stable)) == []
    # 1.x post-releases stay stable
    assert _check(gate, _metadata("1.0.0.post1", status=stable)) == []
    # an upper bound, an extra or a marker does not hide the lower bound
    upper = ("pytest<10,>=8", "sqlalchemy[asyncio]>=2.0.13; python_version >= '3.10'")
    assert _check(gate, _metadata(requires=upper)) == []
    # 8 and 8.0 are the same bound
    assert _check(gate, _metadata(requires=("pytest>=8.0", "sqlalchemy>=2.0.13"))) == []


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (
            _metadata(pythons=("3.10", "3.11")),
            "Python classifiers ['3.10', '3.11'] are not the versions ci.yml tests",
        ),
        (
            _metadata(pythons=("3.10", "3.11", "3.12", "3.13")),
            "are not the versions ci.yml tests ['3.10', '3.11', '3.12']",
        ),
        (_metadata(requires_python=">=3.9"), "ci.yml tests from 3.10"),
        (
            _metadata(requires=("pytest>=8", "sqlalchemy>=2.0")),
            "Requires-Dist sqlalchemy>=2.0 is not the oldest version",
        ),
        (
            _metadata(requires=("pytest", "sqlalchemy>=2.0.13")),
            "Requires-Dist has no lower bound for pytest",
        ),
        (_metadata(extra=("Framework :: Pytest",)), "lacks Typing :: Typed"),
        (_metadata(extra=("Typing :: Typed",)), "lacks Framework :: Pytest"),
        (
            _metadata().replace(
                "Project-URL: Issues, https://example.org/issues\n", ""
            ),
            "has no Issues URL",
        ),
        (_metadata().replace("text/markdown", "text/x-rst"), "is not text/markdown"),
        (_metadata().replace("License-File: LICENSE\n", ""), "lacks License-File"),
        (
            _metadata("0.9.0", status="5 - Production/Stable"),
            "0.9.0 has Development Status",
        ),
    ],
    ids=[
        "python-missing",
        "python-untested",
        "requires-python",
        "sqlalchemy-bound",
        "pytest-unbounded",
        "typed",
        "framework",
        "issues-url",
        "content-type",
        "license-file",
        "status",
    ],
)
def test_bad_metadata_fails(gate: ModuleType, metadata: str, message: str) -> None:
    problems = _check(gate, metadata)
    assert any(message in problem for problem in problems), problems


def test_a_final_1x_release_must_be_stable(gate: ModuleType) -> None:
    (problem,) = _check(gate, _metadata("1.0.0"))
    assert "a final 1.x release says Development Status :: 5" in problem
    # a pre-release of 1.0 may stay Beta
    assert _check(gate, _metadata("1.0.0rc1")) == []


def test_a_pre_release_python_needs_no_classifier(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "PRE_RELEASE_PYTHONS", frozenset({"3.12"}))
    assert gate.ci_python_versions(CI_MATRIX) == ["3.10", "3.11"]
    assert _check(gate, _metadata(pythons=("3.10", "3.11"))) == []


def test_missing_oldest_pins_fail(gate: ModuleType) -> None:
    metadata = _metadata()
    problems = gate.check_metadata(metadata, "0.9.0", CI_MATRIX, "run: true\n")
    assert any(
        "oldest pytest and SQLAlchemy pins were not found" in p for p in problems
    )
