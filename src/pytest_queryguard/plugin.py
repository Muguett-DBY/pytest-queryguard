from __future__ import annotations

import contextlib
import glob
import importlib.metadata
import os
import shlex
import warnings as std_warnings
from collections.abc import Callable, Generator, Sequence
from dataclasses import replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, cast

import pytest

from . import baseline as baselines
from . import budgets as budget_files
from . import cartesian, catalog, eager, hypopg, indexes, sarif, testjson
from . import groups as finding_groups
from .api import NPlusOneDetected, QueryGuardFixture
from .baseline import BaselineError, Identity, sorted_identities
from .callsite import trim_to_user_code
from .collector import QueryCollector, pop, push
from .detectors import (
    DEFAULT_COLUMN_LOAD_THRESHOLD,
    SCOPE_SESSION,
    SCOPE_TEST,
    SCOPES,
    RelationshipFinding,
    detect,
)
from .events import CallSite, RelationshipLoadEvent
from .findings import (
    KIND_CARTESIAN_LOAD,
    KIND_MISSING_INDEX,
    KIND_RELATIONSHIP,
    KIND_UNUSED_EAGER_LOAD,
    Finding,
)
from .ignores import IgnoreRule, apply_rules, check_kind, parse_rules
from .indexes import select_findings
from .listeners import (
    install,
    install_index_check,
    internal_error,
    internal_failure,
    record_error,
    swap_error,
)
from .messages import (
    format_budget,
    format_failure,
    format_findings,
    format_index_findings,
    format_index_notes,
    format_stale_baseline,
    format_stale_ignores,
    format_strict,
    format_time_budget,
    format_truncation,
    make_finding,
    make_findings,
    make_index_finding,
    swap_root,
)
from .warnings import QueryGuardInternalWarning, QueryGuardWarning, warn_internal

_MODES = ("fail", "warn", "off")
_EAGER_MODES = ("off", "warn", "fail")
_eager_enabled = pytest.StashKey[bool]()
_cartesian_enabled = pytest.StashKey[bool]()
_internal_reported = pytest.StashKey[bool]()
# Set on a test item once its function starts: a queryguard fixture requested
# later (request.getfixturevalue in the test) starts counting at once.
_call_started = pytest.StashKey[bool]()
_failure_summarised = pytest.StashKey[bool]()
_outer_error = pytest.StashKey[BaseException | None]()
_outer_stopped = pytest.StashKey[str | None]()
_outer_catalog_error = pytest.StashKey[str | None]()
_outer_no_plpgsql = pytest.StashKey[indexes.EngineSet]()
_outer_root = pytest.StashKey[Path | None]()
_settings = pytest.StashKey["_Settings"]()
_report = pytest.StashKey[list[tuple[str, int]]]()
_index_notes = pytest.StashKey["_IndexNotes"]()
_REPORT_SIZE = 10
# Statements and relationship loads stored per test; generous, a test that
# needs more is far past any N+1 threshold already.
DEFAULT_MAX_EVENTS = 10_000


class _Settings:
    def __init__(
        self,
        mode: str,
        relationship: int,
        repeat: int,
        index_check: bool = False,
        ignore_tables: tuple[str, ...] = (),
        max_events: int | None = DEFAULT_MAX_EVENTS,
        ignore: tuple[IgnoreRule, ...] = (),
        column_load: int = DEFAULT_COLUMN_LOAD_THRESHOLD,
        small_repeat: int | None = None,
        fail_on: tuple[str, ...] = (),
        write_loop: int | None = None,
        unused_eager: str = "off",
        baseline_path: Path | None = None,
        update_baseline: bool = False,
        baseline: frozenset[Identity] | None = None,
        sarif_path: Path | None = None,
        index_advisor: bool = True,
        hypopg: str = "auto",
        repeat_scope: str = SCOPE_TEST,
    ) -> None:
        self.mode = mode
        self.repeat_scope = repeat_scope
        self.relationship = relationship
        self.repeat = repeat
        self.index_check = index_check
        self.ignore_tables = ignore_tables
        self.max_events = max_events
        self.ignore = ignore
        self.column_load = column_load
        self.small_repeat = small_repeat
        self.fail_on = fail_on
        self.write_loop = write_loop
        self.unused_eager = unused_eager
        self.index_advisor = index_advisor
        self.hypopg = hypopg
        self.baseline_path = baseline_path
        self.sarif_path = sarif_path
        self.rootpath: Path | None = None  # for the SARIF location prefix
        # findings that count (not ignored, not baselined), for the SARIF file
        self.reported: sarif.Collected = {}
        # the same counted finding in several tests, for the terminal summary
        self.groups: finding_groups.Groups = {}
        self.cartesian = "warn"  # queryguard_cartesian_load
        self.strict = False  # queryguard_strict
        # budgets: queryguard_max_queries, the budget file and its update
        self.default_budget: int | None = None
        self.budgets_path: Path | None = None
        self.budgets: dict[str, int] = {}
        self.update_budgets = False
        self.budgets_written: str | None = None
        self.budgets_failed: str | None = None
        self.budgets_previous_error: str | None = None
        # for the update: the node ids collected and those that failed
        self.collected: set[str] = set()
        self.failed: set[str] = set()
        # per-test counts and findings for --queryguard-json
        self.json_path: Path | None = None
        self.tests: testjson.Tests = {}
        self.json_incomplete = False
        self.json_result: str | None = None
        self.json_failed: str | None = None
        self.sarif_result: str | None = None
        self.sarif_failed: str | None = None
        self.update_baseline = update_baseline
        # None: no baseline in use (or one that is about to be rewritten, which
        # then starts empty and is compared with what the run finds)
        self.baseline = baseline
        # identities of the findings the run made (after ignores), baselined or not
        self.seen: set[Identity] = set()
        # (rootdir-relative file, function) of every test that ran its call
        # phase, and whether this session deselected any test (-k, -m, --lf,
        # --deselect); both feed a partial baseline update
        self.executed: set[tuple[str | None, str]] = set()
        self.deselected = False
        # the run is incomplete: a worker crashed or stopped measuring
        self.baseline_incomplete = False
        # a collection error was reported (by this process or an xdist worker)
        self.collection_failed = False
        self.baseline_previous_error: str | None = None
        self.baseline_written: str | None = None
        self.baseline_failed: str | None = None
        # what the ignores did, for the stale-ignore warning in the summary
        self.ignore_used: set[IgnoreRule] = set()
        self.stale_markers: list[tuple[str, IgnoreRule]] = []

    @property
    def baselining(self) -> bool:
        return self.baseline_path is not None

    @property
    def counting(self) -> bool:
        """Whether each test's counts are kept: for the JSON or the budget update."""
        return self.json_path is not None or self.update_budgets

    def fails(self, kind: str) -> bool:
        """True when a finding of this kind fails the test; else it only warns."""
        if self.mode != "fail":
            return False
        return not self.fail_on or any(fnmatchcase(kind, p) for p in self.fail_on)


class _IndexNotes:
    """What the index check could not do, summed up over the session."""

    def __init__(self) -> None:
        self.notices: dict[str, None] = {}  # ordered set
        self.errors = 0
        self.partial_skipped: list[tuple[str, ...]] = []
        self.first_error: tuple[str, str] | None = None


