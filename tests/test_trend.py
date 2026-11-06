"""Query counts over several --queryguard-json files (#248)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from pytest_queryguard import trend


def _finding(label: str, count: int = 5, **extra: Any) -> dict[str, Any]:
    return {
        "kind": "relationship-load",
        "label": label,
        "count": count,
        "file": "app/orders.py",
        "function": "list_orders",
        "line": 10,
        "baselined": False,
        **extra,
    }


def _doc(tests: dict[str, Any], complete: bool = True) -> dict[str, Any]:
    full = {
        nodeid: {
            "statements": entry.get("statements", 1),
            "by_verb": {},
            "db_time": None,
            "truncated": False,
            "findings": entry.get("findings", []),
        }
        for nodeid, entry in tests.items()
    }
    return {"format": 1, "tool": {"name": "x"}, "complete": complete, "tests": full}


RUNS = [
    _doc(
        {
            "t.py::grows": {"statements": 3},
            "t.py::shrinks": {"statements": 50, "findings": [_finding("A.b")]},
            "t.py::same": {"statements": 4},
            "t.py::renamed_old": {"statements": 9},
        }
    ),
    _doc(
        {
            "t.py::grows": {"statements": 5},
            "t.py::shrinks": {"statements": 50, "findings": [_finding("A.b")]},
            "t.py::same": {"statements": 4},
            "t.py::renamed_new": {"statements": 9},
        }
    ),
    _doc(
        {
            "t.py::grows": {"statements": 53, "findings": [_finding("O.items")]},
            "t.py::shrinks": {"statements": 2},
            "t.py::same": {"statements": 4},
            "t.py::renamed_new": {"statements": 9},
        }
    ),
]
LABELS = ["r1", "r2", "r3"]


def test_sparkline_scales_between_the_lowest_and_highest_count() -> None:
    assert trend.sparkline((0, 7, 14)) == "▁▅█"
    assert trend.sparkline((1, 2, 3, 4, 5, 6, 7, 8)) == "▁▂▃▄▅▆▇█"
    assert trend.sparkline((4, 4, 4)) == "▁▁▁"
    assert trend.sparkline((3, None, 9)) == "▁·█"
    assert trend.sparkline((None, None)) == "··"
    assert trend.sparkline((0, None, 14), ascii=True) == "_.@"
    assert trend.sparkline((1, 2, 3, 4, 5, 6, 7, 8), ascii=True).isascii()


def test_totals_per_run_and_the_tests_that_changed_most_first() -> None:
    result = trend.trend(RUNS, LABELS)
    assert [(r.label, r.tests, r.statements) for r in result.runs] == [
        ("r1", 4, 66),
        ("r2", 4, 68),
        ("r3", 4, 68),
    ]
    assert [(t.nodeid, t.first, t.last, t.delta) for t in result.changed] == [
        ("t.py::grows", 3, 53, 50),
        ("t.py::shrinks", 50, 2, -48),
    ]
    assert result.changed[0].counts == (3, 5, 53)


def test_a_renamed_test_is_one_new_and_one_gone() -> None:
    result = trend.trend(RUNS, LABELS)
    assert (result.added_tests, result.gone_tests) == (1, 1)
    nodeids = [t.nodeid for t in result.changed]
    assert "t.py::renamed_old" not in nodeids
    assert "t.py::renamed_new" not in nodeids  # 9 -> 9


def test_a_test_new_in_the_window_is_compared_from_its_first_run() -> None:
    runs = [_doc({}), _doc({"t.py::a": {"statements": 2}}), _doc({"t.py::a": {}})]
    (only,) = trend.trend(runs, LABELS).changed
    assert (only.counts, only.first, only.last) == ((None, 2, 1), 2, 1)


def test_findings_new_and_gone_over_the_window() -> None:
    result = trend.trend(RUNS, LABELS)
    assert [(f.row.label, f.since) for f in result.new] == [("O.items", 2)]
    assert [row.label for row in result.gone] == ["A.b"]


def test_a_new_finding_is_dated_from_the_run_it_stayed_since() -> None:
    a = {"t.py::a": {"findings": [_finding("X.y")]}}
    runs = [_doc({"t.py::a": {}}), _doc(a), _doc({"t.py::a": {}}), _doc(a), _doc(a)]
    (finding,) = trend.trend(runs, ["1", "2", "3", "4", "5"]).new
    assert finding.since == 3


def test_an_incomplete_run_is_marked_and_does_not_lose_tests() -> None:
    runs = [
        _doc({"t.py::a": {"statements": 1}, "t.py::b": {"statements": 1}}),
        _doc({"t.py::a": {"statements": 4}}, complete=False),
    ]
    result = trend.trend(runs, ["old", "new"])
    assert not result.complete and not result.runs[1].complete
    # b did not run in the stopped run: counted as not in the latest run,
    # with no count to compare
    assert result.gone_tests == 1
    assert [t.nodeid for t in result.changed] == ["t.py::a"]
    text = trend.markdown(result)
    assert "[!WARNING]" in text and "`new`" in text
    assert "| `new` | 1 | 4 | incomplete |" in text
    assert "(incomplete)" in trend.text(result)


def test_a_test_missing_from_a_stopped_last_run_keeps_its_trend() -> None:
    runs = [
        _doc({"t.py::a": {"statements": 1}, "t.py::b": {"statements": 1}}),
        _doc({"t.py::a": {"statements": 1}, "t.py::b": {"statements": 6}}),
        _doc({"t.py::a": {"statements": 1}}, complete=False),
    ]
    (changed,) = trend.trend(runs, LABELS).changed
    assert (changed.nodeid, changed.counts, changed.delta) == (
        "t.py::b",
        (1, 6, None),
        5,
    )


def test_a_pattern_limits_every_table_to_matching_tests() -> None:
    result = trend.trend(RUNS, LABELS, test="grows")
    assert [r.statements for r in result.runs] == [3, 5, 53]
    assert [t.nodeid for t in result.changed] == ["t.py::grows"]
    assert result.gone == []
    glob = trend.trend(RUNS, LABELS, test="t.py::renamed_*")
    assert [r.tests for r in glob.runs] == [1, 1, 1]


def test_markdown_report() -> None:
    text = trend.markdown(trend.trend(RUNS, LABELS), key="postgres")
    assert text.startswith(
        "### pytest-queryguard: query counts over the last 3 runs (postgres)\n"
    )
    assert (
        "3 runs, statements 66 -> 68 (+2) `▁██`. 2 tests changed their count, "
        "1 new, 1 not in the latest run; 1 new findings, 1 gone."
    ) in text
    assert "| `r1` | 4 | 66 |  |" in text
    assert "| `t.py::grows` | `▁▁█` | 3 | 53 | +50 |" in text
    assert "| `t.py::shrinks` | `██▁` | 50 | 2 | -48 |" in text
    assert (
        "| relationship-load | `O.items` | `app/orders.py in list_orders` | `r3` |"
        in (text)
    )
    assert "#### Findings gone in this window" in text
    assert "WARNING" not in text


def test_markdown_folded_and_cut() -> None:
    runs = [
        _doc({f"t.py::t{n:02}": {"statements": 1} for n in range(30)}),
        _doc({f"t.py::t{n:02}": {"statements": 2 + n} for n in range(30)}),
    ]
    text = trend.markdown(trend.trend(runs, ["a", "b"]), top=3, details=True)
    assert text.startswith(
        "<details><summary>pytest-queryguard: query counts over the last 2 runs"
        "</summary>\n"
    )
    assert text.rstrip().endswith("</details>")
    assert text.count("| `t.py::t") == 3
    assert "...and 27 more." in text
    assert text.index("t.py::t29") < text.index("t.py::t28")


def test_ascii_reports() -> None:
    result = trend.trend(RUNS, LABELS)
    assert trend.markdown(result, ascii=True).isascii()
    assert trend.text(result, ascii=True).isascii()
    plain = trend.text(result)
    assert "▁▁█       3 -> 53         +50  t.py::grows" in plain
    assert "since r3" in plain


def test_json_report() -> None:
    doc = json.loads(trend.as_json(trend.trend(RUNS, LABELS), top=1))
    assert [r["statements"] for r in doc["runs"]] == [66, 68, 68]
    assert doc["complete"] is True
    assert doc["changed"] == [
        {
            "nodeid": "t.py::grows",
            "counts": [3, 5, 53],
            "first": 3,
            "last": 53,
            "change": 50,
        }
    ]
    assert doc["changed_total"] == 2
    assert doc["new_findings"][0]["since"] == "r3"
    assert doc["gone_findings"][0]["label"] == "A.b"


def test_one_run_is_a_trend_without_changes() -> None:
    result = trend.trend(RUNS[:1], ["only"])
    assert result.changed == [] and result.new == [] and result.gone == []
    text = trend.markdown(result)
    assert "over the last 1 run\n" in text
    assert "1 run, statements 66 -> 66 (0)" in text


def test_labels_default_to_the_file_names(tmp_path: Path) -> None:
    paths = [tmp_path / "a" / "x.json", tmp_path / "b" / "x.json"]
    assert trend.default_labels(paths) == ["1", "2"]
    assert trend.default_labels([tmp_path / "mon.json", tmp_path / "tue.json"]) == [
        "mon",
        "tue",
    ]


def _write(tmp_path: Path) -> list[str]:
    paths = []
    for label, doc in zip(LABELS, RUNS, strict=True):
        path = tmp_path / f"{label}.json"
        path.write_text(json.dumps(doc))
        paths.append(str(path))
    return paths


def test_the_cli_writes_each_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _write(tmp_path)
    out = tmp_path / "trend.md"
    assert trend.main([*paths, "--output", str(out)]) == 0
    printed = capsys.readouterr().out
    written = out.read_text(encoding="utf-8")
    assert written.startswith("### pytest-queryguard: query counts over")
    assert "| `r1` |" in written
    # the console gets ASCII sparklines when it cannot print the blocks
    assert printed == written or (printed.isascii() and "`__@`" in printed)
    assert trend.main([*paths, "--format", "json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["runs"]) == 3
    assert trend.main([*paths, "--format", "text", "--ascii", "--top", "1"]) == 0
    printed = capsys.readouterr().out
    assert printed.isascii() and "...and 1 more." in printed
    assert trend.main([*paths, "--label", "a", "--label", "b", "--label", "c"]) == 0
    assert "| `c` |" in capsys.readouterr().out


def test_the_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = _write(tmp_path)
    (tmp_path / "x.json").write_text('{"version": 2}')
    assert trend.main([paths[0], str(tmp_path / "x.json")]) == 2
    assert "not a pytest-queryguard JSON file" in capsys.readouterr().err
    assert trend.main([paths[0], str(tmp_path / "missing.json")]) == 2
    for wrong in (["--label", "a"], ["--top", "0"], ["--format", "html"]):
        with pytest.raises(SystemExit) as exc:
            trend.main([*paths, *wrong])
        assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        trend.main([])
    assert exc.value.code == 2


def test_the_module_runs_in_a_cp1252_console(
    tmp_path: Path, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows consoles without UTF-8: ASCII sparklines instead of an error
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    result = pytester.run(
        sys.executable, "-m", "pytest_queryguard.trend", *_write(tmp_path)
    )
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*| `t.py::grows` | `__@` | 3 | 53 | +50 |*"])
