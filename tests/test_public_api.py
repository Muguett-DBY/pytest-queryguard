"""A snapshot of everything users can depend on.

If one of these tests fails, a public name, signature, option or default
changed. That is a breaking change unless it is a pure addition: follow
docs/stability.md (deprecate first, remove in a major release), and update
the snapshot here in the same commit so the change is visible in review.
Additions are allowed but must be added to the snapshot on purpose.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.metadata
import inspect
import pickle
import re
import warnings
from pathlib import Path
from typing import Any

import pytest

import pytest_queryguard

EXPECTED_ALL = [
    "CallSite",
    "NPlusOneDetected",
    "Query",
    "QueryBudgetExceeded",
    "QueryGuardDeprecationWarning",
    "QueryGuardFixture",
    "QueryGuardInternalWarning",
    "QueryGuardWarning",
    "QueryLog",
    "__version__",
    "ignore",
    "max_db_time",
    "max_queries",
    "record_queries",
]

EXPECTED_FUNCTIONS = {
    "ignore": "(*kinds: 'str', reason: 'str | None' = None) -> 'IgnoreBlock'",
    "max_db_time": "(seconds: 'float') -> 'AbstractContextManager[QueryLog]'",
    "max_queries": "(limit: 'int') -> 'AbstractContextManager[QueryLog]'",
    "record_queries": "() -> 'AbstractContextManager[QueryLog]'",
}

EXPECTED_BASES = {
    "CallSite": "object",
    "NPlusOneDetected": "AssertionError",
    "Query": "object",
    "QueryBudgetExceeded": "AssertionError",
    "QueryGuardDeprecationWarning": "DeprecationWarning",
    "QueryGuardFixture": "object",
    "QueryGuardInternalWarning": "UserWarning",
    "QueryGuardWarning": "UserWarning",
    "QueryLog": "object",
}

EXPECTED_DATACLASS_FIELDS = {
    "CallSite": [
        ("filename", "str"),
        ("lineno", "int"),
        ("function", "str"),
        ("callers", "tuple[CallSite, ...]"),
    ],
    "Query": [
        ("sql", "str"),
        ("callsite", "CallSite | None"),
        ("relationship", "str | None"),
        ("duration", "float | None"),
    ],
}

# Public members of QueryLog (names without a leading underscore, plus the
# container protocol), with their signatures.
EXPECTED_QUERYLOG = {
    "__getitem__": "(self, index: 'int | slice') -> 'Query | tuple[Query, ...]'",
    "__iter__": "(self) -> 'Iterator[Query]'",
    "__len__": "(self) -> 'int'",
    "assert_no_nplusone": (
        "(self, *, relationship_threshold: 'int' = 2, repeat_threshold: 'int' = 5, "
        "column_load_threshold: 'int' = 5, "
        "small_repeat_threshold: 'int | None' = None, "
        "write_loop_threshold: 'int | None' = None, "
        "scope: 'str' = 'test', repeat_scope: 'str | None' = None) -> 'None'"
    ),
    "count": "property",
    "db_time": "property",
    "queries": "property",
    "slowest": "(self, n: 'int' = 5) -> 'tuple[Query, ...]'",
}

# Public members of QueryGuardFixture, what the queryguard fixture gives a test.
EXPECTED_FIXTURE = {
    "count": "property",
    "max_queries": "(self, limit: 'int') -> 'AbstractContextManager[QueryLog]'",
    "num_queries": "(self, expected: 'int') -> 'AbstractContextManager[QueryLog]'",
    "queries": "property",
}

EXPECTED_INI = {
    "queryguard_mode": "fail",
    "queryguard_relationship_threshold": "2",
    "queryguard_repeat_threshold": "5",
    "queryguard_column_load_threshold": "5",
    "queryguard_max_events": "10000",
    "queryguard_stack_depth": "3",
    "queryguard_small_repeat_threshold": "0",
    "queryguard_scope": "None",
    "queryguard_repeat_scope": "None",
    "queryguard_write_loop_threshold": "0",
    "queryguard_unused_eager_load": "off",
    "queryguard_unused_columns": "off",
    "queryguard_cartesian_load": "warn",
    "queryguard_strict": "False",
    "queryguard_show_patch": "False",
    "queryguard_fail_on": "[]",
    "queryguard_ignore": "[]",
    "queryguard_baseline": "",
    "queryguard_budgets": "",
    "queryguard_max_queries": "",
    "queryguard_index_check": "False",
    "queryguard_index_ignore_tables": "[]",
    "queryguard_index_advisor": "True",
    "queryguard_hypopg": "auto",
}

EXPECTED_FLAGS = [
    "--no-queryguard-index-check",
    "--queryguard-baseline",
    "--queryguard-budgets",
    "--queryguard-index-check",
    "--queryguard-json",
    "--queryguard-mode",
    "--queryguard-report",
    "--queryguard-sarif",
    "--queryguard-show-patch",
    "--queryguard-strict",
    "--queryguard-update-baseline",
    "--queryguard-update-budgets",
    "--queryguard-write-patch",
]

EXPECTED_MARKERS = [
    "allow_missing_index",
    "allow_nplusone",
    "max_db_time",
    "max_queries",
    "queryguard_ignore",
    "queryguard_max_db_time",
    "queryguard_max_queries",
]


def test_all_is_the_documented_list() -> None:
    assert sorted(pytest_queryguard.__all__) == EXPECTED_ALL
    for name in pytest_queryguard.__all__:
        assert hasattr(pytest_queryguard, name), name


def test_deprecation_warning_is_apart_from_the_finding_warnings() -> None:
    """``error::QueryGuardWarning`` must not turn a deprecation into an error."""
    deprecation = pytest_queryguard.QueryGuardDeprecationWarning
    assert not issubclass(deprecation, pytest_queryguard.QueryGuardWarning)
    assert not issubclass(deprecation, pytest_queryguard.QueryGuardInternalWarning)
    assert not issubclass(deprecation, UserWarning)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.filterwarnings("error", category=pytest_queryguard.QueryGuardWarning)
        warnings.warn("old name, use new", deprecation, stacklevel=1)
    assert [w.category for w in caught] == [deprecation]


def test_version_is_a_release_number() -> None:
    """A plain release number, optionally a release candidate (PEP 440)."""
    assert re.fullmatch(r"\d+\.\d+\.\d+(rc\d+)?", pytest_queryguard.__version__)


@pytest.mark.parametrize("name", sorted(EXPECTED_FUNCTIONS))
def test_function_signatures(name: str) -> None:
    func = getattr(pytest_queryguard, name)
    assert str(inspect.signature(func)) == EXPECTED_FUNCTIONS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_BASES))
def test_class_bases(name: str) -> None:
    cls = getattr(pytest_queryguard, name)
    assert [base.__name__ for base in cls.__bases__] == [EXPECTED_BASES[name]]


@pytest.mark.parametrize("name", sorted(EXPECTED_DATACLASS_FIELDS))
def test_dataclass_fields(name: str) -> None:
    cls = getattr(pytest_queryguard, name)
    fields = [(f.name, str(f.type)) for f in dataclasses.fields(cls)]
    assert fields == EXPECTED_DATACLASS_FIELDS[name]
    assert cls.__dataclass_params__.frozen  # type: ignore[attr-defined]


def test_query_constructor_is_keyword_only() -> None:
    params = inspect.signature(pytest_queryguard.Query).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def _public_members(cls: type) -> dict[str, str]:
    members: dict[str, str] = {}
    for name, value in vars(cls).items():
        if name.startswith("_") and name not in ("__getitem__", "__iter__", "__len__"):
            continue
        if isinstance(value, property):
            members[name] = "property"
        elif callable(value):
            members[name] = str(inspect.signature(value))
    return members


def test_querylog_public_members() -> None:
    assert _public_members(pytest_queryguard.QueryLog) == EXPECTED_QUERYLOG


def test_queryguard_fixture_public_members() -> None:
    assert _public_members(pytest_queryguard.QueryGuardFixture) == EXPECTED_FIXTURE


def test_queryguard_fixture_name(pytester: pytest.Pytester) -> None:
    text = _help_lines(pytester, "--fixtures")
    assert re.search(r"^queryguard -- ", text, re.MULTILINE), text


def _help_lines(pytester: pytest.Pytester, *args: str) -> str:
    return pytester.runpytest_subprocess(*args).stdout.str()


def test_ini_option_names_and_defaults(pytester: pytest.Pytester) -> None:
    config = pytester.parseconfig()
    defaults: dict[str, Any] = {}
    for name in sorted(EXPECTED_INI):
        defaults[name] = str(config.getini(name))
    assert defaults == EXPECTED_INI


def test_no_unlisted_ini_options(pytester: pytest.Pytester) -> None:
    text = _help_lines(pytester, "--help")
    found = sorted(set(re.findall(r"^\s+(queryguard_\w+) \(", text, re.MULTILINE)))
    assert found == sorted(EXPECTED_INI)


def test_command_line_flags(pytester: pytest.Pytester) -> None:
    text = _help_lines(pytester, "--help")
    found = sorted(set(re.findall(r"(--(?:no-)?queryguard-[\w-]+)", text)))
    assert found == EXPECTED_FLAGS


def test_markers(pytester: pytest.Pytester) -> None:
    text = _help_lines(pytester, "--markers")
    found = sorted(
        {
            name
            for name in re.findall(r"@pytest\.mark\.(\w+)", text)
            if name in EXPECTED_MARKERS
            or name.startswith(("allow_", "max_", "queryguard_"))
        }
    )
    assert found == EXPECTED_MARKERS


def test_entry_point_name() -> None:
    points = importlib.metadata.entry_points(group="pytest11")
    ours = [p for p in points if p.value.startswith("pytest_queryguard")]
    assert [(p.name, p.value) for p in ours] == [
        ("queryguard", "pytest_queryguard.plugin")
    ]


def test_exported_names_report_the_package_as_their_module() -> None:
    """Tracebacks and pickles show ``pytest_queryguard.X``, not a submodule."""
    for name in pytest_queryguard.__all__:
        if name == "__version__":
            continue
        obj = getattr(pytest_queryguard, name)
        assert obj.__module__ == "pytest_queryguard", name


def test_exceptions_are_named_by_the_package_in_tracebacks() -> None:
    import traceback

    error = pytest_queryguard.NPlusOneDetected("x")
    text = "".join(traceback.format_exception(error))
    assert "pytest_queryguard.NPlusOneDetected: x" in text
    assert "pytest_queryguard.api" not in text


def test_exported_classes_still_pickle() -> None:
    site = pytest_queryguard.CallSite(filename="a.py", lineno=3, function="f")
    assert pickle.loads(pickle.dumps(site)) == site
    error = pickle.loads(pickle.dumps(pytest_queryguard.QueryBudgetExceeded("x")))
    assert type(error) is pytest_queryguard.QueryBudgetExceeded


def test_type_hints_of_exported_names_still_resolve() -> None:
    """``get_type_hints`` looks names up in ``sys.modules[obj.__module__]``.

    With ``__module__`` set to the package, an annotation that names a type
    the package does not export would stop resolving for classes.
    """
    import typing

    for name in pytest_queryguard.__all__:
        if name == "__version__":
            continue
        typing.get_type_hints(getattr(pytest_queryguard, name))


# The in-app mode is experimental (docs/stability.md#experimental): it may
# change in a minor release without a deprecation period, but not by accident.
EXPECTED_APP_ALL = [
    "CallSite",
    "Finding",
    "Query",
    "QueryGuardASGI",
    "QueryGuardWSGI",
    "RequestReport",
    "Settings",
    "internal_error",
    "is_watched",
    "opentelemetry_reporter",
    "sentry_reporter",
    "set_route",
    "track",
    "unwatch",
    "watch",
]

_MIDDLEWARE_INIT = (
    "(self, app: '{app}', settings: 'Settings | None' = None, *, "
    "panel: 'bool' = False, panel_path: 'str' = '/_queryguard', "
    "panel_history: 'int' = 50, panel_loopback_only: 'bool' = True, "
    "panel_token: 'str | None' = None, **options: 'Any') -> 'None'"
)

EXPECTED_APP_SIGNATURES = {
    "watch": "(engine: 'Any') -> 'None'",
    "unwatch": "(engine: 'Any') -> 'None'",
    "is_watched": "(engine: 'Any') -> 'bool'",
    "track": (
        "(name: 'str' = '', *, settings: 'Settings | None' = None, "
        "**options: 'Any') -> 'track'"
    ),
    "set_route": "(template: 'str') -> 'None'",
    "internal_error": "() -> 'BaseException | None'",
    "opentelemetry_reporter": "() -> 'Reporter'",
    "sentry_reporter": "(*, level: 'str' = 'warning') -> 'Reporter'",
    "QueryGuardASGI.__init__": _MIDDLEWARE_INIT.format(app="ASGIApp"),
    "QueryGuardWSGI.__init__": _MIDDLEWARE_INIT.format(app="WSGIApp"),
}

EXPECTED_APP_FIELDS = {
    "Settings": [
        "relationship_threshold",
        "repeat_threshold",
        "column_load_threshold",
        "small_repeat_threshold",
        "write_loop_threshold",
        "scope",
        "budgets",
        "default_budget",
        "report",
        "max_events",
        "sample_rate",
        "min_db_time",
        "page_report",
        "repeat_scope",
    ],
    "RequestReport": [
        "id",
        "name",
        "method",
        "path",
        "route",
        "status",
        "started",
        "duration",
        "statements",
        "statement_count",
        "db_time",
        "findings",
        "budget",
        "error",
        "sampled",
    ],
}

# The keys of the panel's requests.json, whose "requests" are
# RequestReport.to_dict(); the page reads them.
EXPECTED_PANEL_JSON = ["version", "watched_engines", "internal_error", "requests"]
EXPECTED_REPORT_DICT = [
    "id",
    "name",
    "method",
    "path",
    "route",
    "status",
    "started",
    "duration",
    "statement_count",
    "truncated",
    "db_time",
    "budget",
    "over_budget",
    "error",
    "statements",
    "findings",
]
EXPECTED_STATEMENT_DICT = [
    "sql",
    "duration",
    "relationship",
    "file",
    "line",
    "function",
]
EXPECTED_FINDING_DICT = [
    "kind",
    "label",
    "file",
    "line",
    "function",
    "count",
    "message",
]


def test_app_all_is_the_documented_list() -> None:
    from pytest_queryguard import app

    assert sorted(app.__all__) == EXPECTED_APP_ALL
    for name in app.__all__:
        assert hasattr(app, name), name


@pytest.mark.parametrize("name", sorted(EXPECTED_APP_SIGNATURES))
def test_app_signatures(name: str) -> None:
    from pytest_queryguard import app

    obj: Any = app
    for part in name.split("."):
        obj = getattr(obj, part)
    assert str(inspect.signature(obj)) == EXPECTED_APP_SIGNATURES[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_APP_FIELDS))
def test_app_dataclass_fields(name: str) -> None:
    from pytest_queryguard import app

    fields = dataclasses.fields(getattr(app, name))
    names = [f.name for f in fields if not f.name.startswith("_")]
    assert names == EXPECTED_APP_FIELDS[name]


def test_panel_json_keys() -> None:
    import json

    from pytest_queryguard.app import CallSite, Finding, Query, RequestReport
    from pytest_queryguard.app._panel import History, json_body

    history = History(1)
    history.add(
        RequestReport(
            id=1,
            name="GET /",
            statements=(
                Query(
                    sql="SELECT 1",
                    callsite=CallSite(filename="a.py", lineno=1, function="f"),
                    relationship=None,
                ),
            ),
            statement_count=1,
            findings=(
                Finding(
                    kind="repeated-statement",
                    label="SELECT 1",
                    file="a.py",
                    function="f",
                    message="m",
                ),
            ),
        )
    )
    data = json.loads(json_body(history))
    assert list(data) == EXPECTED_PANEL_JSON
    report = data["requests"][0]
    assert list(report) == EXPECTED_REPORT_DICT
    assert list(report["statements"][0]) == EXPECTED_STATEMENT_DICT
    assert list(report["findings"][0]) == EXPECTED_FINDING_DICT


# --- files, tools and the action (docs/stability.md#files-tools-and-the-action)

_MODELS = (Path(__file__).parent / "models.py").read_text(encoding="utf-8")

_FILES_SUITE = """
import pytest
from qgmodels import Order, make_sync_session


