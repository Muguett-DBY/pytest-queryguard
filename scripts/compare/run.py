"""Overhead of pytest-queryguard next to queryspy and pytest-querycount.

    uv run python scripts/compare/run.py                      # the defaults below
    uv run python scripts/compare/run.py --queryguard . --rounds 15

Builds one virtual environment per tool with ``uv`` (same Python, pytest and
SQLAlchemy in each, the tool pinned), then measures, interleaving the tools
and reversing their order every round, each run in a fresh process pinned to
one core (``taskset``, when there is one):

* per statement: a Core ``select`` by key, an ORM ``select`` by primary key
  and one lazy load, against in-memory SQLite (``bench_micro.py``); per
  process the fastest of ``--blocks`` blocks, then the median over rounds;
* a whole suite: the generated suite of ``scripts/bench_suite.py`` against
  file SQLite, each tool with the settings that make it report N+1.

It prints a Markdown table with the versions, for docs/performance.md.
"""

from __future__ import annotations

import argparse
import importlib.util
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

# The released versions compared on the last run (docs/performance.md).
QUERYSPY = "queryspy==0.6.0"
QUERYCOUNT = "pytest-querycount==0.4.0"
QUERYGUARD = "pytest-queryguard==0.5.0"

# What makes each tool look for N+1 in the suite.
SUITE_FLAGS = {
    "off": [],
    "queryguard": ["--queryguard-mode=warn"],
    "queryspy": ["--queryspy-strict"],
    "querycount": [],
}
QUERYCOUNT_CONFTEST = """
import pytest
def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.no_n_plus_one(threshold=3))
"""
# the plugins other than the one measured stay out of each run
OTHERS_OFF = {
    "off": ["-p", "no:queryguard"],
    "queryguard": [],
    "queryspy": ["-p", "no:queryguard"],
    "querycount": ["-p", "no:queryguard"],
}


