"""Overhead of each tool on one machine: per statement and on a whole suite.

    python scripts/head_to_head/overhead.py --queryguard . --out overhead.json

Builds one virtual environment per tool with ``uv`` (the same Python, pytest
and SQLAlchemy in each, the tool pinned as in ``common.TOOLS``), then, with
the tools interleaved and their order reversed every round, each run a fresh
process (pinned to one core with ``taskset`` when there is one):

* per statement: ``scripts/compare/bench_micro.py`` (a Core ``select``, an ORM
  ``select`` by primary key, one lazy load, against in-memory SQLite);
* a suite: the generated suite of ``scripts/bench_suite.py`` against file
  SQLite, each tool set up to look for N+1 in every test.

For each tool the best (lowest) time over the rounds is kept, and the median
too. A tool whose environment or run fails is recorded under ``failed`` and
left out; the others still run. Meant for the head-to-head workflow: it
installs the competitors, so do not run it where they must not be installed.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))

import bench_suite
import common

MICRO = ROOT / "scripts" / "compare" / "bench_micro.py"

# how each tool is told to look for N+1 in every test of the suite
SUITE_ARGS = {
    "off": ["-p", "no:queryguard"],
    "queryguard": ["--queryguard-mode=warn"],
    "queryspy": ["-p", "no:queryguard", "--queryspy-strict"],
    "querycount": ["-p", "no:queryguard"],
    "sniffer": ["-p", "no:queryguard"],
    "nplusone": ["-p", "no:queryguard"],
}
MARK_ALL = """
import pytest
def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.no_n_plus_one)
"""
SUITE_CONFTEST = {
    "querycount": MARK_ALL,
    "sniffer": MARK_ALL
    + """
@pytest.fixture
def nplusone_engine(engine):
    return engine
""",
    # every notification allowed: the detection runs, a test cannot fail
    "nplusone": """
import pytest
import nplusone.ext.sqlalchemy
from nplusone.core import profiler
ALLOW = [{"label": "n_plus_one"}, {"label": "unused_eager_load"}]
@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    with profiler.Profiler(whitelist=ALLOW):
        return (yield)
