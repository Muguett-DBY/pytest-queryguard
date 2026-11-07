"""Comparing two --queryguard-json files (#231)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from pytest_queryguard import compare

MODELS = (Path(__file__).parent / "models.py").read_text()


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


BASE = _doc(
    {
        "t.py::same": {"statements": 3},
        "t.py::grows": {"statements": 2},
        "t.py::shrinks": {"statements": 50, "findings": [_finding("Order.customer")]},
        "t.py::gone": {"statements": 9},
    }
)
HEAD = _doc(
    {
        "t.py::same": {"statements": 3},
        "t.py::grows": {
            "statements": 40,
            "findings": [_finding("Order.items", 38, line=99)],
        },
        "t.py::shrinks": {"statements": 2},
        "t.py::new_test": {"statements": 7, "findings": [_finding("X.y")]},
    }
)


def test_counts_and_findings_are_compared_for_the_tests_of_both_runs() -> None:
    result = compare.compare(BASE, HEAD)
    assert [(c.nodeid, c.delta) for c in result.more] == [("t.py::grows", 38)]
    assert [(c.nodeid, c.delta) for c in result.fewer] == [("t.py::shrinks", -48)]
    # X.y comes from a test the change added
    assert [r.label for r in result.new] == ["Order.items", "X.y"]
    assert [r.label for r in result.fixed] == ["Order.customer"]
    assert (result.added_tests, result.removed_tests, result.compared_tests) == (
        1,
        1,
        3,
    )
    assert (result.total_before, result.total_after) == (55, 45)


def test_a_finding_on_another_line_is_the_same_finding() -> None:
    base = _doc({"t.py::a": {"findings": [_finding("A.b", line=1)]}})
    head = _doc({"t.py::a": {"findings": [_finding("A.b", 9, line=2)]}})
    result = compare.compare(base, head)
    assert result.new == [] and result.fixed == []


def test_baselined_findings_are_neither_new_nor_fixed() -> None:
    base = _doc({"t.py::a": {"findings": [_finding("A.b", baselined=True)]}})
    head = _doc({"t.py::a": {"findings": [_finding("C.d", baselined=True)]}})
    result = compare.compare(base, head)
    assert result.new == [] and result.fixed == []


def test_one_finding_in_several_tests_is_one_row() -> None:
    head = _doc({f"t.py::t{n}": {"findings": [_finding("A.b", 2)]} for n in range(3)})
    base = _doc({f"t.py::t{n}": {} for n in range(3)})
    (row,) = compare.compare(base, head).new
    assert (row.count, row.tests) == (6, ("t.py::t0", "t.py::t1", "t.py::t2"))
    assert "`t.py::t0` and 2 more" in compare.markdown(compare.compare(base, head))


def test_the_report_leads_with_new_findings_and_more_statements() -> None:
    text = compare.markdown(compare.compare(BASE, HEAD))
    assert text.startswith(compare.MARKER + "\n")
    assert (
        "3 tests compared: 55 -> 45 statements (-10). **1** run more, 1 fewer; "
        "**2** new findings, 1 fixed."
    ) in text
    assert "Not compared: 1 new tests, 1 tests not in this run." in text
    assert text.index("#### New findings") < text.index("#### Tests that run more")
    assert (
        "| relationship-load | `Order.items` | `app/orders.py in list_orders` | 38 |"
        in text
    )
    assert "| `t.py::grows` | 2 | 40 | +38 |" in text
    assert "<details><summary>Fixed findings</summary>" in text
    assert "| `t.py::shrinks` | 50 | 2 | -48 |" in text
    assert "WARNING" not in text


def test_no_change_says_so() -> None:
    text = compare.markdown(compare.compare(BASE, BASE))
    assert "No test changed its number of statements." in text
    assert "####" not in text


def test_an_incomplete_run_is_flagged() -> None:
    text = compare.markdown(compare.compare(BASE, _doc({}, complete=False)))
    assert "[!WARNING]" in text


def test_long_tables_are_cut() -> None:
    base = _doc({f"t.py::t{n:02}": {"statements": 1} for n in range(30)})
    head = _doc({f"t.py::t{n:02}": {"statements": 2 + n} for n in range(30)})
    text = compare.markdown(compare.compare(base, head), rows=5)
    assert text.count("| `t.py::t") == 5
    assert "...and 25 more." in text
    # the biggest increase first
    assert text.index("t.py::t29") < text.index("t.py::t28")


def test_markdown_in_labels_cannot_break_the_table() -> None:
    head = _doc(
        {
            "t.py::a": {
                "findings": [_finding("SELECT `x` | 1", kind="repeated-statement")]
            }
        }
    )
    text = compare.markdown(compare.compare(_doc({"t.py::a": {}}), head))
    assert "`SELECT 'x' \\| 1`" in text


def test_the_cli_writes_the_report_and_rejects_other_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base, head, out = tmp_path / "b.json", tmp_path / "h.json", tmp_path / "r.md"
    base.write_text(json.dumps(BASE))
    head.write_text(json.dumps(HEAD))
    assert compare.main([str(base), str(head), "--output", str(out)]) == 0
    assert capsys.readouterr().out == out.read_text(encoding="utf-8") != ""
    (tmp_path / "x.json").write_text('{"version": 2}')
    assert compare.main([str(base), str(tmp_path / "x.json")]) == 2
    assert "not a pytest-queryguard JSON file" in capsys.readouterr().err
    assert compare.main([str(base), str(tmp_path / "missing.json")]) == 2


SUITE_BASE = """
from qgmodels import Order, make_sync_session

