"""A snapshot of everything users can depend on.

If one of these tests fails, a public name, signature, option or default
changed. That is a breaking change unless it is a pure addition: follow
docs/stability.md (deprecate first, remove in a major release), and update
the snapshot here in the same commit so the change is visible in review.
Additions are allowed but must be added to the snapshot on purpose.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import inspect
import pickle
import re
import warnings
from typing import Any

import pytest

import pytest_queryguard

EXPECTED_ALL = [
    "CallSite",
    "NPlusOneDetected",
    "Query",
    "QueryBudgetExceeded",
    "QueryGuardDeprecationWarning",
    "QueryGuardInternalWarning",
    "QueryGuardWarning",
    "QueryLog",
    "__version__",
    "max_db_time",
    "max_queries",
    "record_queries",
]

EXPECTED_FUNCTIONS = {
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
    "QueryGuardInternalWarning": "UserWarning",
    "QueryGuardWarning": "UserWarning",
    "QueryLog": "object",
}

EXPECTED_DATACLASS_FIELDS = {
    "CallSite": [("filename", "str"), ("lineno", "int"), ("function", "str")],
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
        "repeat_scope: 'str' = 'test') -> 'None'"
    ),
    "count": "property",
    "db_time": "property",
    "queries": "property",
    "slowest": "(self, n: 'int' = 5) -> 'tuple[Query, ...]'",
}

EXPECTED_INI = {
    "queryguard_mode": "fail",
    "queryguard_relationship_threshold": "2",
    "queryguard_repeat_threshold": "5",
    "queryguard_column_load_threshold": "5",
    "queryguard_max_events": "10000",
    "queryguard_small_repeat_threshold": "0",
    "queryguard_repeat_scope": "test",
    "queryguard_write_loop_threshold": "0",
    "queryguard_unused_eager_load": "off",
    "queryguard_cartesian_load": "warn",
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
    "--queryguard-update-baseline",
    "--queryguard-update-budgets",
]

EXPECTED_MARKERS = [
    "allow_missing_index",
    "allow_nplusone",
    "max_db_time",
    "max_queries",
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
            if name in EXPECTED_MARKERS or name.startswith(("allow_", "max_"))
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
        "**options: 'Any') -> 'Iterator[RequestReport]'"
    ),
    "set_route": "(template: 'str') -> 'None'",
    "internal_error": "() -> 'BaseException | None'",
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
        "repeat_scope",
        "budgets",
        "default_budget",
        "report",
        "max_events",
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