def list_orders(session):
    return [order.customer.name for order in session.query(Order).all()]


@pytest.mark.parametrize("i", range(2))
def test_nplusone(i):
    list_orders(make_sync_session())


@pytest.mark.max_queries(100)
def test_marked():
    make_sync_session().query(Order).all()
"""

EXPECTED_JSON_TOP = ["format", "tool", "complete", "tests"]
EXPECTED_JSON_TOOL = ["name", "version"]
EXPECTED_JSON_TEST = [
    "statements",
    "by_verb",
    "db_time",
    "truncated",
    "findings",
    "max_queries_marker",
]
EXPECTED_JSON_FINDING = [
    "kind",
    "label",
    "count",
    "file",
    "function",
    "line",
    "stack",
    "baselined",
    "message",
]
EXPECTED_JSON_FRAME = ["file", "function", "line"]
EXPECTED_BASELINE_TOP = ["version", "findings"]
EXPECTED_BASELINE_ENTRY = ["kind", "label", "file", "function"]
EXPECTED_BUDGETS_TOP = ["format", "budgets"]

# SARIF rule ids in the order of ruleIndex: a new kind goes at the end.
EXPECTED_SARIF_RULES = [
    "queryguard/relationship-load",
    "queryguard/repeated-statement",
    "queryguard/column-load",
    "queryguard/write-loop",
    "queryguard/unused-eager-load",
    "queryguard/unused-columns",
    "queryguard/cartesian-load",
    "queryguard/missing-index",
]
EXPECTED_SARIF_FINGERPRINT = "queryguardIdentity/v1"
EXPECTED_SARIF_PROPERTIES = ["count", "label", "tests", "peakCount"]

# the finding kinds that queryguard_ignore and queryguard_fail_on accept
EXPECTED_KINDS = [
    "relationship-load",
    "repeated-statement",
    "column-load",
    "write-loop",
    "unused-eager-load",
    "unused-columns",
    "cartesian-load",
    "missing-index",
]


def _json(path: Path) -> Any:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def test_the_written_files_keep_their_keys(pytester: pytest.Pytester) -> None:
    """The keys of every file the plugin writes, as one run writes them."""
    pytester.makepyfile(qgmodels=_MODELS, test_files=_FILES_SUITE)
    result = pytester.runpytest_subprocess(
        "-p",
        "no:cacheprovider",
        "--queryguard-mode=warn",
        "--queryguard-json=q.json",
        "--queryguard-sarif=s.sarif",
        "--queryguard-budgets=b.json",
        "--queryguard-update-budgets",
    )
    result.assert_outcomes(passed=3)
    counts = _json(pytester.path / "q.json")
    assert list(counts) == EXPECTED_JSON_TOP
    assert counts["format"] == 1
    assert list(counts["tool"]) == EXPECTED_JSON_TOOL
    entry = counts["tests"]["test_files.py::test_nplusone[0]"]
    assert list(entry) == EXPECTED_JSON_TEST
    assert list(entry["findings"][0]) == EXPECTED_JSON_FINDING
    assert list(entry["findings"][0]["stack"][0]) == EXPECTED_JSON_FRAME
    assert counts["tests"]["test_files.py::test_marked"]["max_queries_marker"] is True

    budgets = _json(pytester.path / "b.json")
    assert list(budgets) == EXPECTED_BUDGETS_TOP
    assert budgets["format"] == 1
    # a test with its own marker has no entry
    assert sorted(budgets["budgets"]) == [
        "test_files.py::test_nplusone[0]",
        "test_files.py::test_nplusone[1]",
    ]

    sarif = _json(pytester.path / "s.sarif")
    assert sarif["version"] == "2.1.0"
    driver = sarif["runs"][0]["tool"]["driver"]
    assert [rule["id"] for rule in driver["rules"]] == EXPECTED_SARIF_RULES
    (finding,) = sarif["runs"][0]["results"]  # one finding, from two tests
    assert finding["ruleId"] == "queryguard/relationship-load"
    assert EXPECTED_SARIF_FINGERPRINT in finding["partialFingerprints"]
    assert list(finding["properties"]) == EXPECTED_SARIF_PROPERTIES

    result = pytester.runpytest_subprocess(
        "-p",
        "no:cacheprovider",
        "--queryguard-baseline=base.json",
        "--queryguard-update-baseline",
    )
    result.assert_outcomes(passed=3)
    baseline = _json(pytester.path / "base.json")
    assert list(baseline) == EXPECTED_BASELINE_TOP
    assert baseline["version"] == 1
    assert list(baseline["findings"][0]) == EXPECTED_BASELINE_ENTRY


def test_finding_kinds() -> None:
    from pytest_queryguard import ignores, sarif

    assert list(ignores.KINDS) == EXPECTED_KINDS
    assert sorted(sarif.KINDS) == sorted(EXPECTED_KINDS)


def _cli_arguments(main: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The positional arguments and the options of a tool's argument parser."""
    found: list[str] = []

    def capture(parser: argparse.ArgumentParser, *args: Any, **kwargs: Any) -> Any:
        for action in parser._actions:
            if "--help" in action.option_strings:
                continue
            name = "/".join(action.option_strings) or action.dest
            extra = [
                f"{key}={value!r}"
                for key, value in (
                    ("nargs", action.nargs),
                    ("default", action.default),
                    ("choices", action.choices),
                )
                if value not in (None, [], False)
            ]
            found.append(" ".join([name, *extra]))
        raise SystemExit(0)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    with pytest.raises(SystemExit):
        main([])
    return found