def _bench_suite():
    spec = importlib.util.spec_from_file_location(
        "bench_suite", ROOT / "scripts" / "bench_suite.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _versions(python: Path) -> dict[str, str]:
    code = (
        "import importlib.metadata as m, json\n"
        "names = ['pytest', 'sqlalchemy', 'pytest-queryguard', 'queryspy', 'pytest-querycount']\n"
        "out = {}\n"
        "for n in names:\n"
        "    try: out[n] = m.version(n)\n"
        "    except m.PackageNotFoundError: pass\n"
        "print(json.dumps(out))\n"
    )
    return json.loads(
        subprocess.run(
            [str(python), "-c", code], check=True, capture_output=True, text=True
        ).stdout
    )


def _venv(work: Path, tool: str, packages: list[str], python: str) -> Path:
    """The environment of one tool; rebuilt when its package list changes."""
    path = work / tool
    stamp = path / "queryguard-compare.txt"
    wanted = "\n".join([python, *packages])
    if not stamp.exists() or stamp.read_text() != wanted:
        shutil.rmtree(path, ignore_errors=True)
        subprocess.run(
            ["uv", "venv", "-q", "--no-config", "--python", python, str(path)],
            check=True,
        )
        subprocess.run(
            # throwaway environments: not under the project's exclude-newer
            ["uv", "pip", "install", "-q", "--no-config", "--python",
             str(path / "bin" / "python"), *packages],
            check=True,
        )  # fmt: skip
        stamp.write_text(wanted)  # written last: a failed install is redone
    else:
        # a checkout's path stays the same while its code changes
        local = [p for p in packages if Path(p).exists()]
        if local:
            subprocess.run(
                ["uv", "pip", "install", "-q", "--no-config", "--reinstall",
                 "--no-deps", "--python", str(path / "bin" / "python"), *local],
                check=True,
            )  # fmt: skip
    return path / "bin" / "python"


def _pinned(command: list[str], core: str | None) -> list[str]:
    if core is not None and shutil.which("taskset"):
        return ["taskset", "-c", core, *command]
    return command


def _micro(tools, rounds: int, statements: int, blocks: int, core: str | None):
    results: dict[str, dict[str, list[float]]] = {name: {} for name in tools}
    for r in range(rounds):
        order = list(tools.items()) if r % 2 == 0 else list(reversed(tools.items()))
        for name, python in order:
            command = [
                str(python),
                "-I",
                str(HERE / "bench_micro.py"),
                name,
                str(statements),
                str(blocks),
            ]
            done = subprocess.run(
                _pinned(command, core),
                check=False,
                capture_output=True,
                text=True,
                cwd=tempfile.gettempdir(),
            )
            if done.returncode:
                sys.exit(f"{name} (micro) failed:\n{done.stderr[-2000:]}")
            for loop, times in json.loads(done.stdout).items():
                results[name].setdefault(loop, []).append(min(times))
        print(f"micro: round {r + 1}/{rounds}", file=sys.stderr, flush=True)
    return {
        name: {loop: statistics.median(v) for loop, v in loops.items()}
        for name, loops in results.items()
    }


def _suite(tools, rounds: int, tests: int, core: str | None):
    bench = _bench_suite()
    results: dict[str, list[float]] = {name: [] for name in tools}
    for r in range(rounds):
        order = list(tools.items()) if r % 2 == 0 else list(reversed(tools.items()))
        for name, python in order:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                conftest = bench.CONFTEST + (
                    QUERYCOUNT_CONFTEST if name == "querycount" else ""
                )
                (root / "conftest.py").write_text(conftest)
                (root / "test_suite.py").write_text(
                    bench.TESTS.replace("__N__", str(tests))
                )
                out = root / "out.txt"
                env = dict(
                    os.environ,
                    QG_BENCH_URL=f"sqlite:///{tmp}/bench.db",
                    QG_BENCH_OUT=str(out),
                )
                env.pop("PYTHONPATH", None)
                command = [
                    str(python), "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                    str(root), "--rootdir", str(root), *OTHERS_OFF[name], *SUITE_FLAGS[name],
                ]  # fmt: skip
                done = subprocess.run(
                    _pinned(command, core),
                    check=False,
                    env=env,
                    capture_output=True,
                    text=True,
                    cwd=tmp,
                )
                if done.returncode:
                    sys.exit(
                        f"{name} (suite) failed:\n{done.stdout[-3000:]}{done.stderr[-2000:]}"
                    )
                wall, _cpu = out.read_text().split()
                results[name].append(float(wall))
        print(f"suite: round {r + 1}/{rounds}", file=sys.stderr, flush=True)
    return {name: statistics.median(v) for name, v in results.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--queryguard",
        default=QUERYGUARD,
        help="requirement or path (default: %(default)s)",
    )
    parser.add_argument(
        "--python", default=f"{sys.version_info.major}.{sys.version_info.minor}"
    )
    parser.add_argument(
        "--work", type=Path, default=Path(tempfile.gettempdir()) / "queryguard-compare"
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=15,
        help="processes per tool (default: %(default)s)",
    )
    parser.add_argument("--statements", type=int, default=2000)
    parser.add_argument("--blocks", type=int, default=5)
    parser.add_argument("--suite-rounds", type=int, default=9)
    parser.add_argument("--tests", type=int, default=300)
    parser.add_argument("--core", default="0", help="CPU to pin to; empty: none")
    args = parser.parse_args()
    import pytest  # the same pytest and SQLAlchemy everywhere
    import sqlalchemy

    common = [f"sqlalchemy=={sqlalchemy.__version__}", f"pytest=={pytest.__version__}"]
    queryguard = args.queryguard if args.queryguard != "." else str(ROOT)
    tools = {
        "off": _venv(args.work, "off", common, args.python),
        "queryguard": _venv(
            args.work, "queryguard", [*common, queryguard], args.python
        ),
        "queryspy": _venv(args.work, "queryspy", [*common, QUERYSPY], args.python),
        "querycount": _venv(
            args.work, "querycount", [*common, QUERYCOUNT], args.python
        ),
    }
    core = args.core or None
    micro = _micro(tools, args.rounds, args.statements, args.blocks, core)
    suite = _suite(tools, args.suite_rounds, args.tests, core)
    versions = {name: _versions(python) for name, python in tools.items()}
    base = versions["off"]
    print(
        f"\nPython {platform.python_version()}, SQLAlchemy {base['sqlalchemy']}, pytest {base['pytest']}, "
        f"{platform.machine()}; pytest-queryguard {versions['queryguard'].get('pytest-queryguard')}, "
        f"pytest-querycount {versions['querycount'].get('pytest-querycount')}, "
        f"queryspy {versions['queryspy'].get('queryspy')}\n"
    )
    names = ["queryguard", "querycount", "queryspy"]
    print("| | pytest-queryguard | pytest-querycount | queryspy |")
    print("|---|---|---|---|")
    row = [f"{(suite[n] - suite['off']) / suite['off']:+.1%}" for n in names]
    print(f"| {args.tests}-test suite, added time | " + " | ".join(row) + " |")
    for loop, label in (
        ("core", "Core `select`"),
        ("orm", "ORM `select`"),
        ("lazy", "Lazy load"),
    ):
        cells = [f"{micro[n][loop] - micro['off'][loop]:+.0f} us" for n in names]
        print(f"| {label}, added per statement | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
