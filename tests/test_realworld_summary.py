"""scripts/realworld_summary.py and the files .github/workflows/realworld.yml uses."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SCRIPT = SCRIPTS / "realworld_summary.py"

pytestmark = pytest.mark.skipif(
    not SCRIPT.is_file(), reason="scripts/ is not in the source distribution"
)


@pytest.fixture(scope="module")
def summary() -> ModuleType:
    spec = importlib.util.spec_from_file_location("realworld_summary", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up
    spec.loader.exec_module(module)
    return module


def summarize(summary: ModuleType, *args: Any, **kwargs: Any) -> tuple[str, list[str]]:
    """The report and the plugin's problems."""
    result = summary.summarize(*args, **kwargs)
    return result.report, result.problems


def junit(*cases: tuple[str, str]) -> str:
    """JUnit XML as pytest writes it; each case is (name, outcome)."""
    body = []
    for name, outcome in cases:
        inner = {
            "passed": "",
            "failed": '<failure message="AssertionError: boom&#10;more">trace</failure>',
            "error": '<error message="fixture broke">trace</error>',
            "skipped": '<skipped type="pytest.skip" message="no">skip</skipped>',
        }[outcome]
        body.append(
            f'<testcase classname="tests.test_app" name="{name}" time="0.1">'
            f"{inner}</testcase>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites>'
        f'<testsuite name="pytest">{"".join(body)}</testsuite></testsuites>'
    )


def finding(kind: str, line: int, label: str = "Order.customer") -> dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "count": 10,
        "file": "app/orders.py",
        "function": "list_orders",
        "line": line,
        "stack": [],
        "baselined": False,
    }


def write_runs(
    directory: Path,
    baseline: str | None,
    plugin: str | None,
    *,
    baseline_meta: dict[str, Any] | None = None,
    plugin_meta: dict[str, Any] | None = None,
    counts: dict[str, Any] | None = None,
    probe: list[dict[str, Any]] | None = None,
) -> Path:
    if baseline is not None:
        (directory / "baseline.xml").write_text(baseline, encoding="utf-8")
    if plugin is not None:
        (directory / "plugin.xml").write_text(plugin, encoding="utf-8")
    meta_b = baseline_meta if baseline_meta is not None else {"exit": 0, "seconds": 10}
    meta_p = plugin_meta if plugin_meta is not None else {"exit": 0, "seconds": 12.5}
    (directory / "baseline.meta.json").write_text(json.dumps(meta_b), encoding="utf-8")
    (directory / "plugin.meta.json").write_text(json.dumps(meta_p), encoding="utf-8")
    if counts is not None:
        (directory / "queryguard.json").write_text(json.dumps(counts), encoding="utf-8")
    if probe is not None:
        (directory / "probe.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in probe), encoding="utf-8"
        )
    return directory


def counts_file(
    tests: dict[str, list[dict[str, Any]]], complete: bool = True
) -> dict[str, Any]:
    return {
        "format": 1,
        "tool": {"name": "pytest-queryguard", "version": "0.6.0"},
        "complete": complete,
        "tests": {
            test: {"statements": 3, "by_verb": {"select": 3}, "findings": findings}
            for test, findings in tests.items()
        },
    }


def test_a_clean_run_reports_findings_and_overhead(
    summary: ModuleType, tmp_path: Path
) -> None:
    cases = junit(("test_a", "passed"), ("test_b", "passed"), ("test_c", "skipped"))
    counts = counts_file(
        {
            "tests/test_app.py::test_a": [
                finding("relationship-load", 12),
                finding("unused-columns", 30, "Order"),
            ],
            "tests/test_app.py::test_b": [finding("relationship-load", 14)],
        }
    )
    write_runs(tmp_path, cases, cases, counts=counts, probe=[])
    report, problems = summarize(summary, tmp_path, "demo", "org/demo@v1")

    assert problems == []
    assert report.startswith("## demo @ `org/demo@v1`")
    assert "| passed | 2 | 2 (+0) |" in report
    assert "| skipped | 1 | 1 (+0) |" in report
    assert "Overhead: **+25.0 %**" in report
    assert "### Tests that pass only without the plugin: 0" in report
    assert "### Internal warnings: 0, internal errors: 0" in report
    assert "### Findings: 3" in report
    assert "2 tests measured, 6 statements" in report
    assert "| `relationship-load` | 2 | 2 |" in report
    assert "| `unused-columns` | 1 | 1 |" in report
    assert "- `app/orders.py:12` Order.customer ×10 in `tests/test_app.py::test_a`" in (
        report
    )
    assert "### Problems" not in report