EXPECTED_TOOL_ARGUMENTS = {
    # public
    "compare": [
        "base",
        "head",
        "--output",
        "--rows default=25",
        "--key default=''",
    ],
    # experimental, snapshotted so a change is deliberate
    "trend": [
        "runs nargs='+'",
        "--top default=10",
        "--test",
        "--format default='markdown' choices=('markdown', 'text', 'json')",
        "--ascii",
        "--label",
        "--key default=''",
        "--details",
        "--skip-unreadable",
        "--output",
    ],
    "lint": [
        "paths nargs='+'",
        "--models",
        "--format default='text' choices=('text', 'json', 'sarif')",
        "--output",
    ],
}


@pytest.mark.parametrize("tool", sorted(EXPECTED_TOOL_ARGUMENTS))
def test_command_line_tool_arguments(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module(f"pytest_queryguard.{tool}")
    found = _cli_arguments(module.main, monkeypatch)
    assert found == EXPECTED_TOOL_ARGUMENTS[tool]


def test_compare_marker() -> None:
    from pytest_queryguard import compare

    assert compare.MARKER == "<!-- pytest-queryguard compare -->"


EXPECTED_TREND_JSON = [
    "format",
    "runs",
    "complete",
    "changed",
    "changed_total",
    "added_tests",
    "gone_tests",
    "new_findings",
    "gone_findings",
]


EXPECTED_TREND_RUN = ["label", "tests", "statements", "complete"]
EXPECTED_TREND_CHANGED = ["nodeid", "counts", "first", "last", "change"]
EXPECTED_TREND_FINDING = ["kind", "label", "file", "function", "count", "tests"]


def test_trend_json_keys() -> None:
    import json

    from pytest_queryguard import trend

    def run(tests: dict[str, Any]) -> dict[str, Any]:
        return {"format": 1, "complete": True, "tests": tests}

    def finding(label: str) -> dict[str, Any]:
        return {
            "kind": "relationship-load",
            "label": label,
            "count": 3,
            "file": "a.py",
            "function": "f",
            "line": 1,
        }

    runs = [
        run({"t.py::a": {"statements": 2, "findings": [finding("A.old")]}}),
        run({"t.py::a": {"statements": 9, "findings": [finding("A.new")]}}),
    ]
    doc = json.loads(trend.as_json(trend.trend(runs, ["r1", "r2"])))
    assert list(doc) == EXPECTED_TREND_JSON
    assert doc["format"] == 1
    assert list(doc["runs"][0]) == EXPECTED_TREND_RUN
    assert list(doc["changed"][0]) == EXPECTED_TREND_CHANGED
    assert list(doc["new_findings"][0]) == [*EXPECTED_TREND_FINDING, "since"]
    assert list(doc["gone_findings"][0]) == EXPECTED_TREND_FINDING


# Experimental (docs/mcp.md): tool name -> (argument names, required ones).
EXPECTED_MCP_TOOLS = {
    "run_tests": (["args", "paths", "timeout"], []),
    "explain_finding": (["kind"], ["kind"]),
    "compare": (["base_json", "head_json"], ["base_json", "head_json"]),
}


def test_mcp_tools_and_arguments() -> None:
    from pytest_queryguard import mcp

    found = {
        name: (sorted(tool.properties), sorted(tool.required))
        for name, tool in mcp.TOOLS.items()
    }
    assert found == EXPECTED_MCP_TOOLS


EXPECTED_ACTION_INPUTS = {
    "pytest-args": '""',
    "python-version": '""',
    "package": '""',
    "baseline": '""',
    "fail": '"true"',
    "working-directory": '"."',
    "sarif-file": "queryguard.sarif",
    "upload": '"true"',
    "category": "pytest-queryguard",
    "json-file": "queryguard.json",
    "compare": '"true"',
    "comment": '"true"',
    "trend": '"false"',
    "github-token": "${{ github.token }}",
}
EXPECTED_ACTION_OUTPUTS = ["exit-code", "sarif-file", "json-file", "trend-report"]


def _action_section(text: str, name: str) -> str:
    match = re.search(rf"^{name}:\n(.*?)(?=^\S)", text, re.MULTILINE | re.DOTALL)
    assert match, name
    return match[1]


def test_action_inputs_outputs_and_artifact() -> None:
    text = (Path(__file__).parents[1] / "action" / "action.yml").read_text(
        encoding="utf-8"
    )
    inputs: dict[str, str] = {}
    name = ""
    for line in _action_section(text, "inputs").splitlines():
        key = re.fullmatch(r"  ([a-z-]+):", line)
        if key:
            name = key[1]
            inputs[name] = "(none)"
        default = re.fullmatch(r"    default: (.*)", line)
        if default:
            inputs[name] = default[1].strip()
    assert inputs == EXPECTED_ACTION_INPUTS
    outputs = re.findall(
        r"^  ([a-z-]+):$", _action_section(text, "outputs"), re.MULTILINE
    )
    assert outputs == EXPECTED_ACTION_OUTPUTS
    assert "name: queryguard-json-${{ inputs.category }}" in text


# docs/stability.md#deprecated-names
def test_deprecated_names_say_when_they_go() -> None:
    from pytest_queryguard import app
    from pytest_queryguard.warnings import REMOVED_IN

    assert REMOVED_IN == "2.0"
    deprecation = pytest_queryguard.QueryGuardDeprecationWarning
    with pytest.warns(deprecation, match=r"removed in pytest-queryguard 2\.0"):
        settings = app.Settings(repeat_scope="session")
    assert settings.scope == "session"
    with pytest_queryguard.record_queries() as log:
        pass
    with pytest.warns(deprecation, match=r"removed in pytest-queryguard 2\.0"):
        log.assert_no_nplusone(repeat_scope="session")


def test_deprecated_option_and_marker_say_when_they_go(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeini("[pytest]\nqueryguard_repeat_scope = session\n")
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.max_queries(n=5)
        def test_old_marker():
            pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    removed = "is deprecated and will be removed in pytest-queryguard 2.0*"
    result.stdout.fnmatch_lines(
        [
            f"*QueryGuardDeprecationWarning: queryguard_repeat_scope {removed}",
            f"*QueryGuardDeprecationWarning: max_queries(n=...) {removed}",
        ]
    )


# Experimental, but the defaults are documented in docs/in-app.md.
EXPECTED_APP_SETTINGS_DEFAULTS = {
    "relationship_threshold": 2,
    "repeat_threshold": 5,
    "column_load_threshold": 5,
    "small_repeat_threshold": None,
    "write_loop_threshold": None,
    "scope": "request",
    "budgets": {},
    "default_budget": None,
    "report": "log",
    "max_events": 1000,
    "sample_rate": 1.0,
    "min_db_time": 0.0,
    "page_report": "off",
    "repeat_scope": None,
}


def test_app_settings_defaults() -> None:
    from pytest_queryguard import app

    settings = app.Settings()
    found = {
        f.name: getattr(settings, f.name) for f in dataclasses.fields(app.Settings)
    }
    assert found == EXPECTED_APP_SETTINGS_DEFAULTS


def test_query_defaults_agree_between_the_plugin_and_python() -> None:
    """``assert_no_nplusone()`` and ``app.Settings`` use the ini defaults."""
    from pytest_queryguard import app

    params = inspect.signature(pytest_queryguard.QueryLog.assert_no_nplusone).parameters
    for name in ("relationship_threshold", "repeat_threshold", "column_load_threshold"):
        ini = int(EXPECTED_INI[f"queryguard_{name}"])
        assert params[name].default == ini, name
        assert EXPECTED_APP_SETTINGS_DEFAULTS[name] == ini, name
    for name in ("small_repeat_threshold", "write_loop_threshold"):
        assert EXPECTED_INI[f"queryguard_{name}"] == "0"  # 0 is off in ini
        assert params[name].default is None  # None is off in Python
        assert getattr(app.Settings(), name) is None
