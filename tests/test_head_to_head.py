"""The parts of scripts/head_to_head that need no competitor installed:
reading a test's report, scoring, and putting the results together."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent / "scripts" / "head_to_head"
sys.path.insert(0, str(HERE))

import aggregate
import common
from common import (
    COLUMN,
    ERROR,
    FLAGGED,
    PASSED,
    REL,
    REPEAT,
    TOOLS,
    UNKNOWN_KIND,
    UNUSED,
    Verdict,
)

QUERYSPY_REPORT = """\
def test_scenario(prepared):
>       module.run(ctx)
E   queryspy.api.NPlusOneError:
E
E   N+1 detected: 5 queries for Author.books (lazy load)
E     triggered from tests/corpus/scenarios/bug_01.py:30 in run()
E     SELECT book.id FROM book WHERE ? = book.author_id
E     fix: .options(selectinload(Author.books))
E
E   4 identical queries
E     SELECT tag.id FROM tag WHERE tag.name = ?
"""


def call(outcome: str, longrepr: str = "", when: str = "call") -> dict[str, str]:
    return {"scenario": "s", "when": when, "outcome": outcome, "longrepr": longrepr}


def test_queryspy_report_gives_kinds():
    verdict = common.classify(TOOLS["queryspy"], [call("failed", QUERYSPY_REPORT)])
    assert verdict.status == FLAGGED
    assert verdict.kinds == {REL, REPEAT}
    assert "identical queries" in verdict.detail or "SELECT" in verdict.detail


def test_queryspy_column_load():
    text = "E   N+1 detected: 3 column loads for Book.summary\n"
    assert common.parse_kinds(TOOLS["queryspy"], text) == {COLUMN}


def test_nplusone_messages():
    tool = TOOLS["nplusone"]
    text = "E   nplusone.core.exceptions.NPlusOneError: Potential n+1 query detected on `Author.books`"
    assert common.classify(tool, [call("failed", text)]) == Verdict(
        FLAGGED, frozenset({REL}), common.first_line(text)
    )
    eager = "NPlusOneError: Potential unnecessary eager load detected on `Author.books`"
    assert common.classify(tool, [call("failed", eager)]).kinds == {UNUSED}


def test_binary_tool_is_flagged_without_kind():
    text = "E   pytest_querycount.errors.DuplicateQueryError: test ran 5x SELECT ..."
    verdict = common.classify(TOOLS["querycount"], [call("failed", text)])
    assert verdict.status == FLAGGED
    assert verdict.kinds == {UNKNOWN_KIND}


def test_sniffer_failure_message():
    text = (
        "Detected 1 suspected N+1 query pattern(s) (threshold=3):\n\nN+1 suspected: ..."
    )
    assert common.classify(TOOLS["sniffer"], [call("failed", text)]).status == FLAGGED


def test_another_failure_is_an_error_not_a_pass():
    text = "E   AttributeError: 'Query' object has no attribute '_offset'"
    verdict = common.classify(TOOLS["nplusone"], [call("failed", text)])
    assert verdict.status == ERROR
    assert verdict.detail == "AttributeError: 'Query' object has no attribute '_offset'"


def test_setup_error_is_an_error():
    phases = [call("failed", "E   RuntimeError: boom", when="setup")]
    assert common.classify(TOOLS["queryspy"], phases).status == ERROR


def test_scenario_that_never_ran_is_an_error():
    assert common.classify(TOOLS["queryspy"], []).status == ERROR


def test_pass():
    assert common.classify(TOOLS["querycount"], [call("passed")]) == Verdict(PASSED)


def test_queryguard_uses_its_findings():
    tool = TOOLS["queryguard"]
    assert common.classify(tool, [call("passed")], frozenset({REL})).status == FLAGGED
    assert common.classify(tool, [call("passed")], frozenset()).status == PASSED
    failed = [call("failed", "E   ValueError: x")]
    assert common.classify(tool, failed, frozenset()).status == ERROR


SCENARIOS = {
    "bug_rel": {"verdict": "BUG", "kinds": [REL]},
    "bug_repeat": {"verdict": "BUG", "kinds": [REPEAT]},
    "bug_unused": {"verdict": "BUG", "kinds": [UNUSED]},
    "clean_a": {"verdict": "CLEAN", "kinds": []},
    "clean_b": {"verdict": "CLEAN", "kinds": []},
}


def test_score_of_a_kind_reporting_tool():
    tool = TOOLS["queryspy"]  # REL, COLUMN, REPEAT
    verdicts = {
        "bug_rel": Verdict(FLAGGED, frozenset({REL})),
        "bug_repeat": Verdict(FLAGGED, frozenset({REL})),  # wrong kind
        "bug_unused": Verdict(PASSED),
        "clean_a": Verdict(FLAGGED, frozenset({REPEAT})),
        "clean_b": Verdict(ERROR, detail="boom"),
    }
    s = common.score(tool, SCENARIOS, verdicts)
    assert (s.bug_flagged, s.bug_missed, s.bug_error) == (2, 1, 0)
    assert (s.clean_passed, s.clean_flagged, s.clean_error) == (0, 1, 1)
    assert s.precision == pytest.approx(2 / 3)
    assert s.recall == pytest.approx(2 / 3)
    # unused-eager-load is not a kind queryspy claims: out of scope
    assert (s.in_scope_bugs, s.in_scope_flagged) == (2, 2)
    assert s.kinds[UNUSED] is None
    rel, repeat = s.kinds[REL], s.kinds[REPEAT]
    assert rel is not None and (rel.tp, rel.fp, rel.fn) == (1, 1, 0)
    assert repeat is not None and (repeat.tp, repeat.fp, repeat.fn) == (0, 1, 1)
    assert s.errors == [("clean_b", "boom")]
    assert s.false_positives == ["clean_a"]
    assert s.clean_pass_rate == 0


def test_score_of_a_binary_tool():
    tool = TOOLS["querycount"]
    unknown = frozenset({UNKNOWN_KIND})
    verdicts = {
        "bug_rel": Verdict(FLAGGED, unknown),
        "bug_repeat": Verdict(ERROR, detail="crash"),
        "bug_unused": Verdict(FLAGGED, unknown),
        "clean_a": Verdict(PASSED),
        "clean_b": Verdict(PASSED),
    }
    s = common.score(tool, SCENARIOS, verdicts)
    assert (s.bug_flagged, s.bug_missed, s.bug_error) == (2, 0, 1)
    assert s.precision == 1
    assert s.in_scope_recall == pytest.approx(1 / 2)
    rel, repeat = s.kinds[REL], s.kinds[REPEAT]
    assert rel is not None and (rel.flagged, rel.total) == (1, 1)
    assert repeat is not None and (repeat.flagged, repeat.total) == (0, 1)
    assert s.misses == ["bug_repeat"]
    assert common.kind_cell(tool, rel) == "- / 100%"
    assert common.kind_cell(tool, s.kinds[UNUSED]) == "n/a"


def test_missing_verdict_is_an_error():
    s = common.score(TOOLS["queryspy"], SCENARIOS, {})
    assert s.bug_error == 3 and s.clean_error == 2


def write_tool(
    directory: Path, key: str, results: dict[str, dict[str, object]]
) -> None:
    sub = directory / f"corpus-{key}"
    sub.mkdir()
    payload = {
        "tool": key,
        "versions": {"python": "3.13.0", "sqlalchemy": "2.1.1", "pytest": "9.1.1"},
        "scenarios": SCENARIOS,
        "results": results,
    }
    (sub / f"{key}.json").write_text(json.dumps(payload))


def test_aggregate(tmp_path):
    ok = {"status": PASSED, "kinds": []}
    write_tool(
        tmp_path,
        "queryguard",
        {
            "bug_rel": {"status": FLAGGED, "kinds": [REL]},
            "bug_repeat": {"status": FLAGGED, "kinds": [REPEAT]},
            "bug_unused": ok,
            "clean_a": ok,
            "clean_b": ok,
        },
    )
    write_tool(
        tmp_path,
        "nplusone",
        {name: {"status": ERROR, "kinds": [], "detail": "x | y"} for name in SCENARIOS},
    )
    write_tool(tmp_path, "none", {name: ok for name in SCENARIOS})
    overhead = {
        "rounds": 3,
        "tests": 300,
        "micro": {
            "off": {"core": 10.0, "orm": 20.0, "lazy": 30.0},
            "queryguard": {"core": 11.0, "orm": 22.0, "lazy": 33.5},
        },
        "suite": {"off": 2.0, "queryguard": 2.1},
        "failed": ["sniffer: install failed"],
    }
    (tmp_path / "overhead").mkdir()
    (tmp_path / "overhead" / "overhead.json").write_text(json.dumps(overhead))
    corpus, loaded = aggregate.load(tmp_path)
    assert set(corpus) == {"queryguard", "nplusone", "none"}
    markdown, payload = aggregate.build(corpus, loaded, "https://example/run")
    assert (
        "| pytest-queryguard, defaults | 2 | 1 | 0 | 2 | 0 | 0 | 100% | 67% | 100% |"
        in (markdown)
    )
    # every scenario an error: no precision, recall 0, not a pass
    assert (
        "| nplusone (SQLAlchemy) | 0 | 0 | 3 | 0 | 0 | 2 | n/a | 0% | 0% |" in markdown
    )
    assert (
        "Not run (job failed or tool not installable): pytest-queryguard, all opt-in"
        in (markdown)
    )
    assert "every scenario passes" in markdown
    assert "| 300-test suite, added wall time | +5.0% |" in markdown
    assert "| lazy load, added per statement | +3.5 us |" in markdown
    assert "Failed: sniffer: install failed" in markdown
    assert "x / y" in markdown  # a | in a detail cannot break the list
    assert payload["tools"]["nplusone"]["bug_error"] == 3  # type: ignore[index]


def test_aggregate_without_results(tmp_path):
    markdown, _ = aggregate.build({}, None)
    assert "No corpus results." in markdown and "No overhead results." in markdown


def test_every_compared_tool_is_defined():
    assert set(common.COMPARED) <= set(TOOLS)
    for key in common.COMPARED:
        tool = TOOLS[key]
        assert tool.supported <= set(common.KINDS)
        assert tool.config is not None or tool.markers, key