def test_samples_are_limited_per_kind(summary: ModuleType, tmp_path: Path) -> None:
    cases = junit(("test_a", "passed"))
    counts = counts_file(
        {"t::a": [finding("repeated-statement", line) for line in range(1, 9)]}
    )
    write_runs(tmp_path, cases, cases, counts=counts, probe=[])
    report, _ = summarize(summary, tmp_path, "demo", "v1", samples=3)
    assert report.count("- `app/orders.py:") == 3
    assert "| `repeated-statement` | 8 | 1 |" in report


def test_a_test_the_plugin_breaks_is_a_problem(
    summary: ModuleType, tmp_path: Path
) -> None:
    write_runs(
        tmp_path,
        junit(("test_a", "passed"), ("test_b", "passed"), ("test_c", "failed")),
        junit(("test_a", "failed"), ("test_b", "error"), ("test_c", "passed")),
        plugin_meta={"exit": 1, "seconds": 11},
        counts=counts_file({}),
    )
    report, problems = summarize(summary, tmp_path, "demo", "v1")

    assert problems == ["2 test(s) pass only without the plugin"]
    assert "### Tests that pass only without the plugin: 2" in report
    # first line of the message only
    assert "- `tests.test_app::test_a`: AssertionError: boom\n" in report
    assert "- `tests.test_app::test_b`: fixture broke" in report
    assert "1 test(s) failed without the plugin and passed with it (flaky?)" in report
    assert "### Problems\n\n- 2 test(s) pass only without the plugin" in report


def test_a_teardown_error_counts_as_a_failure(
    summary: ModuleType, tmp_path: Path
) -> None:
    """pytest writes a second testcase for an error in teardown."""
    write_runs(
        tmp_path,
        junit(("test_a", "passed")),
        junit(("test_a", "passed"), ("test_a", "error")),
        counts=counts_file({}),
    )
    _, problems = summarize(summary, tmp_path, "demo", "v1")
    assert problems == ["1 test(s) pass only without the plugin"]


def test_internal_warnings_and_errors_are_problems(
    summary: ModuleType, tmp_path: Path
) -> None:
    cases = junit(("test_a", "passed"))
    probe = [
        {"event": "internal-warning", "message": "listener failed", "nodeid": "t::a"},
        {"event": "internal-warning", "message": "listener failed", "nodeid": "t::b"},
        {"event": "internal-warning", "message": "other", "nodeid": "t::c"},
        {"event": "internal-error", "message": "Traceback\nKeyError: x"},
    ]
    write_runs(tmp_path, cases, cases, counts=counts_file({}), probe=probe)
    (tmp_path / "probe.jsonl").write_text(
        (tmp_path / "probe.jsonl").read_text() + "not json\n\n", encoding="utf-8"
    )
    report, problems = summarize(summary, tmp_path, "demo", "v1")

    assert problems == ["3 internal warning(s), 1 internal error(s)"]
    assert "### Internal warnings: 3, internal errors: 1" in report
    assert "- 2× listener failed (first in `t::a`)" in report
    assert "- 1× other (first in `t::c`)" in report
    assert "- INTERNALERROR: Traceback" in report


