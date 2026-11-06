"""Run the accuracy corpus and print precision, recall and every mismatch.

    uv run python scripts/accuracy_report.py                 # pytest-queryguard, every configuration
    uv run python scripts/accuracy_report.py --compare PY    # also queryspy and pytest-querycount

``--compare`` takes the Python of a scratch environment where queryspy and
pytest-querycount are installed next to the corpus's own dependencies; the
project never depends on them. Those tools only say pass or fail per scenario,
so the comparison is at the level of scenarios, not of finding kinds.

PostgreSQL: set QUERYGUARD_CORPUS_PG_URL to a pg8000 URL to run the corpus there.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.corpus import kit, runner
from tests.corpus.metrics import Score, score

# tool -> (name for the conftest, pytest args, ini): each tool's defaults plus
# what makes it fail a test without a per-test marker, the other one switched off.
OTHERS: dict[str, tuple[str, tuple[str, ...], str]] = {
    # queryspy only fails tests with --queryspy-strict.
    "queryspy": ("queryspy", ("--queryspy-strict", "-p", "no:querycount"), ""),
    # pytest-querycount checks the tests that carry @pytest.mark.no_n_plus_one;
    # the corpus conftest adds the marker to every scenario.
    "pytest-querycount": ("querycount", ("-p", "no:queryspy"), ""),
}


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.0f}%"


def kind_table(config: str, result: Score) -> list[str]:
    lines = [
        "| kind | TP | FP | FN | precision | recall |",
        "|---|---|---|---|---|---|",
    ]
    for kind, t in result.kinds.items():
        lines.append(
            f"| {kind} | {t.tp} | {t.fp} | {t.fn} | {pct(t.precision)} | {pct(t.recall)} |"
        )
    return lines


def mismatch_table(result: Score) -> list[str]:
    if not result.mismatches:
        return ["No mismatches."]
    lines = [
        "| scenario | verdict | expected | reported | status |",
        "|---|---|---|---|---|",
    ]
    for m in result.mismatches:
        status = f"documented: {m.known}" if m.known else "UNEXPLAINED"
        lines.append(
            f"| `{m.scenario}` | {m.verdict} | {', '.join(sorted(m.expected)) or '-'} "
            f"| {', '.join(sorted(m.observed)) or '-'} | {status} |"
        )
    return lines


def run_queryguard(workdir: Path) -> dict[str, dict[str, runner.Result]]:
    names = [s.name for s in runner.load()]
    found = {}
    for config, (ini, _) in runner.CONFIGS.items():
        found[config] = runner.run_subprocess(workdir / config, names, ini=ini)
    return found


def run_other(workdir: Path, python: str, tool: str) -> dict[str, runner.Result]:
    label, args, ini = OTHERS[tool]
    names = [s.name for s in runner.load()]
    return runner.run_subprocess(
        workdir / tool, names, ini=ini, args=args, python=python, tool=label
    )


def versions(python: str | None = None) -> dict[str, str]:
    out = {"python": platform.python_version(), "platform": platform.platform()}
    for name in (
        "pytest-queryguard",
        "sqlalchemy",
        "pytest",
        "sqlmodel",
        "flask-sqlalchemy",
        "fastapi",
    ):
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = "not installed"
    return out


def binary(
    scenarios: list[runner.Scenario], results: dict[str, runner.Result]
) -> dict[str, int]:
    tally = {"bug_found": 0, "bug_missed": 0, "clean_passed": 0, "clean_flagged": 0}
    for s in scenarios:
        flagged = results[s.name].failed
        if s.verdict == kit.BUG:
            tally["bug_found" if flagged else "bug_missed"] += 1
        else:
            tally["clean_flagged" if flagged else "clean_passed"] += 1
    return tally


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--compare",
        metavar="PYTHON",
        help="Python with queryspy and pytest-querycount installed",
    )
    parser.add_argument("--json", metavar="FILE", help="write the raw results here")
    parser.add_argument(
        "--workdir",
        metavar="DIR",
        help="where the generated sessions go (default: a temp directory)",
    )
    args = parser.parse_args()

    scenarios = runner.load()
    with tempfile.TemporaryDirectory(dir=args.workdir) as tmp:
        work = Path(tmp)
        raw = run_queryguard(work)
        others = (
            {tool: run_other(work, args.compare, tool) for tool in OTHERS}
            if args.compare
            else {}
        )
    bugs = sum(s.verdict == kit.BUG for s in scenarios)
    print(
        f"{len(scenarios)} scenarios ({bugs} BUG, {len(scenarios) - bugs} CLEAN); skipped: {runner.SKIPPED or 'none'}"
    )
    print(json.dumps(versions()))
    scores = {
        config: score(config, scenarios, raw[config]) for config in runner.CONFIGS
    }
    for config, result in scores.items():
        print(
            f"\n### {config}: {result.bug_found} of {result.bug_found + result.bug_missed} BUG scenarios flagged, "
            f"{result.clean_flagged} of {result.clean_flagged + result.clean_passed} CLEAN scenarios flagged\n"
        )
        print("\n".join(kind_table(config, result)))
        print()
        print("\n".join(mismatch_table(result)))
    if others:
        print(
            "\n### Comparison (scenario level, default configuration of each tool, strict mode)\n"
        )
        print("| tool | BUG flagged | BUG missed | CLEAN passed | CLEAN flagged |")
        print("|---|---|---|---|---|")
        rows = {
            "pytest-queryguard, defaults": binary(scenarios, raw["default"]),
            "pytest-queryguard, all opt-in rules": binary(scenarios, raw["all-opt-in"]),
            **{t: binary(scenarios, r) for t, r in others.items()},
        }
        for tool, t in rows.items():
            print(
                f"| {tool} | {t['bug_found']} | {t['bug_missed']} | {t['clean_passed']} | {t['clean_flagged']} |"
            )
    if args.json:
        payload = {
            "versions": versions(),
            "queryguard": {
                c: {
                    n: {
                        "failed": r.failed,
                        "findings": r.findings,
                        "warnings": r.warnings,
                    }
                    for n, r in res.items()
                }
                for c, res in raw.items()
            },
            "others": {
                t: {n: {"failed": r.failed} for n, r in res.items()}
                for t, res in others.items()
            },
        }
        Path(args.json).write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
