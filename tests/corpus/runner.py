"""Run the corpus through pytest and read back what each tool said.

``write_session`` puts the generated session in a directory; ``run_subprocess``
starts pytest on it (any tool whose plugin is installed in ``python``), and the
test suite runs the same session in-process with ``pytester``. What comes back
per scenario is ``Result``: whether the test failed and, for pytest-queryguard,
the ``(kind, label)`` of every finding, apart for the findings that failed the
test and those that only warned (``cartesian-load`` by default).
"""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from . import kit, scenarios

ROOT = Path(__file__).resolve().parents[2]

# The kinds the default configuration can report. cartesian-load only warns
# by default; a warning is a finding that the user sees, so it is measured.
DEFAULT_KINDS = frozenset({kit.REL, kit.REPEAT, kit.COLUMN, kit.CARTESIAN})

# name -> (ini lines, the kinds that configuration adds to the default ones).
CONFIGS: dict[str, tuple[str, frozenset[str]]] = {
    "default": ("", frozenset()),
    "small-repeat": ("queryguard_small_repeat_threshold = 2\n", frozenset()),
    "write-loop": ("queryguard_write_loop_threshold = 2\n", frozenset({kit.WRITE})),
    "column-load-2": ("queryguard_column_load_threshold = 2\n", frozenset()),
    "unused-eager-load": (
        "queryguard_unused_eager_load = fail\n",
        frozenset({kit.UNUSED}),
    ),
    "unused-columns": (
        "queryguard_unused_columns = fail\n",
        frozenset({kit.COLUMNS}),
    ),
    # Counts the repeat, relationship and column-load rules per Session.
    "session-scope": ("queryguard_repeat_scope = session\n", frozenset()),
    # Raises at the lazy load that makes an N+1, inside the scenario's code.
    "strict": ("queryguard_strict = true\n", frozenset()),
    # Every opt-in rule at once: what a project that turned them all on sees.
    "all-opt-in": (
        (
            "queryguard_small_repeat_threshold = 2\n"
            "queryguard_write_loop_threshold = 2\n"
            "queryguard_column_load_threshold = 2\n"
            "queryguard_unused_eager_load = fail\n"
            "queryguard_unused_columns = fail\n"
        ),
        frozenset({kit.WRITE, kit.UNUSED, kit.COLUMNS}),
    ),
}


def scope(config: str) -> frozenset[str]:
    """The kinds a configuration is measured on."""
    return DEFAULT_KINDS | CONFIGS[config][1]


@dataclass(frozen=True)
class Scenario:
    name: str
    module: ModuleType

    @property
    def verdict(self) -> str:
        return str(self.module.VERDICT)

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(self.module.KINDS)

    @property
    def known(self) -> dict[str, str]:
        return dict(getattr(self.module, "KNOWN", {}))

    @property
    def doc(self) -> str:
        return (self.module.__doc__ or "").strip()


SKIPPED: list[tuple[str, str]] = []


def load() -> list[Scenario]:
    """Every scenario whose imports work, in name order.

    A scenario that needs a package that is not installed (SQLModel, which pins
    SQLAlchemy below 2.1) is left out and listed in ``SKIPPED``.
    """
    found = []
    SKIPPED.clear()
    for info in sorted(pkgutil.iter_modules(scenarios.__path__), key=lambda i: i.name):
        try:
            module = importlib.import_module(f"{scenarios.__name__}.{info.name}")
        except ModuleNotFoundError as exc:
            SKIPPED.append((info.name, str(exc.name)))
            continue
        found.append(Scenario(info.name, module))
    return found


@dataclass
class Result:
    failed: bool
    # the findings that failed the test, and those that only warned
    findings: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(kind for kind, _ in (*self.findings, *self.warnings))


