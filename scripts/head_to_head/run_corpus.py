"""Run the accuracy corpus under one tool and write its per-scenario verdicts.

    python scripts/head_to_head/run_corpus.py TOOL --out results/TOOL.json

TOOL is a key of ``common.TOOLS``. Run it with the Python of an environment
that has the corpus's dependencies (``uv sync``) and the tool installed (the
``requirement`` in ``common.TOOLS``); the head-to-head workflow does that in
GitHub Actions, one job per tool. Nothing here installs anything.

The session is the one of ``scripts/accuracy_report.py`` (``tests/corpus/
runner.py``): one test per scenario, the scenario's setup in a fixture, its
``run()`` in the test. ``session_plugin.py`` connects the tool and records
every test's outcome; ``common.classify`` turns that into a verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import common
from tests.corpus import runner

DISTRIBUTIONS = (
    "pytest",
    "sqlalchemy",
    "pytest-queryguard",
    "queryspy",
    "pytest-querycount",
    "n-plus-one-sniffer",
    "nplusone",
    "fastapi",
    "flask-sqlalchemy",
    "sqlmodel",
)


def versions() -> dict[str, str]:
    out = {"python": platform.python_version(), "platform": platform.platform()}
    for name in DISTRIBUTIONS:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    if os.environ.get("GITHUB_SHA"):
        out["commit"] = os.environ["GITHUB_SHA"]
    return out


def run(tool: common.Tool, work: Path, timeout: int) -> dict[str, object]:
    scenarios = runner.load()
    names = [s.name for s in scenarios]
    ini = runner.CONFIGS[tool.config][0] if tool.config else ""
    out = runner.write_session(work, ini, names)
    shutil.copy(HERE / "session_plugin.py", work / "session_plugin.py")
    lines_path = work / "h2h.jsonl"
    env = {
        **os.environ,
        # the corpus conftest connects to pytest-queryguard only for "queryguard"
        **runner.session_env(names, out, "queryguard" if tool.config else "other"),
        "H2H_TOOL": tool.key,
        "H2H_OUT": str(lines_path),
        "PYTHONPATH": os.pathsep.join(
            p for p in (str(work), str(ROOT), os.environ.get("PYTHONPATH")) if p
        ),
    }
    command = [
        sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-W", "ignore",
        "-q", "-p", "session_plugin", f"--timeout={timeout}", *tool.args,
    ]  # fmt: skip
    proc = subprocess.run(
        command, cwd=work, env=env, capture_output=True, text=True, check=False
    )
    lines = []
    if lines_path.exists():
        for raw in lines_path.read_text(encoding="utf-8").splitlines():
            lines.append(json.loads(raw))
    phases = common.group_lines(lines)
    findings: dict[str, frozenset[str]] = {}
    if tool.config and out.exists():
        findings = {name: r.kinds for name, r in runner.read_results(out).items()}
    results = {}
    for name in names:
        verdict = common.classify(tool, phases.get(name, []), findings.get(name))
        results[name] = {
            "status": verdict.status,
            "kinds": sorted(verdict.kinds),
            "detail": verdict.detail,
        }
    return {
        "tool": tool.key,
        "label": tool.label,
        "requirement": tool.requirement,
        "args": list(tool.args),
        "versions": versions(),
        "scenarios": {
            s.name: {"verdict": s.verdict, "kinds": sorted(s.kinds)} for s in scenarios
        },
        "skipped": [list(pair) for pair in runner.SKIPPED],
        "results": results,
        "returncode": proc.returncode,
        "output_tail": (proc.stdout[-3000:] + proc.stderr[-3000:]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n\n")[0],
    )
    parser.add_argument("tool", choices=sorted(common.TOOLS))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--timeout", type=int, default=300, help="seconds per scenario (pytest-timeout)"
    )
    args = parser.parse_args()
    tool = common.TOOLS[args.tool]
    with tempfile.TemporaryDirectory() as tmp:
        payload = run(tool, Path(tmp), args.timeout)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    statuses = [r["status"] for r in payload["results"].values()]  # type: ignore[attr-defined]
    print(
        f"{tool.label}: "
        + ", ".join(f"{s} {statuses.count(s)}" for s in sorted(set(statuses)))
        + f" (pytest exit {payload['returncode']})"
    )
    if statuses.count(common.ERROR) == len(statuses):
        print(payload["output_tail"], file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