def test_missing_and_incomplete_outputs_of_the_plugin_run(
    summary: ModuleType, tmp_path: Path
) -> None:
    write_runs(
        tmp_path,
        junit(("test_a", "passed")),
        None,
        plugin_meta={"exit": 124, "seconds": 720},
    )
    report, problems = summarize(summary, tmp_path, "demo", "v1")
    assert problems == [
        "the run with the plugin left no JUnit XML",
        "the run with the plugin ended with 124 (timed out)",
    ]
    assert "| passed | 1 | no JUnit XML |" in report
    assert "Overhead: not comparable" in report
    assert "No queryguard JSON file." in report

    other = tmp_path / "other"
    other.mkdir()
    cases = junit(("test_a", "passed"))
    write_runs(other, cases, cases, counts=counts_file({}, complete=False))
    _, problems = summarize(summary, other, "demo", "v1")
    assert problems == ["the queryguard JSON says the run is incomplete"]

    third = tmp_path / "third"
    third.mkdir()
    write_runs(third, cases, cases)
    _, problems = summarize(summary, third, "demo", "v1")
    assert problems == ["the run with the plugin wrote no queryguard JSON"]


def test_a_test_missing_from_the_plugin_run_is_a_problem(
    summary: ModuleType, tmp_path: Path
) -> None:
    write_runs(
        tmp_path,
        junit(("test_a", "passed"), ("test_b", "passed")),
        junit(("test_a", "passed")),
        counts=counts_file({}),
    )
    report, problems = summarize(summary, tmp_path, "demo", "v1")
    assert problems == ["1 test(s) did not run with the plugin"]
    assert "for example `tests.test_app::test_b`" in report


def test_a_baseline_without_junit_is_not_the_plugins_problem(
    summary: ModuleType, tmp_path: Path
) -> None:
    write_runs(
        tmp_path,
        None,
        junit(("test_a", "passed")),
        baseline_meta={"exit": 4, "seconds": 1},
        plugin_meta={"exit": 4, "seconds": 1},
        counts=counts_file({}),
    )
    result = summary.summarize(tmp_path, "demo", "v1")
    assert result.problems == []
    assert "The run without the plugin left no JUnit XML." in result.report
    assert "| exit status | 4 (usage error) | 4 (usage error) |" in result.report
    (problem,) = result.harness
    assert problem.startswith("the tests did not run without the plugin (4 (usage")
    assert "### Harness problems" in result.report


def test_a_usage_error_in_both_runs_is_a_harness_problem_only(
    summary: ModuleType, tmp_path: Path
) -> None:
    """The first run of the full-stack template: a wrong test path.

    pytest stops with a usage error in both runs and the plugin's JSON says the
    run is incomplete; that is the harness's problem, not the plugin's.
    """
    empty = junit()
    write_runs(
        tmp_path,
        empty,
        empty,
        baseline_meta={"exit": 4, "seconds": 0.2},
        plugin_meta={"exit": 4, "seconds": 0.4},
        counts=counts_file({}, complete=False),
    )
    result = summary.summarize(tmp_path, "demo", "v1")
    assert result.problems == []
    assert len(result.harness) == 1
    assert summary.main(["--dir", str(tmp_path), "--project", "p", "--ref", "r"]) == 2


def test_unreadable_files_do_not_crash(summary: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "baseline.xml").write_text("<testsuites><broken", encoding="utf-8")
    (tmp_path / "baseline.meta.json").write_text('{"exit": "x"}', encoding="utf-8")
    (tmp_path / "plugin.meta.json").write_text("[1]", encoding="utf-8")
    (tmp_path / "queryguard.json").write_text("[]", encoding="utf-8")
    report, _ = summarize(summary, tmp_path, "demo", "v1")
    assert "| exit status | unknown | unknown |" in report
    assert "| wall time | unknown | unknown |" in report


