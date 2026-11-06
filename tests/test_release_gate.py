"""scripts/release_gate.py must pass on this repository and fail on a bad release."""

from __future__ import annotations

import importlib.util
import json
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


def test_the_accuracy_thresholds_are_recorded_in_the_docs() -> None:
    thresholds = json.loads(
        (ROOT / "scripts" / "accuracy_thresholds.json").read_text(encoding="utf-8")
    )
    page = (ROOT / "docs" / "accuracy.md").read_text(encoding="utf-8")
    for kind, wanted in thresholds["default"].items():
        row = f"| `{kind}` | {100 * wanted['precision']:.0f}% | {100 * wanted['recall']:.0f}% |"
        assert row in page, row
    assert str(thresholds["min_scenarios"]) in page