# pytest 9 reads native [tool.pytest] TOML, where thresholds are real integers
# and a string-typed option would crash; older versions only know strings.
_NATIVE_INT = pytest.version_tuple >= (9,)
_INT_TYPE: dict[str, Any] = {"type": "int"} if _NATIVE_INT else {}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("queryguard")
    group.addoption(
        "--queryguard-mode",
        dest="queryguard_mode",
        default=None,
        help="N+1 handling: fail, warn or off (overrides queryguard_mode).",
    )
    group.addoption(
        "--queryguard-strict",
        dest="queryguard_strict",
        action="store_true",
        default=None,
        help="Raise at the lazy load that makes an N+1 (by default the second "
        "parent from one query line), with the test's traceback (overrides "
        "queryguard_strict).",
    )
    group.addoption(
        "--queryguard-report",
        dest="queryguard_report",
        action="store_true",
        default=False,
        help="Print the tests with the most queries at the end of the session.",
    )
    group.addoption(
        "--queryguard-index-check",
        dest="queryguard_index_check",
        action="store_const",
        const=True,
        default=None,
        help="Check statements for a missing index (PostgreSQL 16 or newer, SQLite, MySQL or MariaDB); "
        "overrides queryguard_index_check.",
    )
    group.addoption(
        "--no-queryguard-index-check",
        dest="queryguard_index_check",
        action="store_const",
        const=False,
        help="Turn the missing-index check off for this run.",
    )
    group.addoption(
        "--queryguard-baseline",
        dest="queryguard_baseline",
        default=None,
        metavar="PATH",
        help="Baseline file: findings listed in it do not fail or warn "
        "(overrides queryguard_baseline).",
    )
    group.addoption(
        "--queryguard-budgets",
        dest="queryguard_budgets",
        default=None,
        metavar="PATH",
        help="Budget file: each test listed in it fails above its number of "
        "queries (overrides queryguard_budgets).",
    )
    group.addoption(
        "--queryguard-update-budgets",
        dest="queryguard_update_budgets",
        action="store_true",
        default=False,
        help="Write each test's query count of this run to the budget file. "
        "On a partial run the entries of tests that did not run are kept.",
    )
    group.addoption(
        "--queryguard-update-baseline",
        dest="queryguard_update_baseline",
        action="store_true",
        default=False,
        help="Write the findings of this run to the baseline file instead of "
        "reporting them. On a partial run (-k, -m, --lf, a path) only the "
        "entries of tests that ran are replaced.",
    )
    group.addoption(
        "--queryguard-sarif",
        dest="queryguard_sarif",
        default=None,
        metavar="PATH",
        help="Write the findings of this run to PATH as SARIF 2.1.0 (GitHub "
        "code scanning). Findings in the baseline are left out.",
    )
    group.addoption(
        "--queryguard-json",
        dest="queryguard_json",
        default=None,
        metavar="PATH",
        help="Write each test's statement counts and findings to PATH as JSON, "
        "to compare runs (a pull request against its base branch).",
    )
    parser.addini("queryguard_mode", "fail, warn or off", default="fail")
    parser.addini(
        "queryguard_relationship_threshold",
        "Lazy loads of one relationship treated as N+1 (integer >= 2)",
        default=2 if _NATIVE_INT else "2",
        **_INT_TYPE,
    )
    parser.addini(
        "queryguard_repeat_threshold",
        "Executions of one statement treated as N+1 (integer >= 2)",
        default=5 if _NATIVE_INT else "5",
        **_INT_TYPE,
    )
    parser.addini(
        "queryguard_small_repeat_threshold",
        "Executions (below queryguard_repeat_threshold) of one statement from one "
        "source line, each with different parameters, treated as N+1; 0 turns "
        "this off (integer >= 2, or 0)",
        default=0 if _NATIVE_INT else "0",
        **_INT_TYPE,
    )
    parser.addini(
        "queryguard_write_loop_threshold",
        "Row-by-row INSERT, UPDATE or DELETE statements of one shape from one "
        "source line treated as a write loop; 0 turns the check off (integer "
        ">= 2, or 0)",
        default=0 if _NATIVE_INT else "0",
        **_INT_TYPE,
    )
    parser.addini(
        "queryguard_repeat_scope",
        "What the repeat, relationship, column-load and write-loop rules count "
        "together: test (everything one test runs) or session (each SQLAlchemy "
        "Session or connection checkout on its own, for tests that make several "
        "requests)",
        default=SCOPE_TEST,
    )
    parser.addini(
        "queryguard_fail_on",
        "Finding kinds that fail a test in fail mode, one per line (fnmatch "
        "patterns); the other kinds only warn. Empty: all kinds fail",
        type="linelist",
        default=[],
    )
    parser.addini(
        "queryguard_column_load_threshold",
        "Instances of one class that must load deferred or expired columns on "
        "their own to count as N+1 (integer >= 2)",
        default=DEFAULT_COLUMN_LOAD_THRESHOLD
        if _NATIVE_INT
        else str(DEFAULT_COLUMN_LOAD_THRESHOLD),
        **_INT_TYPE,
    )
    parser.addini(
        "queryguard_max_events",
        "Statements stored per test for the report (integer >= 1, or 0 for no limit); "
        "beyond it they are only counted",
        default=DEFAULT_MAX_EVENTS if _NATIVE_INT else str(DEFAULT_MAX_EVENTS),
        **_INT_TYPE,
    )
    parser.addini(
        "queryguard_ignore",
        "Findings to leave out of reports, one per line: "
        "KIND LABEL [file=GLOB] [function=GLOB] (fnmatch patterns)",
        type="linelist",
        default=[],
    )
    parser.addini(
        "queryguard_strict",
        "Raise NPlusOneDetected at the lazy load that makes an N+1 (the parent "
        "that reaches queryguard_relationship_threshold among those loaded at one "
        "line), instead of reporting it when the test ends",
        type="bool",
        default=False,
    )
    parser.addini(
        "queryguard_cartesian_load",
        "One SELECT that joins two collections side by side (joinedload of both), "
        "whose rows grow as their product: warn (default), fail or off",
        default="warn",
    )
    parser.addini(
        "queryguard_unused_eager_load",
        "EXPERIMENTAL: report relationships a test loads with selectinload, "
        "joinedload, subqueryload or immediateload and never reads: off "
        "(default), warn or fail",
        default="off",
    )
    parser.addini(
        "queryguard_budgets",
        "Budget file (relative to the rootdir): each test listed in it fails above "
        "its number of queries, unless it has a max_queries marker",
        default="",
    )
    parser.addini(
        "queryguard_max_queries",
        "Default query budget of every test that has no max_queries marker and no "
        "entry in the budget file (integer >= 0; empty: none)",
        default="",
    )
    parser.addini(
        "queryguard_baseline",
        "Baseline file (relative to the rootdir): findings listed in it do not "
        "fail or warn",
        default="",
    )
    parser.addini(
        "queryguard_index_check",
        "Check statements for a missing index (PostgreSQL 16 or newer, SQLite, MySQL or MariaDB)",
        type="bool",
        default=False,
    )
    parser.addini(
        "queryguard_index_advisor",
        "Suggest a CREATE INDEX for each missing-index finding (used with the "
        "index check)",
        type="bool",
        default=True,
    )
    parser.addini(
        "queryguard_hypopg",
        "Verify suggested indexes with the hypopg extension: auto (when "
        "installed), on (also create it inside the check's savepoint) or off",
        default="auto",
    )
    parser.addini(
        "queryguard_index_ignore_tables",
        "Tables (names or fnmatch patterns) the missing-index check ignores",
        type="linelist",
        default=[],
    )


def _threshold(config: pytest.Config, name: str) -> int:
    detail = ""
    try:
        raw: Any = config.getini(name)
    except (TypeError, ValueError) as exc:
        raw, detail = None, f": {exc}"
    try:
        value = 0 if isinstance(raw, bool) else int(raw)
    except (TypeError, ValueError):
        value = 0
    if value < 2:
        shown = detail or f", got {raw!r}"
        raise pytest.UsageError(f"{name} must be an integer >= 2{shown}")
    return value


def _small_repeat(config: pytest.Config, repeat: int) -> int | None:
    name = "queryguard_small_repeat_threshold"
    try:
        raw: Any = config.getini(name)
    except (TypeError, ValueError) as exc:
        raise pytest.UsageError(f"{name} must be 0 or an integer >= 2: {exc}") from None
    try:
        value = -1 if isinstance(raw, bool) else int(raw)
    except (TypeError, ValueError):
        value = -1
    if value == 0:
        return None
    if value < 2:
        raise pytest.UsageError(f"{name} must be 0 or an integer >= 2, got {raw!r}")
    if value >= repeat:
        raise pytest.UsageError(
            f"{name} must be lower than queryguard_repeat_threshold ({repeat}), "
            f"got {value}"
        )
    return value


def _choice(
    config: pytest.Config,
    name: str,
    choices: tuple[str, ...],
    *,
    flag: str | None = None,
) -> str:
    """Read an option with a fixed set of values; case and edge spaces are ignored.

    ``flag`` is the command-line option that overrides the ini option, if any.
    Every enum-valued option goes through here so that they agree on case.
    """
    source = name
    raw: Any = None
    if flag is not None:
        raw = config.getoption(name)
        if raw is not None:
            source = flag
    if raw is None:
        try:
            raw = config.getini(name)
        except (TypeError, ValueError) as exc:
            # pytest 9 rejects a non-string value from native TOML on its own.
            raise pytest.UsageError(
                f"{name} must be one of {', '.join(choices)}: {exc}"
            ) from None
    value = str(raw).strip().lower()
    if value not in choices:
        raise pytest.UsageError(
            f"{source} must be one of {', '.join(choices)}, got {raw!r}"
        )
    return value


def _repeat_scope(config: pytest.Config) -> str:
    return _choice(config, "queryguard_repeat_scope", SCOPES)


def _write_loop(config: pytest.Config) -> int | None:
    name = "queryguard_write_loop_threshold"
    try:
        raw: Any = config.getini(name)
    except (TypeError, ValueError) as exc:
        raise pytest.UsageError(f"{name} must be 0 or an integer >= 2: {exc}") from None
    try:
        value = -1 if isinstance(raw, bool) else int(raw)
    except (TypeError, ValueError):
        value = -1
    if value == 0:
        return None
    if value < 2:
        raise pytest.UsageError(f"{name} must be 0 or an integer >= 2, got {raw!r}")
    return value


def _fail_on(config: pytest.Config) -> tuple[str, ...]:
    name = "queryguard_fail_on"
    try:
        raw: Any = config.getini(name)
    except (TypeError, ValueError) as exc:
        raise pytest.UsageError(f"{name} must be a list of kinds: {exc}") from None
    if not isinstance(raw, list) or not all(isinstance(k, str) for k in raw):
        raise pytest.UsageError(f"{name} must be a list of kinds, got {raw!r}")
    kinds = tuple(k.strip() for k in raw if k.strip() and not k.strip().startswith("#"))
    for kind in kinds:
        try:
            check_kind(kind)
        except ValueError as exc:
            raise pytest.UsageError(f"{name}: {exc}") from None
    return kinds


def _max_events(config: pytest.Config) -> int | None:
    name = "queryguard_max_events"
    try:
        raw: Any = config.getini(name)
    except (TypeError, ValueError) as exc:
        raise pytest.UsageError(f"{name} must be an integer >= 0: {exc}") from None
    try:
        value = -1 if isinstance(raw, bool) else int(raw)
    except (TypeError, ValueError):
        value = -1
    if value < 0:
        raise pytest.UsageError(f"{name} must be an integer >= 0, got {raw!r}")
    return value or None


