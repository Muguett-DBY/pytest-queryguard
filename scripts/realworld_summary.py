"""Summarise one project's run of .github/workflows/realworld.yml as Markdown.

The workflow runs a project's test suite twice: without the plugin
(``-p no:queryguard``) and with it in warn mode. This script reads what the two
runs left in one directory and prints a Markdown report:

- ``baseline.xml``, ``plugin.xml``: the JUnit XML of each run (``--junitxml``);
- ``baseline.meta.json``, ``plugin.meta.json``: ``{"exit": N, "seconds": S}``;
- ``queryguard.json``: the plugin run's ``--queryguard-json`` file;
- ``probe.jsonl``: what scripts/realworld_probe.py recorded (internal
  warnings and internal errors).

Any of them may be missing (a crashed or timed-out run); the report says so.
It lists the tests that pass without the plugin and fail with it (the plugin
broke them), the plugin's internal warnings and errors, its findings by kind
with ``file:line`` samples, and the overhead in wall time.

The exit status is 1 when the plugin broke something (a test it made fail, an
internal warning or error, a run that stopped or timed out only with the
plugin, a JSON file that is missing or incomplete) and 0 otherwise. Findings
alone are not a failure: they are what is being measured.

Standard library only:

    python scripts/realworld_summary.py --dir out --project sqlmodel --ref 0.0.24
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# worst first: a test listed twice (a call and a teardown error) keeps the worst
_RANK = {"failed": 0, "skipped": 1, "passed": 2}

EXIT_NAMES = {
    0: "all passed",
    1: "some tests failed",
    2: "interrupted",
    3: "internal error",
    4: "usage error",
    5: "no tests collected",
    124: "timed out",
    137: "killed",
}


@dataclass
class Run:
    """One pytest run: per-test outcomes, exit status and wall time."""

    outcomes: dict[str, str] | None  # None: no JUnit XML
    messages: dict[str, str] = field(default_factory=dict)
    exit: int | None = None
    seconds: float | None = None

    def count(self, outcome: str) -> int:
        return sum(1 for o in (self.outcomes or {}).values() if o == outcome)


def read_junit(path: Path) -> tuple[dict[str, str], dict[str, str]] | None:
    """Outcome (passed, failed, skipped) and failure message per test id."""
    if not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    outcomes: dict[str, str] = {}
    messages: dict[str, str] = {}
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        test_id = f"{classname}::{name}" if classname else name
        outcome = "passed"
        message = ""
        for child in case:
            if child.tag in ("failure", "error"):
                outcome = "failed"
                message = child.get("message") or (child.text or "")
                break
            if child.tag == "skipped":
                outcome = "skipped"
                message = "skipped: " + (child.get("message") or "")
        previous = outcomes.get(test_id)
        if previous is None or _RANK[outcome] < _RANK[previous]:
            outcomes[test_id] = outcome
            if message:
                messages[test_id] = message
    return outcomes, messages


def read_meta(path: Path) -> tuple[int | None, float | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    try:
        exit_code: int | None = int(data["exit"])
    except (KeyError, TypeError, ValueError):
        exit_code = None
    try:
        seconds: float | None = float(data["seconds"])
    except (KeyError, TypeError, ValueError):
        seconds = None
    return exit_code, seconds


def read_run(directory: Path, name: str) -> Run:
    junit = read_junit(directory / f"{name}.xml")
    exit_code, seconds = read_meta(directory / f"{name}.meta.json")
    if junit is None:
        return Run(None, exit=exit_code, seconds=seconds)
    outcomes, messages = junit
    return Run(outcomes, messages, exit_code, seconds)


def read_probe(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def read_counts(path: Path) -> dict[str, Any] | str | None:
    """The plugin's JSON file; None when missing, a reason when unusable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    except ValueError as error:
        return f"not JSON ({error})"
    # the checks of pytest_queryguard.compare.load, plus the entries' shape
    if not isinstance(data, dict) or data.get("format") != 1:
        return "not a format 1 queryguard JSON file"
    tests = data.get("tests")
    if not isinstance(tests, dict) or not all(
        isinstance(entry, dict) and isinstance(entry.get("findings", []), list)
        for entry in tests.values()
    ):
        return "its tests are not a mapping of entries"
    return data