def test_main_exit_status(
    summary: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = junit(("test_a", "passed"))
    write_runs(tmp_path, cases, cases, counts=counts_file({}))
    assert summary.main(["--dir", str(tmp_path), "--project", "p", "--ref", "r"]) == 0
    assert capsys.readouterr().out.startswith("## p @ `r`")
    write_runs(tmp_path, cases, junit(("test_a", "failed")))
    assert summary.main(["--dir", str(tmp_path), "--project", "p", "--ref", "r"]) == 1


def test_findings_alone_exit_zero(
    summary: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = junit(("test_a", "passed"))
    counts = counts_file({"t::a": [finding("relationship-load", 3)]})
    write_runs(tmp_path, cases, cases, counts=counts)
    assert summary.main(["--dir", str(tmp_path), "--project", "p", "--ref", "r"]) == 0
    assert "### Findings: 1" in capsys.readouterr().out


def test_the_json_numbers_and_the_table(
    summary: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "good"
    good.mkdir()
    write_runs(
        good,
        junit(("test_a", "passed"), ("test_b", "passed"), ("test_c", "skipped")),
        junit(("test_a", "passed"), ("test_b", "failed"), ("test_c", "skipped")),
        plugin_meta={"exit": 1, "seconds": 12.5},
        counts=counts_file(
            {
                "t::a": [finding("relationship-load", 3), finding("unused-columns", 4)],
                "t::b": [finding("relationship-load", 5)],
            }
        ),
        probe=[{"event": "internal-warning", "message": "x", "nodeid": "t::a"}],
    )
    (good / "versions.txt").write_text(
        "3.12.1 (main)\nsqlalchemy 2.0.35\npytest 9.2.0\npytest-queryguard 0.8.0\n",
        encoding="utf-8",
    )
    out = good / "summary.json"
    args = ["--dir", str(good), "--project", "good", "--ref", "o/g@1"]
    assert summary.main([*args, "--json-out", str(out)]) == 1
    capsys.readouterr()
    stats = json.loads(out.read_text())
    assert stats["baseline"] == {
        "exit": 0,
        "seconds": 10,
        "passed": 2,
        "failed": 0,
        "skipped": 1,
    }
    assert stats["findings"] == {"relationship-load": 2, "unused-columns": 1}
    assert stats["broken"] == 1
    assert stats["internal_warnings"] == 1
    assert stats["overhead_pct"] == 25.0
    assert stats["plugin_version"] == "0.8.0"

    bad = tmp_path / "bad"
    bad.mkdir()
    write_runs(bad, None, None, baseline_meta={"exit": 4, "seconds": 1})
    bad_out = bad / "summary.json"
    args = ["--dir", str(bad), "--project", "bad", "--ref", "o/b@1"]
    assert summary.main([*args, "--json-out", str(bad_out)]) == 2
    capsys.readouterr()

    assert summary.main(["--table", str(out), str(bad_out)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("| project | plugin |")
    assert lines[2] == (
        "| good | 0.8.0 | 2/0/1 | -1 / +1 | 1 | relationship-load 2, unused-columns 1 "
        "| 1/0 | +25.0 % | plugin problem |"
    )
    assert lines[3] == "| bad | ? | ?/?/? | ? | ? | ? | 0/0 | ? | harness problem |"


def test_the_passed_counts_show_the_delta(summary: ModuleType, tmp_path: Path) -> None:
    write_runs(
        tmp_path,
        junit(("test_a", "passed"), ("test_b", "passed")),
        junit(("test_a", "passed"), ("test_b", "failed")),
        counts=counts_file({}),
    )
    report, _ = summarize(summary, tmp_path, "demo", "v1")
    assert "| passed | 2 | 1 (-1) |" in report
    assert "| failed | 0 | 1 (+1) |" in report


def test_a_test_skipped_only_with_the_plugin_is_a_problem(
    summary: ModuleType, tmp_path: Path
) -> None:
    write_runs(
        tmp_path,
        junit(("test_a", "passed")),
        junit(("test_a", "skipped"), ("test_new", "passed")),
        counts=counts_file({}),
    )
    report, problems = summarize(summary, tmp_path, "demo", "v1")
    assert problems == ["1 test(s) pass only without the plugin"]
    assert "- `tests.test_app::test_a`: skipped: no" in report
    assert "1 test(s) ran only with the plugin, for example" in report


@pytest.mark.parametrize(
    "content",
    [
        "{broken",
        json.dumps({"format": 2, "tests": {}}),
        json.dumps({"format": 1, "complete": True, "tests": []}),
        json.dumps({"format": 1, "complete": True, "tests": {"t": "x"}}),
        json.dumps({"format": 1, "complete": True, "tests": {"t": {"findings": 3}}}),
    ],
)
def test_an_unusable_json_file_is_a_problem(
    summary: ModuleType, tmp_path: Path, content: str
) -> None:
    cases = junit(("test_a", "passed"))
    write_runs(tmp_path, cases, cases)
    (tmp_path / "queryguard.json").write_text(content, encoding="utf-8")
    report, problems = summarize(summary, tmp_path, "demo", "v1")
    assert "Unusable queryguard JSON file" in report
    (problem,) = problems
    assert problem.startswith("the queryguard JSON file is unusable")


def test_odd_entries_in_a_valid_file_are_skipped(
    summary: ModuleType, tmp_path: Path
) -> None:
    cases = junit(("test_a", "passed"))
    counts = counts_file({"t::a": ["junk", finding("relationship-load", 3)]})
    counts["tests"]["t::a"]["statements"] = "many"
    write_runs(tmp_path, cases, cases, counts=counts)
    report, problems = summarize(summary, tmp_path, "demo", "v1")
    assert problems == []
    assert "### Findings: 1" in report
    assert "0 statements" in report


PROBED = """
import warnings
from pytest_queryguard import QueryGuardInternalWarning, QueryGuardWarning

def test_a():
    warnings.warn(QueryGuardInternalWarning("listener failed"))
    warnings.warn(QueryGuardWarning("a finding"))
    warnings.warn(UserWarning("unrelated"))

def test_b():
    pass
"""

UNCONFIGURE = """
import warnings
from pytest_queryguard import QueryGuardInternalWarning

def pytest_unconfigure(config):
    if not hasattr(config, "workerinput"):
        warnings.warn(QueryGuardInternalWarning("cleanup failed"))
"""


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param([], id="plain"),
        # the project ignores UserWarning; the workflow's -W wins
        pytest.param(
            ["-W", "ignore::UserWarning"]
            + ["-W", "always::pytest_queryguard.QueryGuardInternalWarning"],
            id="ignored",
        ),
        pytest.param(["-p", "no:warnings"], id="no-warnings-plugin"),
        pytest.param(["-n", "2"], id="xdist"),
    ],
)
def test_the_probe_records_internal_warnings(
    pytester: pytest.Pytester,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
) -> None:
    if "-n" in extra:
        pytest.importorskip("xdist")
    out = tmp_path / "probe.jsonl"
    monkeypatch.setenv("QUERYGUARD_PROBE_OUT", str(out))
    monkeypatch.setenv("PYTHONPATH", str(SCRIPTS))  # as the workflow does
    pytester.makepyfile(PROBED)
    pytester.makeconftest(UNCONFIGURE)
    result = pytester.runpytest_subprocess(
        "-p", "realworld_probe", "-p", "no:queryguard", *extra
    )
    result.assert_outcomes(passed=2)
    events = [json.loads(line) for line in out.read_text().splitlines()]
    assert all(e["event"] == "internal-warning" for e in events), events
    # once each, also under pytest-xdist (no replay from the controller)
    assert sorted(e["message"] for e in events) == [
        "cleanup failed",
        "listener failed",
    ]
    (in_test,) = [e for e in events if e["message"] == "listener failed"]
    if "no:warnings" not in extra:
        assert in_test["nodeid"].endswith("::test_a")


def test_the_project_list_is_well_formed() -> None:
    data = json.loads((SCRIPTS / "realworld_projects.json").read_text())
    projects = data["projects"]
    assert 4 <= len(projects) <= 6
    names = [p["name"] for p in projects]
    assert len(set(names)) == len(names)
    keys = {"name", "repository", "ref", "python", "workdir", "install"}
    keys |= {"prepare", "pytest_args", "postgres", "env"}
    for project in projects:
        assert set(project) == keys, project["name"]
        assert project["ref"] and project["ref"] not in ("main", "master", "HEAD")
        assert project["repository"].count("/") == 1
        assert "," not in project["name"]