def _ignore_rules(config: pytest.Config) -> tuple[IgnoreRule, ...]:
    name = "queryguard_ignore"
    try:
        raw: Any = config.getini(name)
    except (TypeError, ValueError) as exc:
        raise pytest.UsageError(f"{name} must be a list of patterns: {exc}") from None
    if not isinstance(raw, list) or not all(isinstance(line, str) for line in raw):
        raise pytest.UsageError(f"{name} must be a list of patterns, got {raw!r}")
    try:
        return parse_rules(raw)
    except ValueError as exc:
        raise pytest.UsageError(f"{name}: {exc}") from None


def _strict(config: pytest.Config) -> bool:
    if config.getoption("queryguard_strict"):
        return True
    name = "queryguard_strict"
    try:
        raw: Any = config.getini(name)
    except (TypeError, ValueError) as exc:
        raise pytest.UsageError(f"{name} must be true or false: {exc}") from None
    if not isinstance(raw, bool):
        raise pytest.UsageError(f"{name} must be true or false, got {raw!r}")
    return raw


def _strict_hook(
    item: pytest.Item, settings: _Settings
) -> (
    Callable[[RelationshipLoadEvent, CallSite | None, int], BaseException | None] | None
):
    """The strict-mode check of this test, or None when it cannot raise.

    It raises only what would fail the test anyway: an N+1 that no
    allow_nplusone marker, queryguard_ignore pattern or baseline entry covers.
    """
    if not settings.strict or not settings.fails(KIND_RELATIONSHIP):
        return None
    if settings.update_baseline:
        return None  # the update records the N+1s; raising would stop the loop
    ignore_all, marker_rules = _nplusone_ignores(item)
    if ignore_all:
        return None
    rules = (*settings.ignore, *marker_rules)
    root = item.config.rootpath

    def check(
        load: RelationshipLoadEvent, first: CallSite | None, parents: int
    ) -> BaseException | None:
        # the finding the report would make: located at the group's first lazy
        # load, so ignore patterns and baseline entries match it the same way
        parent = load.parent
        finding = make_finding(
            RelationshipFinding(
                load.relationship,
                parents,
                first,
                None,
                scalar=load.scalar,
                parent_site=None if parent is None else parent.callsite,
                parent_root=None if parent is None else parent.root,
                parent_path=() if parent is None else parent.path,
            ),
            cwd=root,
        )
        if rules and not apply_rules([finding], rules)[0]:
            return None
        if settings.baseline and not baselines.split([finding], settings.baseline)[0]:
            return None
        error = NPlusOneDetected(format_strict(finding, parents))
        error.queryguard_strict = True  # type: ignore[attr-defined]
        return error

    return check


def _cartesian_mode(config: pytest.Config) -> str:
    return _choice(config, "queryguard_cartesian_load", _EAGER_MODES)


def _unused_eager(config: pytest.Config) -> str:
    return _choice(config, "queryguard_unused_eager_load", _EAGER_MODES)


def _index_check(config: pytest.Config) -> bool:
    name = "queryguard_index_check"
    try:
        raw: Any = config.getini(name)
    except (TypeError, ValueError) as exc:
        raise pytest.UsageError(f"{name} must be true or false: {exc}") from None
    if not isinstance(raw, bool):
        raise pytest.UsageError(f"{name} must be true or false, got {raw!r}")
    cli: bool | None = config.getoption("queryguard_index_check")
    return raw if cli is None else cli


def _index_advisor(config: pytest.Config) -> bool:
    name = "queryguard_index_advisor"
    try:
        raw: Any = config.getini(name)
    except (TypeError, ValueError) as exc:
        raise pytest.UsageError(f"{name} must be true or false: {exc}") from None
    if not isinstance(raw, bool):
        raise pytest.UsageError(f"{name} must be true or false, got {raw!r}")
    return raw


def _hypopg(config: pytest.Config) -> str:
    return _choice(config, "queryguard_hypopg", tuple(hypopg.MODES))


def _ignore_tables(config: pytest.Config) -> tuple[str, ...]:
    name = "queryguard_index_ignore_tables"
    try:
        raw: Any = config.getini(name)
    except (TypeError, ValueError) as exc:
        raise pytest.UsageError(
            f"{name} must be a list of table names: {exc}"
        ) from None
    if not isinstance(raw, list) or not all(isinstance(t, str) and t for t in raw):
        raise pytest.UsageError(f"{name} must be a list of table names, got {raw!r}")
    return tuple(raw)


def _baseline_path(config: pytest.Config) -> Path | None:
    cli: str | None = config.getoption("queryguard_baseline")
    if cli is not None:
        if not cli.strip():
            raise pytest.UsageError("--queryguard-baseline needs a path")
        return Path(config.invocation_params.dir, cli.strip())
    name = "queryguard_baseline"
    try:
        raw: Any = config.getini(name)
    except (TypeError, ValueError) as exc:
        raise pytest.UsageError(f"{name} must be a path: {exc}") from None
    if not isinstance(raw, str):
        raise pytest.UsageError(f"{name} must be a path, got {raw!r}")
    return Path(config.rootpath, raw.strip()) if raw.strip() else None


def _budgets_path(config: pytest.Config) -> Path | None:
    path = _output_path(config, "queryguard_budgets", "--queryguard-budgets")
    if path is not None:
        return path
    name = "queryguard_budgets"
    try:
        raw: Any = config.getini(name)
    except (TypeError, ValueError) as exc:
        raise pytest.UsageError(f"{name} must be a path: {exc}") from None
    if not isinstance(raw, str):
        raise pytest.UsageError(f"{name} must be a path, got {raw!r}")
    return Path(config.rootpath, raw.strip()) if raw.strip() else None


def _default_budget(config: pytest.Config) -> int | None:
    name = "queryguard_max_queries"
    try:
        raw: Any = config.getini(name)
    except (TypeError, ValueError) as exc:
        raise pytest.UsageError(f"{name} must be an integer >= 0: {exc}") from None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        value = -1 if isinstance(raw, bool) else int(raw)
    except (TypeError, ValueError):
        value = -1
    if value < 0:
        raise pytest.UsageError(f"{name} must be an integer >= 0, got {raw!r}")
    return value


def _output_path(config: pytest.Config, dest: str, flag: str) -> Path | None:
    """The path of an output file option, relative to the invocation directory."""
    raw: str | None = config.getoption(dest)
    if raw is None:
        return None
    if not raw.strip():
        raise pytest.UsageError(f"{flag} needs a path")
    path = Path(config.invocation_params.dir, raw.strip())
    setattr(config.option, dest, str(path))  # workers get the resolved path
    return path


def _sarif_path(config: pytest.Config) -> Path | None:
    return _output_path(config, "queryguard_sarif", "--queryguard-sarif")


def _json_path(config: pytest.Config) -> Path | None:
    return _output_path(config, "queryguard_json", "--queryguard-json")


def _read_settings(config: pytest.Config) -> _Settings:
    mode = _choice(config, "queryguard_mode", _MODES, flag="--queryguard-mode")
    repeat = _threshold(config, "queryguard_repeat_threshold")
    baseline_path = _baseline_path(config)
    update = bool(config.getoption("queryguard_update_baseline"))
    baseline: frozenset[Identity] | None = None
    previous_error: str | None = None
    if update and baseline_path is None:
        raise pytest.UsageError(
            "--queryguard-update-baseline needs a baseline file: pass "
            "--queryguard-baseline=PATH or set queryguard_baseline"
        )
    if update and mode == "off":
        raise pytest.UsageError(
            "--queryguard-update-baseline cannot run with measurement off "
            "(the queryguard mode is off)"
        )
    if baseline_path is not None:
        try:
            baseline = baselines.load(baseline_path)
        except BaselineError as exc:
            if not update:
                raise pytest.UsageError(f"queryguard baseline: {exc}") from None
            # an update replaces a missing or unreadable file
            baseline = frozenset()
            previous_error = str(exc) if baseline_path.exists() else None
        # the CLI path is relative to the invocation directory; workers resolve
        # the same file, whatever their own working directory is
        config.option.queryguard_baseline = str(baseline_path)
    settings = _Settings(
        mode,
        _threshold(config, "queryguard_relationship_threshold"),
        repeat,
        _index_check(config),
        _ignore_tables(config),
        _max_events(config),
        _ignore_rules(config),
        _threshold(config, "queryguard_column_load_threshold"),
        _small_repeat(config, repeat),
        _fail_on(config),
        _write_loop(config),
        _unused_eager(config),
        baseline_path,
        update,
        baseline,
        _sarif_path(config),
        _index_advisor(config),
        _hypopg(config),
        _repeat_scope(config),
    )
    settings.baseline_previous_error = previous_error
    settings.rootpath = config.rootpath
    settings.json_path = _json_path(config)
    settings.cartesian = _cartesian_mode(config)
    settings.strict = _strict(config)
    settings.default_budget = _default_budget(config)
    settings.budgets_path = _budgets_path(config)
    settings.update_budgets = bool(config.getoption("queryguard_update_budgets"))
    if settings.update_budgets and mode == "off":
        raise pytest.UsageError(
            "--queryguard-update-budgets cannot run with measurement off "
            "(the queryguard mode is off)"
        )
    if settings.update_budgets and settings.budgets_path is None:
        raise pytest.UsageError(
            "--queryguard-update-budgets needs a budget file: pass "
            "--queryguard-budgets=PATH or set queryguard_budgets"
        )
    if settings.budgets_path is not None:
        try:
            settings.budgets = budget_files.load(settings.budgets_path)
        except budget_files.BudgetFileError as exc:
            if not settings.update_budgets:
                raise pytest.UsageError(f"queryguard budgets: {exc}") from None
            # an update replaces a missing file; a broken one is reported
            settings.budgets = {}
            if settings.budgets_path.exists():
                settings.budgets_previous_error = str(exc)
    return settings


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "max_queries(limit): fail the test if it runs more than limit SQL statements.",
    )
    config.addinivalue_line(
        "markers",
        "max_db_time(seconds): fail the test if its SQL statements take more "
        "than that many seconds in total.",
    )
    config.addinivalue_line(
        "markers",
        "allow_nplusone(*labels, kind=..., file=..., function=...): do not report "
        "N+1 query findings for this test; with arguments, only the findings "
        "they match (fnmatch patterns).",
    )
    config.addinivalue_line(
        "markers",
        "allow_missing_index(*tables): skip the missing-index check for this "
        "test, or only for the given tables.",
    )
    config.stash[_outer_error] = swap_error(None)
    try:
        _configure(config)
    except pytest.UsageError:
        raise
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        # Measurement stays off; the terminal summary says so.
        record_error(exc, "pytest_configure")


