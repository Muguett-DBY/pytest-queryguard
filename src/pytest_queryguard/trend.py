"""How query counts moved over several runs (``--queryguard-json`` files).

    python -m pytest_queryguard.trend RUN1.json RUN2.json ... [--top N]

The files are given oldest first, one per run (for example the artifacts of
the last runs on the main branch). The report has the statements of each run,
the tests whose count changed most between the first and the last run they
appear in, with a sparkline of every run in between, and the findings that
are new or gone between the oldest and the newest run (matched as
``compare`` matches them). A run that stopped early (``complete: false``) is
marked: tests missing from it did not run, they did not stop querying.

``--format`` picks Markdown (the default; for the job summary or a
pull-request comment), plain text or JSON. The sparklines use the Unicode
blocks; ``--ascii`` uses ASCII characters instead, and so does the console
output when the console's encoding has no blocks (a Windows console with
cp1252, the reason ``compare`` prints only ASCII). The exit status is 0
unless a file cannot be read (2; ``--skip-unreadable`` leaves such a file
out instead) or the arguments are wrong (2).
"""

from __future__ import annotations

import argparse
import fnmatch
import html
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .compare import CompareError, FindingRow, _code, _findings, _where, compare, load

# The "format" key of the JSON output; it goes up when a key changes meaning.
FORMAT_VERSION = 1

_TOP = 10
_ROWS = 25
BLOCKS = "▁▂▃▄▅▆▇█"
ASCII_BLOCKS = "_-~=+*#@"
_MISSING = "·"
_ASCII_MISSING = "."


@dataclass(frozen=True, slots=True)
class Run:
    label: str
    tests: int
    statements: int
    complete: bool


@dataclass(frozen=True, slots=True)
class TestTrend:
    nodeid: str
    counts: tuple[int | None, ...]  # one per run, None: not in that run

    @property
    def first(self) -> int:
        return next(c for c in self.counts if c is not None)

    @property
    def last(self) -> int:
        return next(c for c in reversed(self.counts) if c is not None)

    @property
    def delta(self) -> int:
        return self.last - self.first


@dataclass(frozen=True, slots=True)
class FindingTrend:
    row: FindingRow
    since: int  # index of the run from which on it is in every run


@dataclass(slots=True)
class Trend:
    runs: list[Run] = field(default_factory=list)
    changed: list[TestTrend] = field(default_factory=list)
    added_tests: int = 0  # not in the first run, in the last one
    gone_tests: int = 0  # in an earlier run, not in the last one
    new: list[FindingTrend] = field(default_factory=list)
    gone: list[FindingRow] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return all(run.complete for run in self.runs)


def _matches(nodeid: str, pattern: str) -> bool:
    """A glob when the pattern has one, a substring otherwise."""
    if any(char in pattern for char in "*?["):
        return fnmatch.fnmatchcase(nodeid, pattern)
    return pattern in nodeid


def _select(doc: dict[str, Any], pattern: str | None) -> dict[str, Any]:
    if not pattern:
        return doc
    tests = {k: v for k, v in doc["tests"].items() if _matches(k, pattern)}
    return {**doc, "tests": tests}


def trend(
    docs: list[dict[str, Any]], labels: list[str], *, test: str | None = None
) -> Trend:
    """The trend of ``docs``, oldest first, one label per document."""
    if not docs or len(docs) != len(labels):
        raise ValueError("one label per document, at least one document")
    docs = [_select(doc, test) for doc in docs]
    result = Trend()
    for doc, label in zip(docs, labels, strict=True):
        tests = doc["tests"]
        result.runs.append(
            Run(
                label,
                len(tests),
                sum(int(entry["statements"]) for entry in tests.values()),
                bool(doc.get("complete")),
            )
        )
    nodeids = sorted({nodeid for doc in docs for nodeid in doc["tests"]})
    first, last = docs[0]["tests"], docs[-1]["tests"]
    for nodeid in nodeids:
        counts = tuple(
            int(doc["tests"][nodeid]["statements"]) if nodeid in doc["tests"] else None
            for doc in docs
        )
        series = TestTrend(nodeid, counts)
        if nodeid not in last:
            result.gone_tests += 1
        # a test the newest run left out because it stopped early still
        # shows its trend up to the last run it was in
        if series.delta and (nodeid in last or not result.runs[-1].complete):
            result.changed.append(series)
        if nodeid in last and nodeid not in first:
            result.added_tests += 1
    result.changed.sort(key=lambda t: (-abs(t.delta), -t.delta, t.nodeid))
    # Findings: the oldest run against the newest, as compare does it; a
    # finding of a test that did not run in the newest run is not gone.
    pair = compare(docs[0], docs[-1])
    present = [set(_findings(doc["tests"], baselined=True)) for doc in docs]
    for row in pair.new:
        key = (row.kind, row.label, row.file, row.function)
        # the earliest run of the unbroken stretch up to the newest run; a run
        # that stopped early without it may just not have run its test
        since = len(docs) - 1
        for index in range(len(docs) - 2, -1, -1):
            if key in present[index]:
                since = index
            elif result.runs[index].complete:
                break
        result.new.append(FindingTrend(row, since))
    result.gone = pair.fixed
    return result


