"""Measure the static check (python -m pytest_queryguard.lint) on the accuracy corpus.

    uv run python scripts/lint_accuracy_report.py

Lints each corpus scenario with its own models (no database, nothing runs)
and prints precision and recall for relationship-load at the level of
scenarios, then every scenario where the lint disagrees with the verdict.
See tests/corpus/lint_accuracy.py for the method.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.corpus import runner
from tests.corpus.lint_accuracy import measure


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.0f}%"


def main() -> int:
    scenarios = runner.load()
    score = measure(scenarios)
    print(f"# Static check on the accuracy corpus ({score.scenarios} scenarios)\n")
    print("| rule | TP | FP | FN | TN | precision | recall |")
    print("|---|---|---|---|---|---|---|")
    print(
        f"| relationship-load | {score.tp} | {score.fp} | {score.fn} | {score.tn} "
        f"| {pct(score.precision)} | {pct(score.recall)} |"
    )
    print("\n## Disagreements\n")
    if not score.disagreements:
        print("None.")
    for d in score.disagreements:
        kind = "missed" if d.positive else f"reported {', '.join(d.labels)}"
        print(f"- `{d.scenario}` ({kind}): {d.reason or 'UNEXPLAINED'}")
    if score.left_out:
        print("\n## Left out (findings in a scenario's setup)\n")
        for name, label in score.left_out:
            print(f"- `{name}`: {label}")
    if runner.SKIPPED:
        print(f"\nSkipped (imports missing): {runner.SKIPPED}")
    return 1 if any(d.reason is None for d in score.disagreements) else 0


if __name__ == "__main__":
    raise SystemExit(main())