def _configure(config: pytest.Config) -> None:
    config.stash[_outer_stopped] = indexes.swap_stopped(None)
    config.stash[_outer_catalog_error] = indexes.swap_catalog_error(None)
    config.stash[_outer_no_plpgsql] = indexes.swap_no_plpgsql(indexes.EngineSet())
    config.stash[_outer_root] = swap_root(config.rootpath)
    # A nested session (pytester) may change the database; start afresh.
    catalog.reset()
    settings = _read_settings(config)
    config.stash[_settings] = settings
    _active.append(settings)
    if (
        settings.baselining
        or settings.sarif_path
        or settings.json_path
        or settings.update_budgets
    ):
        config.pluginmanager.register(
            _CollectionErrors(settings), "queryguard-collection-errors"
        )
    if config.getoption("queryguard_report"):
        config.stash[_report] = []
    if settings.mode != "off":
        install()
        if settings.index_check:
            install_index_check()
        if settings.unused_eager != "off":
            config.stash[_eager_enabled] = True  # set first: undone even if it fails
            eager.enable()
        if settings.cartesian != "off":
            config.stash[_cartesian_enabled] = True
            cartesian.enable()


class _CollectionErrors:
    """Notes a collection error, also one that a pytest-xdist worker reports."""

    def __init__(self, settings: _Settings) -> None:
        self._settings = settings

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        try:
            if report.failed:
                self._settings.collection_failed = True
        except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
            record_error(exc, "collection report")


# The settings of the configured sessions, innermost last (pytester nests one
# in the process): for hooks that get no config, such as pytest_runtest_logreport.
_active: list[_Settings] = []


def pytest_unconfigure(config: pytest.Config) -> None:
    stash = config.stash
    settings = stash.get(_settings, None)
    if settings is not None and settings in _active:
        _active.remove(settings)
    # Every step runs, so a nested session (pytester) always gives the outer
    # session its state back.
    steps: tuple[Callable[[], object], ...] = (
        lambda: _disable_eager(config),
        lambda: _disable_cartesian(config),
        lambda: swap_error(stash.get(_outer_error, None)),
        lambda: indexes.swap_stopped(stash.get(_outer_stopped, None)),
        lambda: indexes.swap_catalog_error(stash.get(_outer_catalog_error, None)),
        lambda: indexes.swap_no_plpgsql(
            stash.get(_outer_no_plpgsql, indexes.EngineSet())
        ),
        lambda: swap_root(stash.get(_outer_root, None)),
        catalog.reset,
    )
    for step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
            # After the summary: printed by Python's default warning handler.
            warn_internal(
                QueryGuardInternalWarning(
                    f"pytest-queryguard could not clean up after the session: {exc!r}"
                )
            )


def _disable_cartesian(config: pytest.Config) -> None:
    if config.stash.get(_cartesian_enabled, False):
        config.stash[_cartesian_enabled] = False
        cartesian.disable()


def _disable_eager(config: pytest.Config) -> None:
    if config.stash.get(_eager_enabled, False):
        config.stash[_eager_enabled] = False
        eager.disable()


def _report_internal_error(config: pytest.Config) -> None:
    error = internal_error()
    if error is None or config.stash.get(_internal_reported, False):
        return
    config.stash[_internal_reported] = True
    failure = internal_failure()
    where = f" ({failure.source})" if failure is not None else ""
    warn_internal(
        QueryGuardInternalWarning(
            f"pytest-queryguard disabled itself after an internal error: {error!r}"
            f"{where}"
        )
    )


def _write_internal_failure(terminalreporter: pytest.TerminalReporter) -> None:
    """Tell the user, once, that measurement stopped, where and why."""
    config = terminalreporter.config
    failure = internal_failure()
    if failure is None or config.stash.get(_failure_summarised, False):
        return
    config.stash[_failure_summarised] = True
    terminalreporter.write_sep("=", "queryguard: measurement stopped", yellow=True)
    for line in format_failure(failure):
        terminalreporter.write_line(line)


def _nplusone_ignores(item: pytest.Item) -> tuple[bool, tuple[IgnoreRule, ...]]:
    """(ignore every N+1 finding, rules) from allow_nplusone markers."""
    ignore_all = False
    rules: list[IgnoreRule] = []
    for marker in item.iter_markers("allow_nplusone"):
        patterns = marker.args
        extra = set(marker.kwargs) - {"kind", "file", "function"}
        values = [*patterns, *marker.kwargs.values()]
        if extra or not all(isinstance(v, str) and v for v in values):
            pytest.fail(
                f"allow_nplusone on {item.nodeid} takes label patterns and the "
                "keywords kind, file and function, all as strings, got "
                f"{marker.args!r} {marker.kwargs!r}",
                pytrace=False,
            )
        if not marker.args and not marker.kwargs:
            ignore_all = True
            continue
        kind = marker.kwargs.get("kind", "*")
        try:
            check_kind(kind)
        except ValueError as exc:
            pytest.fail(f"allow_nplusone on {item.nodeid}: {exc}", pytrace=False)
        for label in patterns or ("*",):
            rules.append(
                IgnoreRule(
                    kind,
                    label,
                    marker.kwargs.get("file"),
                    marker.kwargs.get("function"),
                )
            )
    return ignore_all, tuple(rules)


def _fails(settings: _Settings, finding: Finding) -> bool:
    """True when the finding fails the test; else it is only a warning."""
    if finding.kind == KIND_UNUSED_EAGER_LOAD and settings.unused_eager != "fail":
        return False
    if finding.kind == KIND_CARTESIAN_LOAD and settings.cartesian != "fail":
        return False
    return settings.fails(finding.kind)


def _analyse(
    item: pytest.Item,
    collector: QueryCollector,
    settings: _Settings,
    tracker: eager.Tracker | None = None,
) -> tuple[str | None, str | None]:
    """(report of the failing kinds, report of the others) for the finished test.

    Either is None when empty; never raises.
    """
    if internal_error() is not None:
        return None, None
    ignore_all, marker_rules = _nplusone_ignores(item)
    if ignore_all:
        return None, None
    try:
        root = item.config.rootpath
        findings = make_findings(
            detect(
                collector,
                relationship_threshold=settings.relationship,
                repeat_threshold=settings.repeat,
                column_load_threshold=settings.column_load,
                small_repeat_threshold=settings.small_repeat,
                write_loop_threshold=settings.write_loop,
                scope=settings.repeat_scope,
            ),
            cwd=root,
        )
        if tracker is not None:
            findings.extend(eager.findings(tracker, cwd=root))
        if settings.cartesian != "off" and collector.cartesian_loads:
            findings.extend(cartesian.findings(collector, cwd=root))
        if settings.ignore or marker_rules:
            findings = _drop_ignored(item, settings, findings, marker_rules)
        _note_found(settings, findings, item.nodeid)
        findings = _apply_baseline(settings, findings, item)
        finding_groups.add(settings.groups, findings, item.nodeid)
        failing = [f for f in findings if _fails(settings, f)]
        warning = [f for f in findings if not _fails(settings, f)]
        note = ""
        if collector.truncated and settings.max_events is not None:
            note = "\n\n" + format_truncation(settings.max_events)
        return (
            format_findings(failing, cwd=root) + note if failing else None,
            format_findings(warning, cwd=root) + note if warning else None,
        )
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "N+1 analysis")
        return None, None


def _at_the_test(finding: Finding, item: pytest.Item) -> Finding:
    """The finding located at the test, for SARIF, when its call site is unknown.

    The call site is unknown when no application frame was on the stack, for
    example when ``session.execute`` itself is handed to a worker thread. GitHub
    shows a result only with a location, so the test stands in for it. Only
    the SARIF copy changes: the terminal text, the baseline and the ignore
    rules keep the finding as it was.
    """
    if finding.file is not None or finding.function is not None:
        return finding
    try:
        path, line, _ = item.reportinfo()
        file = item.nodeid.split("::", 1)[0]
        if not file or Path(str(path)).name != Path(file).name:
            return finding
        return replace(
            finding,
            file=file,
            function=getattr(item, "originalname", None) or item.name,
            line=(line or 0) + 1,
            message=finding.message.replace(
                "<unknown location>", "<unknown location>, reported at the test"
            ),
        )
    except Exception:  # noqa: BLE001  # a plugin bug must never break the user suite
        return finding


