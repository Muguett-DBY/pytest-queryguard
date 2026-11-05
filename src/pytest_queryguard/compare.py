"""Compare two ``--queryguard-json`` files: what a change did to the queries.

    python -m pytest_queryguard.compare base.json head.json [--output report.md]

Prints a Markdown report: the tests that run more statements (and fewer), the
findings that are new and the ones that are gone. A finding is matched by
kind, label, file and function, the identity the baseline uses, so a moved
line is not a new finding. Baselined findings are left out of both lists.
Tests that exist in only one of the files are counted, not compared.

The report is for people (a pull-request comment, the job summary); the exit
status is 0 unless a file cannot be read (2).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .baseline import Identity

MARKER = "<!-- pytest-queryguard compare -->"


def marker(key: str = "") -> str:
    """The first line of a report; a later run finds its comment by it."""
    key = " ".join(key.replace("--", "").split())
    return f"<!-- pytest-queryguard compare {key} -->" if key else MARKER


_ROWS = 25


class CompareError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Change:
    nodeid: str
    before: int
    after: int

    @property
    def delta(self) -> int:
        return self.after - self.before


@dataclass(frozen=True, slots=True)
class FindingRow:
    kind: str
    label: str
    file: str | None
    function: str | None
    count: int
    tests: tuple[str, ...]


@dataclass(slots=True)
class Comparison:
    more: list[Change] = field(default_factory=list)
    fewer: list[Change] = field(default_factory=list)
    new: list[FindingRow] = field(default_factory=list)
    fixed: list[FindingRow] = field(default_factory=list)
    added_tests: int = 0
    removed_tests: int = 0
    compared_tests: int = 0
    total_before: int = 0
    total_after: int = 0
    complete: bool = True


def load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CompareError(f"cannot read {path}: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != 1:
        raise CompareError(f"{path} is not a pytest-queryguard JSON file (format 1)")
    if not isinstance(data.get("tests"), dict):
        raise CompareError(f"{path} has no tests")
    return data


def _findings(
    tests: dict[str, Any], *, baselined: bool
) -> dict[Identity, tuple[int, set[str]]]:
    """identity -> (total count, node ids); baselined ones only when asked."""
    found: dict[Identity, tuple[int, set[str]]] = {}
    for nodeid, entry in tests.items():
        for f in entry.get("findings", []):
            if f.get("baselined") and not baselined:
                continue
            key: Identity = (f["kind"], f["label"], f.get("file"), f.get("function"))
            count, ids = found.get(key, (0, set()))
            ids.add(nodeid)
            found[key] = (count + int(f.get("count", 0)), ids)
    return found


def _rows(
    found: dict[Identity, tuple[int, set[str]]], keys: set[Identity]
) -> list[FindingRow]:
    rows = [
        FindingRow(*key, found[key][0], tuple(sorted(found[key][1]))) for key in keys
    ]
    rows.sort(key=lambda r: (-r.count, r.kind, r.label, r.file or "", r.function or ""))
    return rows


def compare(base: dict[str, Any], head: dict[str, Any]) -> Comparison:
    before, after = base["tests"], head["tests"]
    result = Comparison(
        complete=bool(base.get("complete")) and bool(head.get("complete"))
    )
    common = sorted(set(before) & set(after))
    result.compared_tests = len(common)
    result.added_tests = len(set(after) - set(before))
    result.removed_tests = len(set(before) - set(after))
    for nodeid in common:
        old, new = int(before[nodeid]["statements"]), int(after[nodeid]["statements"])
        result.total_before += old
        result.total_after += new
        if new > old:
            result.more.append(Change(nodeid, old, new))
        elif new < old:
            result.fewer.append(Change(nodeid, old, new))
    result.more.sort(key=lambda c: (-c.delta, c.nodeid))
    result.fewer.sort(key=lambda c: (c.delta, c.nodeid))
    # New: a finding of the change (new tests included) that the base run did
    # not have at all, baselined or not. Fixed: a finding of the base run, in a
    # test that still ran, that the change no longer has at all; a test that
    # was removed or deselected fixed nothing, and a finding moved into the
    # baseline is suppressed, not fixed.
    new_found = _findings(after, baselined=False)
    old_any = _findings(before, baselined=True)
    old_found = _findings({k: before[k] for k in common}, baselined=False)
    new_any = _findings({k: after[k] for k in common}, baselined=True)
    result.new = _rows(new_found, set(new_found) - set(old_any))
    result.fixed = _rows(old_found, set(old_found) - set(new_any))
    return result


def _code(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > 120:
        text = text[:120] + "..."
    return "`" + text.replace("`", "'").replace("|", "\\|") + "`"


def _where(row: FindingRow) -> str:
    if row.file is None:
        return "unknown location"
    return f"{row.file}" + (f" in {row.function}" if row.function else "")


def _tests(row: FindingRow) -> str:
    first = _code(row.tests[0])
    more = len(row.tests) - 1
    return first + (f" and {more} more" if more else "")


def markdown(result: Comparison, *, rows: int = _ROWS, key: str = "") -> str:
    """The report, with a marker line so a later run can update the comment."""
    title = "### pytest-queryguard: queries compared with the base branch"
    if key:
        title += f" ({key})"
    lines = [marker(key), title, ""]
    delta = result.total_after - result.total_before
    sign = "+" if delta > 0 else ""
    lines.append(
        f"{result.compared_tests} tests compared: {result.total_before} -> "
        f"{result.total_after} statements ({sign}{delta}). "
        f"**{len(result.more)}** run more, {len(result.fewer)} fewer; "
        f"**{len(result.new)}** new findings, {len(result.fixed)} fixed."
    )
    extra = []
    if result.added_tests:
        extra.append(f"{result.added_tests} new tests")
    if result.removed_tests:
        extra.append(f"{result.removed_tests} tests not in this run")
    if extra:
        lines.append(f"Not compared: {', '.join(extra)}.")
    if not result.complete:
        lines.append(
            "> [!WARNING]\n> One of the runs stopped early or measurement failed; "
            "the comparison may be partial."
        )
    if result.new:
        lines += [
            "",
            "#### New findings",
            "",
            "| Kind | Label | Where | Times | Tests |",
        ]
        lines.append("|---|---|---|---:|---|")
        for row in result.new[:rows]:
            lines.append(
                f"| {row.kind} | {_code(row.label)} | {_code(_where(row))} | "
                f"{row.count} | {_tests(row)} |"
            )
        lines += _more(len(result.new), rows)
    if result.more:
        lines += ["", "#### Tests that run more statements", ""]
        lines += ["| Test | Before | After | Change |", "|---|---:|---:|---:|"]
        for change in result.more[:rows]:
            lines.append(
                f"| {_code(change.nodeid)} | {change.before} | {change.after} | "
                f"+{change.delta} |"
            )
        lines += _more(len(result.more), rows)
    if result.fixed:
        lines += ["", "<details><summary>Fixed findings</summary>", ""]
        lines += ["| Kind | Label | Where |", "|---|---|---|"]
        for row in result.fixed[:rows]:
            lines.append(f"| {row.kind} | {_code(row.label)} | {_code(_where(row))} |")
        lines += _more(len(result.fixed), rows)
        lines += ["", "</details>"]
    if result.fewer:
        lines += ["", "<details><summary>Tests that run fewer statements</summary>", ""]
        lines += ["| Test | Before | After | Change |", "|---|---:|---:|---:|"]
        for change in result.fewer[:rows]:
            lines.append(
                f"| {_code(change.nodeid)} | {change.before} | {change.after} | "
                f"{change.delta} |"
            )
        lines += _more(len(result.fewer), rows)
        lines += ["", "</details>"]
    if not (result.new or result.more or result.fixed or result.fewer):
        lines += ["", "No test changed its number of statements."]
    return "\n".join(lines) + "\n"


def _more(total: int, shown: int) -> list[str]:
    return ["", f"...and {total - shown} more."] if total > shown else []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pytest_queryguard.compare",
        description="Compare two --queryguard-json files and print a Markdown report.",
    )
    parser.add_argument("base", type=Path, help="the file of the base branch")
    parser.add_argument("head", type=Path, help="the file of the change")
    parser.add_argument("--output", type=Path, help="write the report here too")
    parser.add_argument(
        "--rows", type=int, default=_ROWS, help="rows per table (default 25)"
    )
    parser.add_argument(
        "--key",
        default="",
        help="name of this comparison (one per CI job), in the title and the marker",
    )
    args = parser.parse_args(argv)
    try:
        result = compare(load(args.base), load(args.head))
    except CompareError as exc:
        print(f"pytest-queryguard compare: {exc}", file=sys.stderr)
        return 2
    report = markdown(result, rows=max(args.rows, 1), key=args.key)
    # a label may hold any character; a console that cannot print it gets an
    # escape instead of a UnicodeEncodeError (the --output file is UTF-8)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")
    sys.stdout.write(report)
    if args.output is not None:
        args.output.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