CONFTEST = """
import json, os
import pytest

OUT = {}
CURRENT = [None]
_patched = None

# id(finding) -> whether it fails the test, noted by the plugin's _fails()
FAILS = {}

try:
    if os.environ.get("CORPUS_TOOL", "queryguard") == "queryguard":
        from pytest_queryguard import plugin as _plugin
        _patched = (_plugin, _plugin.format_findings, _plugin._fails)
except ImportError:
    pass


def _fails(settings, finding):
    verdict = _patched[2](settings, finding)
    FAILS[id(finding)] = verdict
    return verdict


def _record(findings, **kwargs):
    for f in findings:
        entry = OUT.setdefault(CURRENT[0], {"failed": False, "findings": []})
        key = "findings" if FAILS.get(id(f), True) else "warnings"
        entry.setdefault(key, []).append([f.kind, f.label])
    return _patched[1](findings, **kwargs)


def pytest_collection_modifyitems(items):
    # pytest-querycount checks only tests that carry its marker.
    if os.environ.get("CORPUS_TOOL") == "querycount":
        for item in items:
            item.add_marker(pytest.mark.no_n_plus_one)


def pytest_configure(config):
    if _patched is not None:
        _patched[0].format_findings = _record
        _patched[0]._fails = _fails


def pytest_unconfigure(config):
    if _patched is not None:
        _patched[0].format_findings = _patched[1]
        _patched[0]._fails = _patched[2]
    with open(os.environ["CORPUS_OUT"], "w") as handle:
        json.dump(OUT, handle)


@pytest.fixture(autouse=True)
def _current(request):
    CURRENT[0] = request.node.nodeid.split("[", 1)[-1].rstrip("]")
    FAILS.clear()
    OUT.setdefault(CURRENT[0], {"failed": False, "findings": []})
    yield


def pytest_runtest_logreport(report):
    if report.failed and report.when in ("setup", "call"):
        name = report.nodeid.split("[", 1)[-1].rstrip("]")
        OUT.setdefault(name, {"failed": False, "findings": []})["failed"] = True
"""


def write_session(directory: Path, ini: str, names: list[str]) -> Path:
    """Write conftest, ini and the test module; return the output JSON path."""
    (directory / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    (directory / "pytest.ini").write_text(f"[pytest]\n{ini}", encoding="utf-8")
    body = (Path(__file__).with_name("session_body.py")).read_text(encoding="utf-8")
    (directory / "test_session.py").write_text(body, encoding="utf-8")
    return directory / "corpus_out.json"


def session_env(names: list[str], out: Path, tool: str) -> dict[str, str]:
    return {
        "CORPUS_SCENARIOS": ",".join(names),
        "CORPUS_OUT": str(out),
        "CORPUS_TOOL": tool,
    }


def read_results(out: Path) -> dict[str, Result]:
    raw = json.loads(out.read_text(encoding="utf-8"))
    return {
        name: Result(
            entry["failed"],
            [(k, v) for k, v in entry["findings"]],
            [(k, v) for k, v in entry.get("warnings", [])],
        )
        for name, entry in raw.items()
        if name != "None"
    }


def run_subprocess(
    directory: Path,
    names: list[str],
    *,
    ini: str = "",
    args: tuple[str, ...] = (),
    python: str = sys.executable,
    tool: str = "queryguard",
    extra_env: dict[str, str] | None = None,
) -> dict[str, Result]:
    """Run the session with ``python -m pytest``; ``args`` are extra pytest flags."""
    directory.mkdir(parents=True, exist_ok=True)
    out = write_session(directory, ini, names)
    env = {
        **os.environ,
        **session_env(names, out, tool),
        "PYTHONPATH": f"{ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        **(extra_env or {}),
    }
    proc = subprocess.run(
        [python, "-m", "pytest", "-p", "no:cacheprovider", "-W", "ignore", "-q", *args],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if not out.exists():
        raise RuntimeError(
            f"the session wrote no results:\n{proc.stdout}\n{proc.stderr}"
        )
    return read_results(out)