def _note_found(settings: _Settings, findings: list[Finding], nodeid: str) -> None:
    """Note the findings of a test after ignores, before the baseline, for the JSON."""
    if settings.json_path is not None:
        testjson.record_findings(settings.tests, nodeid, findings, settings.baseline)


def _apply_baseline(
    settings: _Settings, findings: list[Finding], item: pytest.Item
) -> list[Finding]:
    """The findings that still count: not in the baseline, and no update run.

    Records every finding it gets, for the stale-entry report and for an update.
    """
    if settings.baselining:
        settings.seen.update(f.identity for f in findings)
        if settings.update_baseline:
            findings = []
        else:
            findings = baselines.split(findings, settings.baseline or frozenset())[0]
    if settings.sarif_path is not None:
        for finding in findings:
            level = (
                sarif.LEVEL_ERROR if _fails(settings, finding) else sarif.LEVEL_WARNING
            )
            sarif.add(settings.reported, _at_the_test(finding, item), level)
    return findings


def _drop_ignored(
    item: pytest.Item,
    settings: _Settings,
    findings: list[Finding],
    marker_rules: tuple[IgnoreRule, ...],
    *,
    indexes: bool = False,
) -> list[Finding]:
    """The findings the ini patterns and the test's markers do not ignore.

    A marker pattern is stale when it matched neither an N+1 finding nor an
    index finding of the test: the N+1 pass (``indexes=False``) lists every
    unmatched pattern, the index pass then takes back those it matched.
    """
    kept, used = apply_rules(findings, (*settings.ignore, *marker_rules))
    settings.ignore_used.update(used)
    if indexes:
        settings.stale_markers[:] = [
            (nodeid, rule)
            for nodeid, rule in settings.stale_markers
            if nodeid != item.nodeid or rule not in used
        ]
    else:
        settings.stale_markers.extend(
            (item.nodeid, rule) for rule in marker_rules if rule not in used
        )
    return kept


def _index_exemption(item: pytest.Item) -> tuple[bool, tuple[str, ...]]:
    """(skip the whole check, tables to ignore) from allow_missing_index markers."""
    exempt_all = False
    tables: list[str] = []
    for marker in item.iter_markers("allow_missing_index"):
        if marker.kwargs or not all(isinstance(t, str) and t for t in marker.args):
            pytest.fail(
                f"allow_missing_index on {item.nodeid} takes table names "
                f"as strings, or nothing, got {marker.args!r} {marker.kwargs!r}",
                pytrace=False,
            )
        if not marker.args:
            exempt_all = True
        tables.extend(marker.args)
    return exempt_all, tuple(tables)


def _analyse_indexes(
    item: pytest.Item,
    collector: QueryCollector,
    settings: _Settings,
    exempt: tuple[str, ...],
) -> str | None:
    """The missing-index report for the finished test, or None; never raises."""
    if internal_error() is not None or not collector.scans:
        return None
    _, marker_rules = _nplusone_ignores(item)
    try:
        root = item.config.rootpath
        scans = select_findings(collector.scans, (*settings.ignore_tables, *exempt))
        if not scans:
            return None
        findings = [make_index_finding(scan, cwd=root) for scan in scans]
        kept = findings
        if settings.ignore or marker_rules:
            kept = _drop_ignored(item, settings, kept, marker_rules, indexes=True)
        _note_found(settings, kept, item.nodeid)
        if settings.baselining or settings.sarif_path:
            kept = _apply_baseline(settings, kept, item)
        if not kept:
            return None
        finding_groups.add(settings.groups, kept, item.nodeid)
        keep_ids = {id(f) for f in kept}
        scans = [
            scan
            for scan, finding in zip(scans, findings, strict=True)
            if id(finding) in keep_ids
        ]
        return format_index_findings(scans, cwd=root)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "missing-index analysis")
        return None


def _note_index_check(config: pytest.Config, collector: QueryCollector) -> None:
    """Add what the index check skipped in this test to the session summary."""
    if not collector.index_check:
        return
    notes = config.stash.setdefault(_index_notes, _IndexNotes())
    notes.notices.update(collector.index_notices)
    notes.partial_skipped.extend(collector.partial_skipped)
    for error in collector.explain_errors:
        notes.errors += 1
        if notes.first_error is None:
            notes.first_error = error


def _budget(item: pytest.Item) -> int | None:
    marker = item.get_closest_marker("max_queries")
    if marker is None:
        return None
    # ``n`` is the spelling of 0.1 to 0.3; it stays accepted, without a warning,
    # until a deprecation period (docs/stability.md) says otherwise.
    if len(marker.args) > 1 or set(marker.kwargs) - {"limit", "n"}:
        pytest.fail(
            f"max_queries on {item.nodeid} takes a single argument, the limit",
            pytrace=False,
        )
    keywords = [name for name in ("limit", "n") if name in marker.kwargs]
    if len(marker.args) + len(keywords) > 1:
        pytest.fail(
            f"max_queries on {item.nodeid} got the limit more than once "
            "(positionally, as limit= or as n=)",
            pytrace=False,
        )
    limit = marker.kwargs[keywords[0]] if keywords else None
    if marker.args:
        limit = marker.args[0]
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        pytest.fail(
            f"max_queries on {item.nodeid} needs a non-negative integer "
            f"as its limit, got {limit!r}",
            pytrace=False,
        )
    return limit


def _other_budget(
    item: pytest.Item, settings: _Settings
) -> tuple[int | None, str | None]:
    """(limit, where it comes from) for a test without a max_queries marker.

    The budget file first, then queryguard_max_queries. While the file is being
    updated neither applies: the run records the counts and writes them.
    """
    if settings.update_budgets:
        return None, None
    if item.nodeid in settings.budgets:
        name = settings.budgets_path.name if settings.budgets_path else "budget file"
        return settings.budgets[item.nodeid], (
            f"the budget of this test in {name}; if the new count is right, "
            "update it with --queryguard-update-budgets"
        )
    if settings.default_budget is not None:
        return settings.default_budget, (
            "queryguard_max_queries, the default budget of tests without one"
        )
    return None, None


def _time_budget(item: pytest.Item) -> float | None:
    marker = item.get_closest_marker("max_db_time")
    if marker is None:
        return None
    if len(marker.args) > 1 or set(marker.kwargs) - {"seconds"}:
        pytest.fail(
            f"max_db_time on {item.nodeid} takes a single argument, the limit "
            "in seconds",
            pytrace=False,
        )
    if marker.args and "seconds" in marker.kwargs:
        pytest.fail(
            f"max_db_time on {item.nodeid} got the limit twice, positionally "
            "and as seconds=",
            pytrace=False,
        )
    limit = marker.args[0] if marker.args else marker.kwargs.get("seconds")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or not limit >= 0
        or limit == float("inf")
    ):
        pytest.fail(
            f"max_db_time on {item.nodeid} needs a non-negative number of "
            f"seconds as its limit, got {limit!r}",
            pytrace=False,
        )
    return float(limit)


def _time_budget_report(
    item: pytest.Item, collector: QueryCollector, limit: float
) -> str | None:
    """The database-time failure text, or None; a bug here never fails the test."""
    try:
        if collector.db_time <= limit:
            return None
        return format_time_budget(
            limit,
            collector.statements,
            cwd=item.config.rootpath,
            total=collector.statement_count,
            db_time=collector.db_time,
        )
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "database-time budget report")
        return None


def _budget_report(
    item: pytest.Item, collector: QueryCollector, limit: int, source: str | None = None
) -> str | None:
    """The budget failure text, or None; a bug here never fails the test."""
    try:
        if collector.statement_count <= limit:
            return None
        return format_budget(
            limit,
            collector.statements,
            cwd=item.config.rootpath,
            total=collector.statement_count,
            db_time=collector.db_time,
            source=source,
        )
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "budget report")
        return None


def _record(item: pytest.Item, collector: QueryCollector, settings: _Settings) -> None:
    """Remember the test's statement count for the session report and the JSON."""
    # After an internal error the listeners stop recording, so a count would be wrong.
    if settings.mode == "off" or internal_error() is not None:
        return
    if settings.json_path is not None:
        testjson.record_counts(settings.tests, item.nodeid, collector)
    elif settings.update_budgets:
        settings.tests[item.nodeid] = {"statements": collector.statement_count}
    counts = item.config.stash.get(_report, None)
    if counts is not None:
        counts.append((item.nodeid, collector.statement_count))


def _write_report(
    terminalreporter: pytest.TerminalReporter, settings: _Settings
) -> None:
    terminalreporter.write_sep("=", "queryguard report")
    # dsession is registered only on the xdist controller when it distributes
    # tests; the counts live in the workers, so the report would be empty.
    if terminalreporter.config.pluginmanager.has_plugin("dsession"):
        terminalreporter.write_line(
            "queryguard report: not available with pytest-xdist"
        )
        return
    if settings.mode == "off":
        terminalreporter.write_line(
            "queryguard report: measurement is off (queryguard_mode = off)"
        )
        return
    if internal_error() is not None:
        terminalreporter.write_line(
            "queryguard report: incomplete, measurement stopped after an internal error"
        )
    counts = terminalreporter.config.stash.get(_report, [])
    if not counts:
        terminalreporter.write_line("no tests were measured")
        return
    # sorted() is stable, so tests with equal counts keep the order they ran in.
    heaviest = sorted(counts, key=lambda entry: entry[1], reverse=True)
    for nodeid, count in heaviest[:_REPORT_SIZE]:
        terminalreporter.write_line(f"{count:>5}  {nodeid}")