def sparkline(counts: tuple[int | None, ...], *, ascii: bool = False) -> str:
    """One character per run, scaled between the lowest and highest count."""
    blocks = ASCII_BLOCKS if ascii else BLOCKS
    missing = _ASCII_MISSING if ascii else _MISSING
    values = [c for c in counts if c is not None]
    if not values:
        return missing * len(counts)
    low, high = min(values), max(values)
    span = high - low
    top = len(blocks) - 1
    return "".join(
        missing
        if c is None
        else blocks[0 if not span else round((c - low) * top / span)]
        for c in counts
    )


def _signed(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


def _runs(result: Trend) -> str:
    n = len(result.runs)
    return f"{n} run" if n == 1 else f"{n} runs"


def _title(result: Trend) -> str:
    return f"pytest-queryguard: query counts over the last {_runs(result)}"


def _incomplete(result: Trend) -> list[str]:
    return [run.label for run in result.runs if not run.complete]


def _summary(result: Trend, ascii: bool) -> str:
    first, last = result.runs[0], result.runs[-1]
    line = sparkline(tuple(run.statements for run in result.runs), ascii=ascii)
    return (
        f"{_runs(result)}, statements {first.statements} -> "
        f"{last.statements} ({_signed(last.statements - first.statements)}) "
        f"`{line}`. {len(result.changed)} tests changed their count, "
        f"{result.added_tests} new, {result.gone_tests} not in the latest run; "
        f"{len(result.new)} new findings, {len(result.gone)} gone."
    )


def markdown(
    result: Trend,
    *,
    top: int = _TOP,
    ascii: bool = False,
    key: str = "",
    details: bool = False,
) -> str:
    """The report; ``details`` folds it into a collapsed <details> block."""
    title = _title(result)
    if key:
        title += f" ({key})"
    lines = (
        [f"<details><summary>{html.escape(title)}</summary>", ""]
        if details
        else [f"### {title}", ""]
    )
    lines.append(_summary(result, ascii))
    incomplete = _incomplete(result)
    if incomplete:
        lines += [
            "",
            (
                "> [!WARNING]\n> Stopped early or measurement failed in "
                f"{', '.join(_code(label) for label in incomplete)}: tests missing "
                "from those runs did not run, and their totals are partial."
            ),
        ]
    lines += ["", "| Run | Tests | Statements | |", "|---|---:|---:|---|"]
    for run in result.runs:
        note = "" if run.complete else "incomplete"
        lines.append(
            f"| {_code(run.label)} | {run.tests} | {run.statements} | {note} |"
        )
    if result.changed:
        lines += ["", "#### Tests whose count changed most", ""]
        lines += ["| Test | Runs | First | Last | Change |", "|---|---|---:|---:|---:|"]
        for t in result.changed[:top]:
            lines.append(
                f"| {_code(t.nodeid)} | `{sparkline(t.counts, ascii=ascii)}` | "
                f"{t.first} | {t.last} | {_signed(t.delta)} |"
            )
        lines += _more(len(result.changed), top)
    if result.new:
        lines += ["", "#### New findings in this window", ""]
        lines += ["| Kind | Label | Where | Since | Times |", "|---|---|---|---|---:|"]
        for f in result.new[:_ROWS]:
            since = result.runs[f.since].label
            lines.append(
                f"| {f.row.kind} | {_code(f.row.label)} | {_code(_where(f.row))} | "
                f"{_code(since)} | {f.row.count} |"
            )
        lines += _more(len(result.new), _ROWS)
    if result.gone:
        lines += ["", "#### Findings gone in this window", ""]
        lines += ["| Kind | Label | Where |", "|---|---|---|"]
        for row in result.gone[:_ROWS]:
            lines.append(f"| {row.kind} | {_code(row.label)} | {_code(_where(row))} |")
        lines += _more(len(result.gone), _ROWS)
    if details:
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def text(result: Trend, *, top: int = _TOP, ascii: bool = False) -> str:
    """The report for a console."""
    lines = [
        _title(result),
        _summary(result, ascii).replace("`", ""),
        "",
    ]
    width = max(len(run.label) for run in result.runs)
    for run in result.runs:
        note = "" if run.complete else "  (incomplete)"
        lines.append(
            f"  {run.label:<{width}}  {run.tests:>6} tests  "
            f"{run.statements:>8} statements{note}"
        )
    if result.changed:
        lines += ["", "Tests whose count changed most:"]
        for t in result.changed[:top]:
            lines.append(
                f"  {sparkline(t.counts, ascii=ascii)}  {t.first:>6} -> "
                f"{t.last:<6} {_signed(t.delta):>7}  {t.nodeid}"
            )
        lines += [line.strip() for line in _more(len(result.changed), top)]
    if result.new:
        lines += ["", "New findings in this window:"]
        for f in result.new:
            lines.append(
                f"  {f.row.kind}  {f.row.label}  ({_where(f.row)}), since "
                f"{result.runs[f.since].label}"
            )
    if result.gone:
        lines += ["", "Findings gone in this window:"]
        for row in result.gone:
            lines.append(f"  {row.kind}  {row.label}  ({_where(row)})")
    return "\n".join(lines) + "\n"


def as_json(result: Trend, *, top: int = _TOP) -> str:
    def finding(row: FindingRow) -> dict[str, Any]:
        return {
            "kind": row.kind,
            "label": row.label,
            "file": row.file,
            "function": row.function,
            "count": row.count,
            "tests": list(row.tests),
        }

    doc = {
        "format": FORMAT_VERSION,
        "runs": [
            {
                "label": run.label,
                "tests": run.tests,
                "statements": run.statements,
                "complete": run.complete,
            }
            for run in result.runs
        ],
        "complete": result.complete,
        "changed": [
            {
                "nodeid": t.nodeid,
                "counts": list(t.counts),
                "first": t.first,
                "last": t.last,
                "change": t.delta,
            }
            for t in result.changed[:top]
        ],
        "changed_total": len(result.changed),
        "added_tests": result.added_tests,
        "gone_tests": result.gone_tests,
        "new_findings": [
            {**finding(f.row), "since": result.runs[f.since].label} for f in result.new
        ],
        "gone_findings": [finding(row) for row in result.gone],
    }
    return json.dumps(doc, indent=2) + "\n"


def _more(total: int, shown: int) -> list[str]:
    return ["", f"...and {total - shown} more."] if total > shown else []


def default_labels(paths: list[Path]) -> list[str]:
    """The file names without .json when they differ, else 1, 2, 3..."""
    stems = [path.stem for path in paths]
    if len(set(stems)) == len(stems):
        return stems
    return [str(n) for n in range(1, len(paths) + 1)]


def _console_has_blocks() -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        (BLOCKS + _MISSING).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pytest_queryguard.trend",
        description=(
            "How query counts moved over several --queryguard-json files, "
            "given oldest first, one per run."
        ),
    )
    parser.add_argument("runs", type=Path, nargs="+", help="the files, oldest first")
    parser.add_argument(
        "--top",
        type=int,
        default=_TOP,
        help=f"how many changed tests to list (default {_TOP})",
    )
    parser.add_argument(
        "--test",
        metavar="PATTERN",
        help="only tests whose node id contains PATTERN (or matches it as a glob)",
    )
    parser.add_argument(
        "--format", choices=("markdown", "text", "json"), default="markdown"
    )
    parser.add_argument(
        "--ascii", action="store_true", help="ASCII sparklines instead of blocks"
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="name of a run, once per file in the same order (default: file names)",
    )
    parser.add_argument(
        "--key", default="", help="name of this trend (one per CI job), in the title"
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Markdown folded into a <details> block (for a pull-request comment)",
    )
    parser.add_argument(
        "--skip-unreadable",
        action="store_true",
        help="leave out a file that cannot be read instead of stopping (exit 2)",
    )
    parser.add_argument("--output", type=Path, help="write the report here too")
    args = parser.parse_args(argv)
    if args.label and len(args.label) != len(args.runs):
        parser.error(f"{len(args.label)} --label for {len(args.runs)} files")
    if args.top < 1:
        parser.error("--top must be at least 1")
    labels = args.label or default_labels(args.runs)
    docs, kept = [], []
    for path, label in zip(args.runs, labels, strict=True):
        try:
            docs.append(load(path))
        except CompareError as exc:
            print(f"pytest-queryguard trend: {exc}", file=sys.stderr)
            if not args.skip_unreadable:
                return 2
        else:
            kept.append(label)
    if not docs:
        print("pytest-queryguard trend: no file could be read", file=sys.stderr)
        return 2
    result = trend(docs, kept, test=args.test)

    def render(ascii: bool) -> str:
        if args.format == "json":
            return as_json(result, top=args.top)
        if args.format == "text":
            return text(result, top=args.top, ascii=ascii)
        return markdown(
            result, top=args.top, ascii=ascii, key=args.key, details=args.details
        )

    report = render(args.ascii)
    shown = report if args.ascii or _console_has_blocks() else render(True)
    # a label may hold any character; a console that cannot print it gets an
    # escape instead of a UnicodeEncodeError (the --output file is UTF-8)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")
    sys.stdout.write(shown)
    if args.output is not None:
        args.output.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