def test_list():
    session = make_sync_session()
    session.query(Order).all()
"""

SUITE_HEAD = """
from qgmodels import Order, make_sync_session

def test_list():
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name
"""


def test_two_real_runs_are_compared(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(qgmodels=MODELS, test_app=SUITE_BASE)
    pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "--queryguard-json=base.json"
    )
    pytester.makepyfile(test_app=SUITE_HEAD)
    pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "--queryguard-json=head.json"
    )
    result = pytester.run(
        sys.executable, "-m", "pytest_queryguard.compare", "base.json", "head.json"
    )
    assert result.ret == 0
    result.stdout.fnmatch_lines(
        [
            "*1 tests compared*",
            "#### New findings",
            "*| relationship-load | `Order.customer` | `test_app.py in test_list` |*",
            "#### Tests that run more statements",
            "*| `test_app.py::test_list` |*",
        ]
    )


def test_a_key_names_the_comparison_and_its_marker() -> None:
    text = compare.markdown(compare.compare(BASE, HEAD), key="postgres --> x")
    first, title = text.splitlines()[:2]
    assert first == "<!-- pytest-queryguard compare postgres > x -->"
    assert title.endswith("(postgres --> x)")
    assert compare.marker() == compare.MARKER


def test_a_finding_of_a_new_test_is_new() -> None:
    base = _doc({"t.py::old": {}})
    head = _doc({"t.py::old": {}, "t.py::added": {"findings": [_finding("A.b")]}})
    assert [r.tests for r in compare.compare(base, head).new] == [("t.py::added",)]


def test_a_finding_moved_into_the_baseline_is_not_fixed() -> None:
    base = _doc({"t.py::a": {"findings": [_finding("A.b")]}})
    head = _doc({"t.py::a": {"findings": [_finding("A.b", baselined=True)]}})
    result = compare.compare(base, head)
    assert result.fixed == [] and result.new == []
    # and taken out of the baseline again it is not new either
    result = compare.compare(head, base)
    assert result.fixed == [] and result.new == []


def test_the_report_is_ascii_so_any_console_prints_it() -> None:
    # a Windows console with cp1252 cannot print arrows or ellipses
    base = _doc({f"t.py::t{n:02}": {"statements": 1} for n in range(30)})
    head = _doc({f"t.py::t{n:02}": {"statements": 2} for n in range(30)})
    assert compare.markdown(compare.compare(base, head), rows=2).isascii()


@pytest.mark.parametrize(
    "entry",
    [
        {},  # no statement count
        [],
        {"statements": "12"},
        {"statements": True},
        {"statements": 1, "findings": [1]},
        {"statements": 1, "findings": {}},
        {"statements": 1, "findings": [{"kind": "relationship-load"}]},
        {"statements": 1, "findings": [{"kind": "k", "label": ["a"]}]},
        {"statements": 1, "findings": [{"kind": "k", "label": "a", "count": "2"}]},
        {"statements": 1, "findings": [{"kind": "k", "label": "a", "count": None}]},
        {"statements": 1, "findings": [{"kind": "k", "label": "a", "file": [1]}]},
    ],
)
def test_a_malformed_test_entry_is_an_unreadable_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], entry: Any
) -> None:
    # a traceback before; the trend in the action could not skip the file
    base, bad = tmp_path / "b.json", tmp_path / "bad.json"
    base.write_text(json.dumps(BASE))
    bad.write_text(json.dumps({"format": 1, "tests": {"t.py::a": entry}}))
    assert compare.main([str(base), str(bad)]) == 2
    err = capsys.readouterr().err
    assert "not a pytest-queryguard JSON file" in err
    assert "t.py::a" in err


def test_a_file_with_a_byte_order_mark_is_read(tmp_path: Path) -> None:
    # Windows PowerShell 5 writes UTF-8 with a BOM
    path = tmp_path / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(BASE).encode())
    assert compare.load(path)["tests"] == BASE["tests"]


def test_an_output_file_that_cannot_be_written_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_path / "b.json"
    base.write_text(json.dumps(BASE))
    out = tmp_path / "missing" / "r.md"
    assert compare.main([str(base), str(base), "--output", str(out)]) == 2
    assert f"cannot write {out}" in capsys.readouterr().err


def test_deeply_nested_json_is_an_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "deep.json"
    path.write_text("[" * 200_000)
    with pytest.raises(compare.CompareError, match="cannot read"):
        compare.load(path)


def test_a_whole_number_written_as_a_float_is_still_read(tmp_path: Path) -> None:
    # int() read 3.0 before the entries were checked; keep reading it
    path = tmp_path / "float.json"
    doc = {"format": 1, "tests": {"t.py::a": {"statements": 3.0, "findings": []}}}
    path.write_text(json.dumps(doc))
    assert compare.compare(compare.load(path), compare.load(path)).total_after == 3