def _write_index_notes(terminalreporter: pytest.TerminalReporter) -> None:
    notes = terminalreporter.config.stash.get(_index_notes, None)
    if notes is None or not (notes.notices or notes.errors or notes.partial_skipped):
        return
    terminalreporter.write_sep("=", "queryguard index check")
    for line in format_index_notes(
        notes.notices, notes.errors, notes.first_error, notes.partial_skipped
    ):
        terminalreporter.write_line(line)


def _write_stale_ignores(
    terminalreporter: pytest.TerminalReporter, settings: _Settings
) -> None:
    """Warn about ignores that matched no finding in this run."""
    config = terminalreporter.config
    # The ini patterns match in the workers, so the controller cannot tell.
    # After an internal error or with measurement off, nothing was matched.
    if (
        config.pluginmanager.has_plugin("dsession")
        or settings.mode == "off"
        or internal_error() is not None
    ):
        return
    stale = [rule for rule in settings.ignore if rule not in settings.ignore_used]
    lines = format_stale_ignores(stale, settings.stale_markers)
    if lines:
        terminalreporter.write_sep("=", "queryguard: stale ignores", yellow=True)
        for line in lines:
            terminalreporter.write_line(line)


def _is_worker(config: pytest.Config) -> bool:
    return hasattr(config, "workerinput")


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object) -> None:
    """pytest-xdist, on the controller: collect what a worker found."""
    try:
        settings = node.config.stash.get(_settings, None)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "merging the results of a pytest-xdist worker")
        return
    if settings is None:
        return
    output = getattr(node, "workeroutput", None) or {}
    if settings.counting:
        try:
            if error is not None or output.get("queryguard_json_incomplete", True):
                settings.json_incomplete = True
            testjson.merge(settings.tests, output.get("queryguard_tests", {}))
            if output.get("queryguard_deselected", False):
                settings.deselected = True
            settings.collected.update(output.get("queryguard_collected", []))
        except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
            settings.json_incomplete = True
            record_error(exc, "merging the results of a pytest-xdist worker")
    try:
        finding_groups.merge_wire(settings.groups, output.get("queryguard_groups", []))
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "merging the results of a pytest-xdist worker")
    if not (settings.baselining or settings.sarif_path):
        return
    try:
        if error is not None or output.get("queryguard_incomplete", True):
            settings.baseline_incomplete = True
        for identity in output.get("queryguard_seen", []):
            settings.seen.add(_identity(identity))
        for file, function in output.get("queryguard_executed", []):
            settings.executed.add((file, function))
        if output.get("queryguard_deselected", False):
            settings.deselected = True
        sarif.merge_wire(settings.reported, output.get("queryguard_reported", []))
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        settings.baseline_incomplete = True
        record_error(exc, "merging the results of a pytest-xdist worker")


def pytest_deselected(items: Sequence[pytest.Item]) -> None:
    """Remember that a filter (-k, -m, --lf, --deselect) left tests out."""
    if not items:
        return
    try:
        settings = items[0].config.stash.get(_settings, None)
        if settings is not None and (
            settings.update_baseline or settings.update_budgets
        ):
            settings.deselected = True
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "baseline update")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """For a budget update: the tests that failed keep their old entry.

    With pytest-xdist the controller gets every worker's reports here.
    """
    if not report.failed:
        return
    try:
        settings = _active[-1] if _active else None
        if settings is not None and settings.update_budgets:
            settings.failed.add(report.nodeid)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "budget update")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> Generator[None, Any, Any]:
    """Notice a hook that drops tests without calling ``pytest_deselected``."""
    before = len(items)
    result = yield
    try:
        settings = config.stash.get(_settings, None)
        if (
            settings is not None
            and (settings.update_baseline or settings.update_budgets)
            and len(items) < before
        ):
            settings.deselected = True
        if settings is not None and settings.update_budgets:
            settings.collected.update(item.nodeid for item in items)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "baseline update")
    return result


def _test_key(item: pytest.Item) -> tuple[str | None, str]:
    """(rootdir-relative file, function) of a test, as a finding names a call site."""
    path = Path(str(item.path))
    try:
        file: str | None = path.relative_to(item.config.rootpath).as_posix()
    except ValueError:
        file = str(path)
    return file, getattr(item, "originalname", None) or item.name


def _run_is_narrowed(config: pytest.Config, deselected: bool) -> bool:
    """True when the session did not aim at the whole suite.

    That is a deselecting filter, ``--lf``, ``--sw``, ``--pyargs``, an ``--ignore``
    given on the command line, or file and node arguments that do not cover every
    ``testpaths`` entry (or the rootdir without it).
    """
    if deselected:
        return True
    # --lf skips whole files at collection without calling pytest_deselected
    for option in ("lf", "stepwise", "pyargs"):
        if config.getoption(option, None):
            return True
    if _ignores_on_command_line(config):
        return True
    suite = _suite_paths(config)
    invocation = config.invocation_params.dir
    given = [
        Path(os.path.abspath(Path(invocation, str(arg))))
        for arg in config.args
        if "::" not in str(arg)
    ]
    # the run covers the suite when every default path is one of the
    # arguments or below one of them; a node id covers nothing
    return not all(any(arg == p or arg in p.parents for arg in given) for p in suite)


def _suite_paths(config: pytest.Config) -> list[Path]:
    """The paths a bare ``pytest`` in the rootdir collects."""
    root = config.rootpath
    found: list[Path] = []
    for entry in config.getini("testpaths") or []:
        found.extend(
            Path(os.path.abspath(Path(root, match)))
            for match in glob.glob(str(entry), root_dir=root, recursive=True)
        )
    return found or [root]


def _ignores_on_command_line(config: pytest.Config) -> bool:
    """An ``--ignore`` or ``--ignore-glob`` on the command line or in PYTEST_ADDOPTS.

    One in the ini ``addopts`` is part of how the suite is defined, like
    ``collect_ignore``, so a run with it still covers the whole suite.
    """
    tokens = [str(arg) for arg in config.invocation_params.args]
    tokens += shlex.split(os.environ.get("PYTEST_ADDOPTS", ""))
    return any(
        token in ("--ignore", "--ignore-glob")
        or token.startswith(("--ignore=", "--ignore-glob="))
        for token in tokens
    )


def _identity(raw: Any) -> Identity:
    kind, label, file, function = raw
    return (kind, label, file, function)


_ABNORMAL_EXIT = frozenset(
    (
        pytest.ExitCode.INTERRUPTED,
        pytest.ExitCode.USAGE_ERROR,
        pytest.ExitCode.INTERNAL_ERROR,
    )
)


def pytest_sessionfinish(
    session: pytest.Session, exitstatus: int | pytest.ExitCode
) -> None:
    config = session.config
    settings = config.stash.get(_settings, None)
    if settings is not None and _is_worker(config):
        try:
            output = cast(Any, config).workeroutput
            if settings.groups:
                output["queryguard_groups"] = finding_groups.to_wire(settings.groups)
            if settings.counting:
                output["queryguard_tests"] = settings.tests
                output["queryguard_deselected"] = settings.deselected
                output["queryguard_collected"] = sorted(settings.collected)
                output["queryguard_json_incomplete"] = internal_error() is not None
        except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
            record_error(exc, "sending the results to the pytest-xdist controller")
    elif settings is not None:
        if settings.json_path is not None:
            _write_json(session, settings, exitstatus)
        if settings.update_budgets:
            _write_budgets(session, settings, exitstatus)
    if settings is None or not (settings.baselining or settings.sarif_path):
        return
    if _is_worker(config):
        # sent to the controller when the worker finishes
        output = cast(Any, config).workeroutput
        output["queryguard_incomplete"] = True  # until the results are in
        try:
            output["queryguard_seen"] = [
                list(identity) for identity in sorted_identities(settings.seen)
            ]
            output["queryguard_reported"] = sarif.to_wire(settings.reported)
            output["queryguard_executed"] = sorted(
                settings.executed, key=lambda key: (key[0] or "", key[1])
            )
            output["queryguard_deselected"] = settings.deselected
        except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
            record_error(exc, "sending the results to the pytest-xdist controller")
            output["queryguard_seen"] = []
            output["queryguard_reported"] = []
        output["queryguard_incomplete"] = internal_error() is not None
        return
    try:
        if internal_error() is not None:
            settings.baseline_incomplete = True
        # A collection error also counts when the run went on: pytest-xdist
        # without -x ends it with "tests failed", a serial run is interrupted.
        interrupted = settings.collection_failed or (
            exitstatus in _ABNORMAL_EXIT
            and not _xdist_maxfail_stop(session, settings, exitstatus)
        )
        if settings.update_baseline:
            _update_baseline(session, settings, interrupted)
        if settings.sarif_path is not None:
            if interrupted:
                # a partial report would close the alerts of the tests that never ran
                settings.sarif_failed = (
                    "not written: the run was interrupted or did not finish "
                    "(collection error, Ctrl-C or internal error); "
                    "an existing file is left as it was"
                )
            else:
                _write_sarif(settings)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "writing the baseline or SARIF file")