""",
}
DIST = {
    "queryguard": "pytest-queryguard",
    "queryspy": "queryspy",
    "querycount": "pytest-querycount",
    "sniffer": "n-plus-one-sniffer",
    "nplusone": "nplusone",
}


def venv(work: Path, name: str, packages: list[str], python: str) -> Path:
    path = work / name
    shutil.rmtree(path, ignore_errors=True)
    subprocess.run(
        ["uv", "venv", "-q", "--no-config", "--python", python, str(path)], check=True
    )
    subprocess.run(
        ["uv", "pip", "install", "-q", "--no-config", "--python",
         str(path / "bin" / "python"), *packages],
        check=True,
    )  # fmt: skip
    return path / "bin" / "python"


def pinned(command: list[str], core: str | None) -> list[str]:
    if core is not None and shutil.which("taskset"):
        return ["taskset", "-c", core, *command]
    return command


def micro_once(python: Path, name: str, statements: int, blocks: int, core: str | None):
    command = [str(python), "-I", str(MICRO), name, str(statements), str(blocks)]
    done = subprocess.run(
        pinned(command, core),
        capture_output=True,
        text=True,
        cwd=tempfile.gettempdir(),
        check=False,
    )
    if done.returncode:
        raise RuntimeError(f"{name} (micro): {done.stderr[-1500:]}")
    return {loop: min(times) for loop, times in json.loads(done.stdout).items()}


def suite_once(python: Path, name: str, tests: int, core: str | None) -> float:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "conftest.py").write_text(
            bench_suite.CONFTEST + SUITE_CONFTEST.get(name, "")
        )
        (root / "test_suite.py").write_text(
            bench_suite.TESTS.replace("__N__", str(tests))
        )
        out = root / "out.txt"
        env = dict(
            os.environ, QG_BENCH_URL=f"sqlite:///{tmp}/bench.db", QG_BENCH_OUT=str(out)
        )
        env.pop("PYTHONPATH", None)
        command = [
            str(python), "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
            str(root), "--rootdir", str(root), *SUITE_ARGS[name],
        ]  # fmt: skip
        done = subprocess.run(
            pinned(command, core),
            env=env,
            capture_output=True,
            text=True,
            cwd=tmp,
            check=False,
        )
        if done.returncode:
            raise RuntimeError(
                f"{name} (suite): {done.stdout[-1500:]}{done.stderr[-800:]}"
            )
        return float(out.read_text().split()[0])


def versions(python: Path) -> dict[str, str]:
    code = (
        "import importlib.metadata as m, json\n"
        f"names = {['pytest', 'sqlalchemy', *DIST.values()]!r}\n"
        "out = {}\n"
        "for n in names:\n"
        "    try: out[n] = m.version(n)\n"
        "    except m.PackageNotFoundError: pass\n"
        "print(json.dumps(out))\n"
    )
    done = subprocess.run(
        [str(python), "-c", code], check=True, capture_output=True, text=True
    )
    return json.loads(done.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--queryguard", default=".", help="path or requirement")
    parser.add_argument(
        "--python", default=f"{sys.version_info.major}.{sys.version_info.minor}"
    )
    parser.add_argument(
        "--work", type=Path, default=Path(tempfile.gettempdir()) / "qg-h2h"
    )
    parser.add_argument("--rounds", type=int, default=9)
    parser.add_argument("--statements", type=int, default=2000)
    parser.add_argument("--blocks", type=int, default=5)
    parser.add_argument("--tests", type=int, default=300)
    parser.add_argument("--core", default="0", help="CPU to pin to; empty: none")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    import pytest
    import sqlalchemy

    common_pkgs = [
        f"sqlalchemy=={sqlalchemy.__version__}",
        f"pytest=={pytest.__version__}",
    ]
    queryguard = str(ROOT) if args.queryguard == "." else args.queryguard
    wanted = {"off": []}
    for key in ("queryguard", "queryspy", "querycount", "sniffer", "nplusone"):
        wanted[key] = (
            [queryguard] if key == "queryguard" else [common.TOOLS[key].requirement]
        )
    failed: list[str] = []
    pythons: dict[str, Path] = {}
    for name, packages in wanted.items():
        try:
            pythons[name] = venv(
                args.work, name, [*common_pkgs, *packages], args.python
            )
        except subprocess.CalledProcessError as exc:
            failed.append(f"{name}: install failed ({exc})")
    core = args.core or None
    micro: dict[str, dict[str, list[float]]] = {n: {} for n in pythons}
    suite: dict[str, list[float]] = {n: [] for n in pythons}
    broken: set[str] = set()
    for r in range(args.rounds):
        order = list(pythons) if r % 2 == 0 else list(reversed(pythons))
        for name in order:
            if name in broken:
                continue
            try:
                for loop, t in micro_once(
                    pythons[name], name, args.statements, args.blocks, core
                ).items():
                    micro[name].setdefault(loop, []).append(t)
                suite[name].append(suite_once(pythons[name], name, args.tests, core))
            except RuntimeError as exc:
                broken.add(name)
                failed.append(str(exc))
        print(f"round {r + 1}/{args.rounds}", file=sys.stderr, flush=True)
    keep = [n for n in pythons if n not in broken]
    payload = {
        "rounds": args.rounds,
        "statements": args.statements,
        "tests": args.tests,
        "machine": f"{platform.machine()} {platform.processor()} {os.cpu_count()} CPUs",
        "python": platform.python_version(),
        "micro": {n: {loop: min(v) for loop, v in micro[n].items()} for n in keep},
        "micro_median": {
            n: {loop: statistics.median(v) for loop, v in micro[n].items()}
            for n in keep
        },
        "suite": {n: min(suite[n]) for n in keep},
        "suite_median": {n: statistics.median(suite[n]) for n in keep},
        "versions": {n: versions(pythons[n]) for n in pythons},
        "failed": failed,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print("\n".join(common.overhead_table(payload)))
    for line in failed:
        print("FAILED:", line, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
