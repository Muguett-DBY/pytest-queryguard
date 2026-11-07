"""The documentation must stay in step with the code.

The configuration reference has to name every ini option, command-line flag and
marker the plugin registers, and the site has to build without broken links.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def _help(pytester: pytest.Pytester, *args: str) -> str:
    return pytester.runpytest_subprocess(*args).stdout.str()


def test_every_ini_option_is_in_the_reference(pytester: pytest.Pytester) -> None:
    reference = (DOCS / "configuration.md").read_text(encoding="utf-8")
    names = set(
        re.findall(r"^\s+(queryguard_\w+) \(", _help(pytester, "--help"), re.MULTILINE)
    )
    assert len(names) >= 10, names  # the regexp still finds the options
    missing = sorted(name for name in names if f"`{name}`" not in reference)
    assert not missing, f"docs/configuration.md does not document {missing}"


def test_every_flag_is_in_the_reference(pytester: pytest.Pytester) -> None:
    reference = (DOCS / "configuration.md").read_text(encoding="utf-8")
    flags = set(re.findall(r"(--(?:no-)?queryguard-[\w-]+)", _help(pytester, "--help")))
    assert len(flags) >= 5, flags
    missing = sorted(flag for flag in flags if flag not in reference)
    assert not missing, f"docs/configuration.md does not document {missing}"


def test_every_marker_is_in_the_reference(pytester: pytest.Pytester) -> None:
    reference = (DOCS / "configuration.md").read_text(encoding="utf-8")
    markers = set(
        re.findall(
            r"@pytest\.mark\.((?:allow|max|queryguard)_\w+)",
            _help(pytester, "--markers"),
        )
    )
    assert markers >= {
        "max_queries",
        "max_db_time",
        "allow_nplusone",
        "queryguard_ignore",
        "queryguard_max_queries",
        "queryguard_max_db_time",
    }, markers
    missing = sorted(m for m in markers if f"`{m}" not in reference)
    assert not missing, f"docs/configuration.md does not document {missing}"


def test_site_builds_in_strict_mode(tmp_path: Path) -> None:
    if not (ROOT / "scripts" / "build_docs.py").is_file():
        pytest.skip("scripts/ is not in the source distribution")
    pytest.importorskip("markdown")
    pytest.importorskip("pygments")
    if sys.version_info < (3, 11):
        pytest.skip("scripts/build_docs.py reads docs/site.toml with tomllib")
    output = tmp_path / "site-café"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            str(ROOT / "scripts" / "build_docs.py"),
            "--strict",
            "--out",
            str(output),
        ],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"into {output}" in result.stdout
    assert (output / "index.html").is_file()


def test_the_stability_policy_has_the_pre_1_0_clause() -> None:
    policy = (DOCS / "stability.md").read_text(encoding="utf-8")
    assert "Pre-1.0 clause for new checks" in policy
    assert "From 1.0 on, the rules above apply strictly" in policy


@pytest.mark.skipif(
    not (ROOT / "CHANGELOG.md").is_file(), reason="CHANGELOG is not shipped"
)
def test_the_changelog_lists_default_changes_first() -> None:
    """Defaults changed by a new check are named at the top of the entry and
    are the first things under "Changed", as the pre-1.0 clause requires."""
    log = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    # The newest entry that has content: [Unreleased] while a release is being
    # prepared, the latest version once it has been cut.
    entry = next(
        part
        for part in log.split("\n## [")[1:]
        if "\n### Added" in part and "**Behavior changes**" in part
    )
    intro, rest = entry.split("\n### Added", 1)
    for phrase in ("column-load", "index advisor", "SQLite"):
        assert phrase in intro, phrase
    changed = rest.split("\n### Changed", 1)[1].split("\n### ")[0]
    first_group = changed.strip().splitlines()[0]
    assert first_group == "**Behavior changes**"
    behavior = changed.split("**Internals**")[0]
    for phrase in (
        "`column-load` finding kind is on by default",
        "advisor is on by default",
        "now also checks SQLite",
    ):
        assert phrase in behavior, phrase


def _slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


@pytest.mark.skipif(not (ROOT / "README.md").is_file(), reason="README is not shipped")
def test_readme_anchors_into_the_docs_exist() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    site = "https://maciejziel.github.io/pytest-queryguard/"
    links = [
        (f"docs/{page}.md", anchor)
        for page, anchor in re.findall(
            rf"\]\({re.escape(site)}([\w.-]+)\.html#([\w-]+)\)", readme
        )
    ]
    assert ("docs/faq.md", "does-it-slow-my-tests-down") in links
    for path, anchor in links:
        headings = re.findall(
            r"^#+ (.+)$", (ROOT / path).read_text(encoding="utf-8"), re.MULTILINE
        )
        assert anchor in {_slug(h) for h in headings}, (path, anchor)


def test_the_faq_states_the_overhead_with_its_conditions() -> None:
    faq = (DOCS / "faq.md").read_text(encoding="utf-8")
    section = faq.split("## Does it slow my tests down?")[1].split("\n## ")[0]
    assert "in-memory SQLite" in section and "micro-benchmark" in section
    assert "ns per" in section or "ns with" in section