def _xdist_maxfail_stop(
    session: pytest.Session, settings: _Settings, exitstatus: int | pytest.ExitCode
) -> bool:
    """A pytest-xdist run that -x or --maxfail stopped, not a real interruption.

    pytest-xdist ends such a run with the "interrupted" exit status where a
    serial run ends with "tests failed"; it is then treated as the serial run.
    A collection error under -x sets the same flags and stays an interruption.
    """
    return (
        exitstatus == pytest.ExitCode.INTERRUPTED
        and bool(session.shouldstop or session.shouldfail)
        and not settings.collection_failed
        and session.config.pluginmanager.has_plugin("dsession")
    )


def _write_sarif(settings: _Settings) -> None:
    path = settings.sarif_path
    if path is None:
        return
    try:
        version = importlib.metadata.version("pytest-queryguard")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    try:
        prefix = sarif.workspace_prefix(
            settings.rootpath, os.environ.get("GITHUB_WORKSPACE")
        )
        document, omitted = sarif.build(settings.reported, version, prefix)
        sarif.write(path, document)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        settings.sarif_failed = f"could not write {path}: {exc}"
        return
    count = len(document["runs"][0]["results"])
    text = f"{path}: {count} results"
    if omitted:
        text += f" ({omitted} left out: no location inside the rootdir)"
    settings.sarif_result = text


def _counts_complete(
    session: pytest.Session, settings: _Settings, exitstatus: int | pytest.ExitCode
) -> bool:
    """Whether every selected test ran and was measured."""
    return not (
        settings.json_incomplete
        or internal_error() is not None
        or settings.collection_failed
        or exitstatus in _ABNORMAL_EXIT
        or session.shouldstop
        or session.shouldfail
    )


def _write_budgets(
    session: pytest.Session, settings: _Settings, exitstatus: int | pytest.ExitCode
) -> None:
    path = settings.budgets_path
    if path is None:
        return
    if not _counts_complete(session, settings, exitstatus):
        settings.budgets_failed = (
            "not updated: the run stopped early or measurement failed"
        )
        return
    if session.config.getoption("collectonly", False):
        settings.budgets_failed = "not updated: --collect-only runs no tests"
        return
    narrowed = _run_is_narrowed(session.config, settings.deselected)
    if narrowed and settings.budgets_previous_error is not None:
        settings.budgets_failed = (
            f"not updated: {settings.budgets_previous_error}; this run does not "
            "cover the whole suite, so the entries of the other tests would be lost"
        )
        return
    before = settings.budgets
    marked = {k for k, e in settings.tests.items() if e.get("max_queries_marker")}
    counts = {
        nodeid: count
        for nodeid, count in budget_files.counts_of(settings.tests).items()
        if nodeid not in marked and nodeid not in settings.failed
    }
    after = budget_files.updated(
        before,
        counts,
        narrowed=narrowed,
        collected=settings.collected,
        marked=marked,
    )
    try:
        budget_files.write(path, after)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        settings.budgets_failed = f"could not write {path}: {exc}"
        return
    text = f"{path}: {budget_files.describe(before, after)}"
    if settings.budgets_previous_error is not None:
        text += f"\nthe previous file was replaced: {settings.budgets_previous_error}"
    kept = len(settings.failed & set(after))
    if kept:
        text += f"\n{kept} failing tests kept their previous budget"
    if narrowed:
        text += "\nthis run did not cover the whole suite: entries of tests that did not run are kept"
    settings.budgets_written = text


def _write_json(
    session: pytest.Session, settings: _Settings, exitstatus: int | pytest.ExitCode
) -> None:
    path = settings.json_path
    if path is None:
        return
    try:
        version = importlib.metadata.version("pytest-queryguard")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    complete = _counts_complete(session, settings, exitstatus)
    try:
        testjson.write(path, testjson.document(settings.tests, version, complete))
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        settings.json_failed = f"could not write {path}: {exc}"
        return
    text = f"{path}: {len(settings.tests)} tests"
    if not complete:
        text += " (incomplete: the run stopped early or measurement failed)"
    settings.json_result = text


def _update_baseline(
    session: pytest.Session, settings: _Settings, interrupted: bool = False
) -> None:
    path = settings.baseline_path
    if path is None:
        return
    if interrupted:
        settings.baseline_failed = (
            "not updated: the run was interrupted or did not finish "
            "(collection error, Ctrl-C or internal error)"
        )
        return
    if session.shouldstop or session.shouldfail or settings.baseline_incomplete:
        settings.baseline_failed = (
            "not updated: the run stopped early or measurement failed"
        )
        return
    if session.config.getoption("collectonly", False):
        settings.baseline_failed = "not updated: --collect-only runs no tests"
        return
    before = settings.baseline or frozenset()
    after = frozenset(settings.seen)
    narrowed = _run_is_narrowed(session.config, settings.deselected)
    kept: frozenset[Identity] = frozenset()
    if narrowed:
        # only tests that ran may replace their entries; an entry whose
        # (file, function) is no test that ran stays, whether its test was
        # left out or deleted (a full run prunes it)
        kept = frozenset(
            entry
            for entry in before - after
            if (entry[2], entry[3]) not in settings.executed
        )
        after = after | kept
    try:
        baselines.write(path, after)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        settings.baseline_failed = f"could not write {path}: {exc}"
        return
    text = (
        f"{path}: {len(after)} entries ({len(after - before)} added, "
        f"{len(before - after)} removed)"
    )
    if narrowed:
        reason = "its test did not run" if len(kept) == 1 else "their tests did not run"
        noun = "entry" if len(kept) == 1 else "entries"
        text += (
            f"\nthis run did not cover the whole suite: {len(kept)} {noun} kept "
            f"because {reason}"
        )
    settings.baseline_written = text


def _write_baseline_summary(
    terminalreporter: pytest.TerminalReporter, settings: _Settings
) -> None:
    if not settings.baselining:
        return
    if settings.update_baseline:
        if settings.baseline_failed is not None:
            terminalreporter.write_sep("=", "queryguard: baseline", red=True)
            terminalreporter.write_line(
                f"queryguard baseline {settings.baseline_failed}"
            )
        elif settings.baseline_written is not None:
            terminalreporter.write_sep("=", "queryguard: baseline updated")
            terminalreporter.write_line(settings.baseline_written)
            if settings.baseline_previous_error is not None:
                terminalreporter.write_line(
                    f"replaced an unusable file ({settings.baseline_previous_error})"
                )
        return
    if (
        settings.mode == "off"
        or settings.baseline_incomplete
        or internal_error() is not None
        or settings.baseline_path is None
    ):
        return
    stale = baselines.sorted_identities(
        (settings.baseline or frozenset()) - settings.seen
    )
    lines = format_stale_baseline(stale, settings.baseline_path)
    if lines:
        terminalreporter.write_sep(
            "=", "queryguard: stale baseline entries", yellow=True
        )
        for line in lines:
            terminalreporter.write_line(line)


def _write_groups(
    terminalreporter: pytest.TerminalReporter, settings: _Settings
) -> None:
    lines = finding_groups.summary_lines(settings.groups)
    if not lines:
        return
    terminalreporter.write_sep("=", "queryguard: the same finding in several tests")
    for line in lines:
        terminalreporter.write_line(line)
    terminalreporter.write_line(
        "a finding from one place in the code is fixed once for all its tests"
    )


def _write_budgets_summary(
    terminalreporter: pytest.TerminalReporter, settings: _Settings
) -> None:
    if settings.budgets_failed is not None:
        terminalreporter.write_sep("=", "queryguard: budgets", red=True)
        terminalreporter.write_line(f"queryguard budgets {settings.budgets_failed}")
    elif settings.budgets_written is not None:
        terminalreporter.write_sep("=", "queryguard: budgets updated")
        terminalreporter.write_line(settings.budgets_written)


def _write_json_summary(
    terminalreporter: pytest.TerminalReporter, settings: _Settings
) -> None:
    if settings.json_failed is not None:
        terminalreporter.write_sep("=", "queryguard: JSON", red=True)
        terminalreporter.write_line(f"queryguard JSON {settings.json_failed}")
    elif settings.json_result is not None:
        terminalreporter.write_sep("=", "queryguard: JSON written")
        terminalreporter.write_line(settings.json_result)


