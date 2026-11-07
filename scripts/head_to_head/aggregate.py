"""Put the head-to-head results together: a Markdown summary and one JSON file.

    python scripts/head_to_head/aggregate.py RESULTS_DIR --markdown summary.md --json results.json

RESULTS_DIR holds the ``TOOL.json`` files of ``run_corpus.py`` and, when the
overhead job ran, ``overhead.json`` of ``overhead.py`` (in any subdirectory:
the workflow downloads each job's artifact into its own). A tool whose file
is missing (its job failed, or it could not be installed) is listed as not
run. Needs no package beyond the standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common


def load(directory: Path) -> tuple[dict[str, dict], dict | None]:
    corpus: dict[str, dict] = {}
    overhead = None
    for path in sorted(directory.rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "micro" in data:
            overhead = data
        elif data.get("tool") in common.TOOLS:
            corpus[data["tool"]] = data
    return corpus, overhead


def verdicts(data: dict) -> dict[str, common.Verdict]:
    return {
        name: common.Verdict(r["status"], frozenset(r["kinds"]), r.get("detail", ""))
        for name, r in data["results"].items()
    }


def build(
    corpus: dict[str, dict], overhead: dict | None, run_url: str = ""
) -> tuple[str, dict]:
    """The Markdown summary and the JSON payload."""
    lines = ["# Head-to-head: pytest-queryguard and other SQLAlchemy N+1 tools", ""]
    if run_url:
        lines += [f"Workflow run: {run_url}", ""]
    payload: dict[str, object] = {"run": run_url, "tools": {}, "overhead": overhead}
    if not corpus:
        lines.append("No corpus results.")
    else:
        # The scenarios every tool's run loaded; a tool that loaded others
        # (a different set of optional packages) is scored on the common set.
        names = set.intersection(*(set(d["scenarios"]) for d in corpus.values()))
        first = next(iter(corpus.values()))
        scenarios = {n: first["scenarios"][n] for n in sorted(names)}
        bugs = sum(m["verdict"] == "BUG" for m in scenarios.values())
        lines += [
            (
                f"## Accuracy on the corpus ({len(scenarios)} scenarios: {bugs} BUG, "
                f"{len(scenarios) - bugs} CLEAN)"
            ),
            "",
            (
                "Each tool with its defaults plus what its documentation says makes it fail a "
                "test on an N+1. `precision`: flagged BUG / all flagged. `recall (all BUG)`: over "
                "every BUG scenario, including kinds the tool does not claim. A scenario where the "
                "test failed for another reason than the tool's report is an error: never a pass."
            ),
            "",
        ]
        scores = {}
        for key in common.COMPARED:
            if key in corpus:
                scores[key] = common.score(
                    common.TOOLS[key], scenarios, verdicts(corpus[key])
                )
        lines += common.accuracy_tables(scores)
        missing = [common.TOOLS[k].label for k in common.COMPARED if k not in corpus]
        if missing:
            lines += [
                "",
                "Not run (job failed or tool not installable): " + ", ".join(missing),
            ]
        for key in ("none", "none-sa20"):
            if key not in corpus:
                continue
            base = common.score(common.TOOLS[key], scenarios, verdicts(corpus[key]))
            failing = [name for name, _ in base.errors]
            lines += [
                "",
                f"{common.TOOLS[key].label}, every plugin off: "
                + (
                    "every scenario passes."
                    if not failing
                    else f"{len(failing)} scenarios fail on their own: "
                    + ", ".join(f"`{n}`" for n in failing)
                ),
            ]
        lines += ["", "### Settings and versions", ""]
        for key in ("none", "none-sa20", *common.COMPARED):
            if key not in corpus:
                continue
            tool, data = common.TOOLS[key], corpus[key]
            v = data["versions"]
            pkg = {
                "queryguard": "pytest-queryguard",
                "queryguard-all": "pytest-queryguard",
                "queryspy": "queryspy",
                "querycount": "pytest-querycount",
                "sniffer": "n-plus-one-sniffer",
                "nplusone": "nplusone",
                "nplusone-sa20": "nplusone",
            }.get(key)
            version = f" {v.get(pkg, '?')}" if pkg else ""
            lines.append(
                f"- **{tool.label}**{version}: {tool.notes} Python {v.get('python')}, "
                f"SQLAlchemy {v.get('sqlalchemy')}, pytest {v.get('pytest')}."
            )
        lines += ["", "### Errors, false positives and misses per tool", ""]
        for key, s in scores.items():
            label = common.TOOLS[key].label
            lines.append(
                f"<details><summary>{label}: {len(s.errors)} errors, "
                f"{len(s.false_positives)} CLEAN flagged, {len(s.misses)} in-scope BUG missed"
                "</summary>\n"
            )
            for name, detail in s.errors:
                lines.append(f"- error `{name}`: {detail.replace('|', '/')}")
            for name in s.false_positives:
                lines.append(f"- CLEAN flagged `{name}`")
            for name in s.misses:
                lines.append(f"- missed `{name}`")
            lines.append("\n</details>\n")
        for key, s in scores.items():
            payload["tools"][key] = {  # type: ignore[index]
                "label": common.TOOLS[key].label,
                "versions": corpus[key]["versions"],
                "bug_flagged": s.bug_flagged,
                "bug_missed": s.bug_missed,
                "bug_error": s.bug_error,
                "clean_passed": s.clean_passed,
                "clean_flagged": s.clean_flagged,
                "clean_error": s.clean_error,
                "precision": s.precision,
                "recall": s.recall,
                "in_scope_recall": s.in_scope_recall,
                "kinds": {
                    k: None if t is None else vars(t) for k, t in s.kinds.items()
                },
                "errors": s.errors,
                "false_positives": s.false_positives,
                "misses": s.misses,
                "results": corpus[key]["results"],
            }
        payload["scenarios"] = scenarios
    lines += ["", "## Overhead", ""]
    if overhead is None:
        lines.append("No overhead results.")
    else:
        lines += [
            (
                f"Best of {overhead.get('rounds')} runs per tool, all tools on the same runner, "
                "interleaved; added time against the same run with no tool."
            ),
            "",
            *common.overhead_table(overhead),
        ]
        if overhead.get("failed"):
            lines += ["", "Failed: " + "; ".join(overhead["failed"])]
        if overhead.get("versions"):
            lines += ["", "```", json.dumps(overhead["versions"], indent=1), "```"]
    return "\n".join(lines) + "\n", payload


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("results", type=Path)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    corpus, overhead = load(args.results)
    url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        url = (
            f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
            f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
    markdown, payload = build(corpus, overhead, url)
    args.markdown.write_text(markdown, encoding="utf-8")
    args.json.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