def _one_line(text: str, limit: int = 160) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    first = first.replace("|", "\\|")
    return first if len(first) <= limit else first[: limit - 1] + "…"


def _exit(run: Run) -> str:
    if run.exit is None:
        return "unknown"
    return f"{run.exit} ({EXIT_NAMES.get(run.exit, 'other')})"


def _seconds(run: Run) -> str:
    return "unknown" if run.seconds is None else f"{run.seconds:.1f} s"


def overhead(baseline: Run, plugin: Run) -> float | None:
    """Extra wall time of the plugin run, in percent of the baseline."""
    if baseline.seconds is None or plugin.seconds is None or baseline.seconds <= 0:
        return None
    # a run that stopped early is not comparable
    if baseline.exit not in (0, 1) or plugin.exit not in (0, 1):
        return None
    return (plugin.seconds - baseline.seconds) / baseline.seconds * 100


def summarize(
    directory: Path, project: str, ref: str, samples: int = 5
) -> tuple[str, list[str]]:
    """The Markdown report and the list of problems (empty: nothing broke)."""
    baseline = read_run(directory, "baseline")
    plugin = read_run(directory, "plugin")
    counts = read_counts(directory / "queryguard.json")
    events = read_probe(directory / "probe.jsonl")
    problems: list[str] = []
    out: list[str] = [f"## {project} @ `{ref}`", ""]

    # the two runs side by side
    out += [
        "| | without the plugin | with the plugin (warn) |",
        "|---|---|---|",
        f"| exit status | {_exit(baseline)} | {_exit(plugin)} |",
        f"| wall time | {_seconds(baseline)} | {_seconds(plugin)} |",
    ]
    for outcome in ("passed", "failed", "skipped"):
        cells = [
            str(run.count(outcome)) if run.outcomes is not None else "no JUnit XML"
            for run in (baseline, plugin)
        ]
        out.append(f"| {outcome} | {cells[0]} | {cells[1]} |")
    out.append("")
    extra = overhead(baseline, plugin)
    if extra is not None:
        out += [f"Overhead: **{extra:+.1f} %** wall time (one run each, noisy).", ""]
    else:
        out += ["Overhead: not comparable (a run is missing or stopped early).", ""]

    if baseline.outcomes is None:
        out += ["The run without the plugin left no JUnit XML.", ""]
    if plugin.outcomes is None and baseline.outcomes is not None:
        problems.append("the run with the plugin left no JUnit XML")
    if plugin.exit in (2, 3, 4, 124, 137) and plugin.exit != baseline.exit:
        problems.append(f"the run with the plugin ended with {_exit(plugin)}")

    # tests the plugin broke
    if baseline.outcomes is not None and plugin.outcomes is not None:
        broken = sorted(
            test
            for test, outcome in baseline.outcomes.items()
            if outcome == "passed"
            and plugin.outcomes.get(test) in ("failed", "skipped")
        )
        missing = sorted(set(baseline.outcomes) - set(plugin.outcomes))
        fixed = sorted(
            test
            for test, outcome in baseline.outcomes.items()
            if outcome == "failed" and plugin.outcomes.get(test) == "passed"
        )
        only_plugin = sorted(set(plugin.outcomes) - set(baseline.outcomes))
        out += [f"### Tests that pass only without the plugin: {len(broken)}", ""]
        for test in broken[:50]:
            message = _one_line(plugin.messages.get(test, ""))
            out.append(f"- `{test}`" + (f": {message}" if message else ""))
        if len(broken) > 50:
            out.append(f"- … and {len(broken) - 50} more")
        if broken:
            out.append("")
            problems.append(f"{len(broken)} test(s) pass only without the plugin")
        if missing:
            out += [
                (
                    f"{len(missing)} test(s) ran only without the plugin, "
                    f"for example `{missing[0]}`."
                ),
                "",
            ]
            problems.append(f"{len(missing)} test(s) did not run with the plugin")
        if only_plugin:
            out += [
                (
                    f"{len(only_plugin)} test(s) ran only with the plugin, "
                    f"for example `{only_plugin[0]}`."
                ),
                "",
            ]
        if fixed:
            out += [
                (
                    f"{len(fixed)} test(s) failed without the plugin and passed "
                    f"with it (flaky?), for example `{fixed[0]}`."
                ),
                "",
            ]

    # the plugin's own errors
    internal = [e for e in events if e.get("event") == "internal-warning"]
    errors = [e for e in events if e.get("event") == "internal-error"]
    out += [
        f"### Internal warnings: {len(internal)}, internal errors: {len(errors)}",
        "",
    ]
    by_message: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in internal:
        by_message[_one_line(str(event.get("message", "")))].append(event)
    for message, group in sorted(by_message.items(), key=lambda kv: -len(kv[1])):
        first = group[0]
        out.append(f"- {len(group)}× {message} (first in `{first.get('nodeid', '')}`)")
    for event in errors[:5]:
        out.append(f"- INTERNALERROR: {_one_line(str(event.get('message', '')))}")
    if internal or errors:
        out.append("")
        problems.append(
            f"{len(internal)} internal warning(s), {len(errors)} internal error(s)"
        )

    # findings
    if counts is None:
        out += ["### Findings", "", "No queryguard JSON file.", ""]
        if plugin.exit in (0, 1):
            problems.append("the run with the plugin wrote no queryguard JSON")
    elif isinstance(counts, str):
        out += ["### Findings", "", f"Unusable queryguard JSON file: {counts}.", ""]
        problems.append(f"the queryguard JSON file is unusable: {counts}")
    else:
        tests = counts.get("tests") or {}
        if counts.get("complete") is not True:
            problems.append("the queryguard JSON says the run is incomplete")
        by_kind: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        statements = 0
        for test_id, entry in tests.items():
            if isinstance(entry.get("statements"), int):
                statements += entry["statements"]
            for finding in entry.get("findings") or []:
                if not isinstance(finding, dict):
                    continue
                by_kind[str(finding.get("kind", "?"))].append((test_id, finding))
        total = sum(len(v) for v in by_kind.values())
        out += [
            f"### Findings: {total}",
            "",
            (
                f"{len(tests)} tests measured, {statements} statements in test "
                f"bodies; JSON complete: {counts.get('complete')}."
            ),
            "",
        ]
        if by_kind:
            out += ["| kind | findings | tests |", "|---|---|---|"]
            for kind, items in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
                n_tests = len({test for test, _ in items})
                out.append(f"| `{kind}` | {len(items)} | {n_tests} |")
            out.append("")
            for kind, items in sorted(by_kind.items()):
                out += [f"<details><summary><code>{kind}</code> samples</summary>", ""]
                for test_id, finding in items[:samples]:
                    where = f"{finding.get('file', '?')}:{finding.get('line', '?')}"
                    label = finding.get("label", "")
                    count = finding.get("count", "")
                    out.append(f"- `{where}` {label} ×{count} in `{test_id}`")
                out += ["", "</details>", ""]

    if problems:
        out += ["### Problems", ""] + [f"- {p}" for p in problems] + [""]
    return "\n".join(out), problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dir", type=Path, required=True, help="the runs' outputs")
    parser.add_argument("--project", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--samples", type=int, default=5, help="per finding kind")
    args = parser.parse_args(argv)
    report, problems = summarize(args.dir, args.project, args.ref, args.samples)
    sys.stdout.write(report)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