def _write_sarif_summary(
    terminalreporter: pytest.TerminalReporter, settings: _Settings
) -> None:
    if settings.sarif_failed is not None:
        terminalreporter.write_sep("=", "queryguard: SARIF", red=True)
        terminalreporter.write_line(f"queryguard SARIF {settings.sarif_failed}")
    elif settings.sarif_result is not None:
        terminalreporter.write_sep("=", "queryguard: SARIF written")
        terminalreporter.write_line(settings.sarif_result)
        if settings.baseline_incomplete:
            terminalreporter.write_line(
                "incomplete: measurement stopped in this process or in a "
                "pytest-xdist worker, so findings may be missing"
            )


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    config = terminalreporter.config
    settings = config.stash.get(_settings, None)
    if settings is None:
        # pytest_configure failed: a usage error (then nothing was recorded)
        # or a bug, which is reported here.
        _report_internal_error(config)
        try:
            _write_internal_failure(terminalreporter)
        except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
            record_error(exc, "failure summary")
        return
    try:
        _write_index_notes(terminalreporter)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "summary of the index check")
    if config.getoption("queryguard_report"):
        try:
            _write_report(terminalreporter, settings)
        except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
            record_error(exc, "queryguard report")
    try:
        _write_groups(terminalreporter, settings)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "summary of findings in several tests")
    try:
        _write_stale_ignores(terminalreporter, settings)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "stale-ignore summary")
    try:
        _write_baseline_summary(terminalreporter, settings)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "baseline summary")
    try:
        _write_sarif_summary(terminalreporter, settings)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "SARIF summary")
    try:
        _write_json_summary(terminalreporter, settings)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "JSON summary")
    try:
        _write_budgets_summary(terminalreporter, settings)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "budget summary")
    _report_internal_error(config)
    try:
        _write_internal_failure(terminalreporter)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "failure summary")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, Any, Any]:
    # A finding escalated by ``filterwarnings = error`` is raised from this frame;
    # keep the plugin's own source out of that failure, as in fail mode. Any other
    # exception (a user test failure, a plugin bug) keeps this frame visible.
    __tracebackhide__ = _hide_escalated_warning
    _start_fixture(item)
    settings = item.config.stash.get(_settings, None)
    if settings is None:
        # pytest_configure did not complete (for example it raised a UsageError
        # for a bad option), so there is nothing to measure with.
        return (yield)
    if settings.update_baseline:
        try:
            settings.executed.add(_test_key(item))
        except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
            record_error(exc, "baseline update")
    # Marker errors fail the test on purpose (pytest.fail), before it runs.
    try:
        limit = _budget(item)
        source = None
        if limit is None:
            limit, source = _other_budget(item, settings)
        time_limit = _time_budget(item)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "start of the measurement")
        _report_internal_error(item.config)
        return (yield)
    if settings.mode == "off" and limit is None and time_limit is None:
        return (yield)
    index_check = settings.mode != "off" and settings.index_check
    try:
        exempt_all, exempt = _index_exemption(item) if index_check else (False, ())
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "start of the measurement")
        _report_internal_error(item.config)
        return (yield)
    try:
        collector, tracker = _start_measuring(
            settings,
            budgets=limit is not None or time_limit is not None,
            index_check=index_check and not exempt_all,
        )
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "start of the measurement")
        _report_internal_error(item.config)
        return (yield)
    if settings.strict and settings.mode != "off":
        collector.strict = _strict_hook(item, settings)
        collector.strict_threshold = settings.relationship
        collector.strict_by_scope = settings.repeat_scope == SCOPE_SESSION
    strict_error: NPlusOneDetected | None = None
    try:
        result = yield
    except NPlusOneDetected as exc:
        if not getattr(exc, "queryguard_strict", False):
            raise
        with contextlib.suppress(Exception):
            trim_to_user_code(exc)
        # raised again below, after the end-of-test analysis has recorded the
        # findings (SARIF, JSON, the summary) as for any failing N+1
        strict_error = exc
    finally:
        try:
            _stop_measuring(item, collector, settings, tracker)
            entry = settings.tests.get(item.nodeid) if settings.counting else None
            if entry is not None:
                # a test with its own marker keeps no entry in the budget file
                entry["max_queries_marker"] = limit is not None and source is None
        except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
            record_error(exc, "end of the measurement")
            _discard(collector, tracker)
        except BaseException:
            # a signal (KeyboardInterrupt, SystemExit) goes on, but leaves
            # no collector on the stack and no tracker behind
            _discard(collector, tracker)
            raise
    try:
        parts, warn_texts = _results(
            item, collector, settings, tracker, limit, time_limit, exempt, source
        )
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "end of the measurement")
        parts, warn_texts = [], []
    _report_internal_error(item.config)
    if strict_error is not None:
        # the failure at the access says it all; the report's text is the same N+1
        raise strict_error
    if not parts:
        _emit_warnings(item, warn_texts)
    if parts:
        pytest.fail("\n\n".join(parts), pytrace=False)
    return result


@pytest.fixture
def queryguard(
    request: pytest.FixtureRequest,
) -> Generator[QueryGuardFixture, None, None]:
    """Query budgets for blocks of a test, counting the test's own thread or task.

    ``with queryguard.max_queries(3):`` fails when the block runs more than 3
    SQL statements, ``with queryguard.num_queries(3):`` unless it runs exactly
    3; ``queryguard.queries`` and ``queryguard.count`` are the statements the
    test function has run so far. Statements of other threads do not count.
    """
    guard = QueryGuardFixture()
    if request.node.stash.get(_call_started, False):
        guard._start()  # requested from the test function itself
    yield guard
    guard._stop()  # when the test function never ran


def _fixtures(item: pytest.Item) -> list[QueryGuardFixture]:
    """The ``queryguard`` fixtures of the test, once each.

    Another fixture may return the same object under its own name.
    """
    values = getattr(item, "funcargs", {}).values()
    return list({id(v): v for v in values if isinstance(v, QueryGuardFixture)}.values())


def _start_fixture(item: pytest.Item) -> None:
    """Start the count of a requested ``queryguard`` fixture, at the test function.

    Here and not in the fixture, so that the statements of the fixtures set up
    after it are not counted. Runs before the test in its thread and context,
    so an asyncio test's task, which copies the context, counts too.
    """
    try:
        item.stash[_call_started] = True
        for guard in _fixtures(item):
            guard._start()
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "start of the queryguard fixture")
        _report_internal_error(item.config)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item) -> None:
    """Stop the ``queryguard`` fixtures before any fixture tears down."""
    try:
        item.stash[_call_started] = False
        for guard in _fixtures(item):
            guard._stop()
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "end of the queryguard fixture")


def _hide_escalated_warning(
    excinfo: pytest.ExceptionInfo[BaseException] | None,
) -> bool:
    """Hide a plugin frame only when a ``QueryGuardWarning`` was raised as an error."""
    return excinfo is not None and (
        excinfo.errisinstance(QueryGuardWarning)
        or bool(getattr(excinfo.value, "queryguard_strict", False))
    )


def _emit_warnings(item: pytest.Item, texts: list[str]) -> None:
    """Emit each finding as a ``QueryGuardWarning`` located at the test."""
    __tracebackhide__ = _hide_escalated_warning
    path, line, _ = item.reportinfo()
    for text in texts:
        std_warnings.warn_explicit(
            QueryGuardWarning(text),
            QueryGuardWarning,
            str(path),
            (line or 0) + 1,
        )


def _start_measuring(
    settings: _Settings, *, budgets: bool, index_check: bool
) -> tuple[QueryCollector, eager.Tracker | None]:
    """A collector on the stack and, when it is on, the eager-load tracker."""
    if budgets:
        install()  # idempotent; budgets also work when the mode is off
    collector = QueryCollector()
    collector.max_events = settings.max_events
    collector.index_check = index_check
    collector.index_advisor = settings.index_advisor
    collector.hypopg = settings.hypopg
    tracker = eager.start() if settings.mode != "off" and eager.is_enabled() else None
    try:
        push(collector)
    except BaseException:
        if tracker is not None:
            eager.stop(tracker)
        raise
    return collector, tracker


def _stop_measuring(
    item: pytest.Item,
    collector: QueryCollector,
    settings: _Settings,
    tracker: eager.Tracker | None,
) -> None:
    """Take the collector off the stack, whatever happens; never raises an Exception.

    Runs in a finally block: an error here would replace the test's own.
    """
    try:
        try:
            if tracker is not None:
                eager.stop(tracker)
        finally:
            pop(collector)
        _report_internal_error(item.config)
        _record(item, collector, settings)  # failing tests are reported too
        _note_index_check(item.config, collector)
    except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
        record_error(exc, "end of the measurement")


def _discard(collector: QueryCollector, tracker: eager.Tracker | None) -> None:
    """Best effort: take the collector and the tracker off after a failed stop."""
    # the error is already recorded; nothing here may add another
    if tracker is not None:
        with contextlib.suppress(Exception):
            eager.stop(tracker)
    with contextlib.suppress(Exception):
        pop(collector)


def _results(
    item: pytest.Item,
    collector: QueryCollector,
    settings: _Settings,
    tracker: eager.Tracker | None,
    limit: int | None,
    time_limit: float | None,
    exempt: tuple[str, ...],
    source: str | None = None,
) -> tuple[list[str], list[str]]:
    """(texts that fail the test, texts that only warn); the first wins when set."""
    parts: list[str] = []
    if limit is not None:
        budget = _budget_report(item, collector, limit, source)
        if budget is not None:
            parts.append(budget)
    if time_limit is not None:
        time_budget = _time_budget_report(item, collector, time_limit)
        if time_budget is not None:
            parts.append(time_budget)
    fail_texts: list[str] = []
    warn_texts: list[str] = []
    if settings.mode != "off":
        failing, warning = _analyse(item, collector, settings, tracker)
        index = _analyse_indexes(item, collector, settings, exempt)
        if index is not None:
            (fail_texts if settings.fails(KIND_MISSING_INDEX) else warn_texts).append(
                index
            )
        if failing is not None:
            fail_texts.insert(0, failing)
        if warning is not None:
            warn_texts.insert(0, warning)
    if parts or fail_texts:
        # one failure: budgets first, then N+1 and index findings, then the
        # findings of kinds that would only have warned
        return [*parts, *fail_texts, *warn_texts], []
    return [], warn_texts
