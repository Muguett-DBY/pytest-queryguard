from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from pytest_queryguard import listeners, plugin
from pytest_queryguard.collector import QueryCollector, pop, push
from pytest_queryguard.messages import format_failure

# pytester runs sessions in-process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone
_quiet = pytest.mark.filterwarnings(
    "ignore::pytest_queryguard.warnings.QueryGuardInternalWarning"
)

MODELS = (Path(__file__).parent / "models.py").read_text()

BROKEN_CALLSITE = """
import pytest
from pytest_queryguard import listeners


@pytest.fixture(autouse=True)
def _broken(monkeypatch):
    def boom():
        raise RuntimeError("callsite broke")

    monkeypatch.setattr(listeners, "find_callsite", boom)
"""

PLAIN_GET = """
from qgmodels import Customer, make_sync_session


def test_first():
    make_sync_session().get(Customer, 1)


def test_second():
    make_sync_session().get(Customer, 1)
"""

LAZY_LOAD = """
from qgmodels import Order, make_sync_session


def test_loop():
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name
"""


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS)
    return pytester


@pytest.fixture(autouse=True)
def _clean_error() -> Iterator[None]:
    yield
    listeners._reset_for_tests()


def test_summary_names_the_statement_listener(project: pytest.Pytester) -> None:
    project.makeconftest(BROKEN_CALLSITE)
    project.makepyfile(test_a=PLAIN_GET)
    result = project.runpytest()
    result.assert_outcomes(passed=2, warnings=1)
    result.stdout.fnmatch_lines(
        [
            "*queryguard: measurement stopped*",
            "pytest-queryguard stopped measuring after an error in the *listener*:",
            "  RuntimeError: callsite broke",
            "  raised at conftest.py:*in boom",
            "Your tests were not affected.*",
        ]
    )


@_quiet
def test_orm_listener_failure_is_named() -> None:
    class Options:
        _for_refresh_state = False

        @property
        def _current_path(self) -> None:
            raise RuntimeError("path broke")

    context = SimpleNamespace(
        execution_options={},
        invoked_statement=SimpleNamespace(
            _propagate_attrs={"compile_state_plugin": "orm"},
            _compile_options=Options(),
        ),
    )
    collector = QueryCollector()
    push(collector)
    try:
        listeners._on_cursor_execute(None, None, "SELECT 1", (), context, False)
    finally:
        pop(collector)
    failure = listeners.internal_failure()
    assert failure is not None
    assert failure.source == "before_cursor_execute listener (statements)"
    assert failure.summary == "RuntimeError: path broke"


def test_summary_appears_once_per_session(project: pytest.Pytester) -> None:
    project.makeconftest(BROKEN_CALLSITE)
    project.makepyfile(test_a=PLAIN_GET)
    result = project.runpytest()
    assert result.stdout.str().count("queryguard: measurement stopped") == 1


def test_summary_without_the_report_flag_and_with_it(
    project: pytest.Pytester,
) -> None:
    project.makeconftest(BROKEN_CALLSITE)
    project.makepyfile(test_a=PLAIN_GET)
    result = project.runpytest("--queryguard-report")
    result.stdout.fnmatch_lines(
        ["*queryguard report*", "*queryguard: measurement stopped*"]
    )
    assert result.stdout.str().count("queryguard: measurement stopped") == 1


def test_no_summary_without_an_error(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=PLAIN_GET)
    result = project.runpytest()
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*measurement stopped*")


def test_failure_in_the_analysis_is_named(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise ValueError("detect broke")

    monkeypatch.setattr(plugin, "detect", boom)
    project.makepyfile(test_a=LAZY_LOAD)
    result = project.runpytest()
    result.assert_outcomes(passed=1, warnings=1)
    result.stdout.fnmatch_lines(
        [
            "*QueryGuardInternalWarning*detect broke*(N+1 analysis)*",
            "*error in the N+1 analysis:*",
            "  ValueError: detect broke",
        ]
    )


@_quiet
def test_failing_transaction_listener_is_named() -> None:
    def boom(*args: object) -> None:
        raise RuntimeError("catalog broke")

    listeners._guarded(boom, "commit")(object())
    failure = listeners.internal_failure()
    assert failure is not None
    assert failure.source == "commit listener (catalog cache)"
    assert failure.summary == "RuntimeError: catalog broke"
    assert failure.raised_at is not None and failure.raised_at.endswith("in boom")


@_quiet
def test_first_failure_wins() -> None:
    listeners.record_error(RuntimeError("one"), "first step")
    listeners.record_error(RuntimeError("two"), "second step")
    failure = listeners.internal_failure()
    assert failure is not None and failure.source == "first step"


@_quiet
def test_failure_without_a_source_is_unknown() -> None:
    previous = listeners.swap_error(RuntimeError("set from outside"))
    try:
        failure = listeners.internal_failure()
        assert failure is not None
        assert failure.source == "unknown"
        assert failure.raised_at is None  # never raised, so no traceback
    finally:
        listeners.swap_error(previous)


def test_no_failure_without_an_error() -> None:
    assert listeners.internal_failure() is None


def test_format_failure_without_a_location() -> None:
    failure = listeners.Failure("a step", ValueError("x"), None)
    lines = format_failure(failure)
    assert lines[:2] == [
        "pytest-queryguard stopped measuring after an error in the a step:",
        "  ValueError: x",
    ]
    assert not any("raised at" in line for line in lines)
