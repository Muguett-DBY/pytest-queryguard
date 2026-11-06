from __future__ import annotations

from pathlib import Path

import pytest

from pytest_queryguard import plugin

# pytester runs sessions in-process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone

MODELS = (Path(__file__).parent / "models.py").read_text()

NPLUSONE_TEST = """
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


def test_nplusone_fails_test(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=NPLUSONE_TEST)
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*N+1 detected*",
            "*Order.customer was lazy-loaded*",
            "*joinedload(Order.customer)*",
        ]
    )


def test_selectinload_version_passes(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from qgmodels import Order, make_sync_session


        def test_ok():
            session = make_sync_session()
            stmt = select(Order).options(selectinload(Order.customer))
            for order in session.scalars(stmt):
                _ = order.customer.name
        """
    )
    project.runpytest().assert_outcomes(passed=1)


def test_warn_mode_passes_with_warning(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=NPLUSONE_TEST)
    result = project.runpytest("--queryguard-mode=warn")
    result.assert_outcomes(passed=1, warnings=1)
    result.stdout.fnmatch_lines(["*QueryGuardWarning*N+1 detected*"])


def test_warning_can_be_escalated_by_its_public_path(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(test_a=NPLUSONE_TEST)
    project.makepyprojecttoml(
        "[tool.pytest.ini_options]\n"
        'queryguard_mode = "warn"\n'
        'filterwarnings = ["error::pytest_queryguard.QueryGuardWarning"]\n'
    )
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*QueryGuardWarning*N+1 detected*"])


@pytest.mark.parametrize("tbstyle", ["auto", "long", "short"])
def test_escalated_warning_hides_plugin_source(
    project: pytest.Pytester, tbstyle: str
) -> None:
    project.makepyfile(test_a=NPLUSONE_TEST)
    project.makeini(
        "[pytest]\nqueryguard_mode = warn\n"
        "filterwarnings =\n    error::pytest_queryguard.QueryGuardWarning\n"
    )
    result = project.runpytest(f"--tb={tbstyle}")
    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert "N+1 detected" in output
    for internal in (
        "def pytest_runtest_call",
        "warn_explicit",
        "_emit_warnings",
        "settings = item.config.stash",
        "plugin.py",
    ):
        assert internal not in output


def test_escalated_warning_hides_plugin_source_under_xdist(
    project: pytest.Pytester,
) -> None:
    pytest.importorskip("xdist")
    project.makepyfile(test_a=NPLUSONE_TEST)
    project.makeini("[pytest]\nqueryguard_mode = warn\nfilterwarnings =\n    error\n")
    result = project.runpytest("-n", "2", "--tb=long", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert "N+1 detected" in output
    assert "def pytest_runtest_call" not in output
    assert "plugin.py" not in output


@pytest.mark.parametrize("mode", ["warn", "fail"])
def test_escalation_keeps_user_failure_traceback(
    project: pytest.Pytester, mode: str
) -> None:
    project.makepyfile(
        test_a="""
        def helper(value):
            raise ValueError(f"user error {value}")


        def test_raises():
            helper("deep")
        """
    )
    project.makeini(
        f"[pytest]\nqueryguard_mode = {mode}\nfilterwarnings =\n    error\n"
    )
    result = project.runpytest("--tb=long")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        ['*helper("deep")*', '*raise ValueError(f"user error {value}")*']
    )
    assert "def pytest_runtest_call" not in result.stdout.str()


def test_plugin_bug_in_hook_keeps_hook_frame(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _emit_warnings is not behind a fail-open guard: a QueryGuardWarning that
    # filterwarnings turns into an error must reach the test. Every other
    # step of the hook degrades to an internal warning instead of failing.
    emit = plugin._emit_warnings

    def boom(item: pytest.Item, texts: list[str]) -> None:
        if item.nodeid != "test_a.py::test_ok":  # this test's own call phase
            return emit(item, texts)
        raise RuntimeError("emit warnings bug")

    monkeypatch.setattr(plugin, "_emit_warnings", boom)
    project.makepyfile(test_a="def test_ok():\n    pass\n")
    result = project.runpytest("--tb=long")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*def pytest_runtest_call*",
            "*_emit_warnings(item, warn_texts)*",
            "*emit warnings bug*",
        ]
    )


def test_plain_warn_mode_warning_points_at_the_test(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(test_a=NPLUSONE_TEST)
    result = project.runpytest("--queryguard-mode=warn")
    result.assert_outcomes(passed=1, warnings=1)
    result.stdout.fnmatch_lines(
        ["*test_a.py:*: QueryGuardWarning: queryguard: N+1 detected*"]
    )


def test_warning_can_be_ignored_by_its_public_path(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=NPLUSONE_TEST)
    result = project.runpytest(
        "--queryguard-mode=warn",
        "-W",
        "ignore::pytest_queryguard.QueryGuardWarning",
    )
    result.assert_outcomes(passed=1)


def test_off_mode_ignores(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=NPLUSONE_TEST)
    project.makeini("[pytest]\nqueryguard_mode = off\n")
    project.runpytest().assert_outcomes(passed=1)


def test_cli_overrides_ini(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=NPLUSONE_TEST)
    project.makeini("[pytest]\nqueryguard_mode = off\n")
    project.runpytest("--queryguard-mode=fail").assert_outcomes(failed=1)


def test_allow_nplusone_marker(project: pytest.Pytester) -> None:
    marked = NPLUSONE_TEST.replace(
        "def test_loop", "@pytest.mark.allow_nplusone\ndef test_loop"
    )
    project.makepyfile(test_a="import pytest\n" + marked)
    project.runpytest().assert_outcomes(passed=1)


def test_fixture_queries_not_counted(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        import pytest
        from qgmodels import Customer, Order, make_sync_session


        @pytest.fixture
        def session():
            session = make_sync_session()
            for order in session.query(Order).all():
                _ = order.customer.name
            return session


        def test_one_query(session):
            session.get(Customer, 1)
        """
    )
    project.runpytest().assert_outcomes(passed=1)


def test_own_failure_preserved(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=NPLUSONE_TEST + "    assert False\n")
    result = project.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*assert False*"])
    result.stdout.no_fnmatch_line("*N+1 detected*")


def test_invalid_mode_is_usage_error(project: pytest.Pytester) -> None:
    project.makeini("[pytest]\nqueryguard_mode = loud\n")
    result = project.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*queryguard_mode*fail*warn*off*"])


@pytest.mark.skipif(
    pytest.version_tuple < (9,), reason="native TOML config needs pytest 9"
)
def test_non_string_mode_from_native_toml_is_usage_error(
    project: pytest.Pytester,
) -> None:
    project.path.joinpath("pytest.toml").write_text("[pytest]\nqueryguard_mode = 1\n")
    result = project.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*queryguard_mode*fail*warn*off*"])
    result.stderr.no_fnmatch_line("*INTERNALERROR*")


def test_invalid_cli_mode_is_usage_error(project: pytest.Pytester) -> None:
    result = project.runpytest("--queryguard-mode=loud")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*queryguard-mode*fail*warn*off*"])


def test_mode_is_case_insensitive_on_the_command_line(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(test_a=NPLUSONE_TEST)
    project.runpytest("--queryguard-mode=Warn").assert_outcomes(passed=1)
    project.runpytest("--queryguard-mode= FAIL ").assert_outcomes(failed=1)


@pytest.mark.parametrize(
    ("option", "value", "expected"),
    [
        ("queryguard_mode", "WARN", "warn"),
        ("queryguard_mode", " Off ", "off"),
        ("queryguard_hypopg", "ON", "on"),
        ("queryguard_unused_eager_load", "Warn", "warn"),
        ("queryguard_unused_columns", " FAIL ", "fail"),
        ("queryguard_repeat_scope", "Session", "session"),
    ],
)
def test_enum_options_are_case_insensitive(
    project: pytest.Pytester, option: str, value: str, expected: str
) -> None:
    project.makeini(f"[pytest]\n{option} = {value}\n")
    config = project.parseconfig()
    settings = plugin._read_settings(config)
    got = {
        "queryguard_mode": settings.mode,
        "queryguard_hypopg": plugin._hypopg(config),
        "queryguard_unused_eager_load": plugin._unused_eager(config),
        "queryguard_unused_columns": plugin._unused_columns(config),
        "queryguard_repeat_scope": plugin._repeat_scope(config),
    }[option]
    assert got == expected


@pytest.mark.parametrize(
    "option",
    [
        "queryguard_mode",
        "queryguard_hypopg",
        "queryguard_unused_eager_load",
        "queryguard_unused_columns",
        "queryguard_repeat_scope",
    ],
)
def test_enum_options_still_reject_unknown_values(
    project: pytest.Pytester, option: str
) -> None:
    project.makeini(f"[pytest]\n{option} = Loud\n")
    result = project.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([f"*{option} must be one of*got 'Loud'*"])


@pytest.mark.parametrize(
    "option", ["queryguard_relationship_threshold", "queryguard_repeat_threshold"]
)
def test_threshold_below_two_is_usage_error(
    project: pytest.Pytester, option: str
) -> None:
    project.makeini(f"[pytest]\n{option} = 1\n")
    result = project.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([f"*{option}*2*"])


def test_repeat_threshold_from_ini(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        from qgmodels import Customer, make_sync_session


        def test_loop():
            session = make_sync_session()
            for i in (1, 2, 3):
                session.get(Customer, i)
        """
    )
    project.makeini("[pytest]\nqueryguard_repeat_threshold = 3\n")
    project.runpytest().assert_outcomes(failed=1)


@pytest.mark.skipif(
    pytest.version_tuple < (9,), reason="native TOML config needs pytest 9"
)
def test_threshold_from_native_toml_int(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_a="""
        from qgmodels import Customer, make_sync_session


        def test_loop():
            session = make_sync_session()
            for i in (1, 2, 3):
                session.get(Customer, i)
        """
    )
    project.path.joinpath("pytest.toml").write_text(
        "[pytest]\nqueryguard_repeat_threshold = 3\n"
    )
    project.runpytest().assert_outcomes(failed=1)


def test_repeat_threshold_from_pyproject_ini_options(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_a="""
        from qgmodels import Customer, make_sync_session


        def test_loop():
            session = make_sync_session()
            for i in (1, 2, 3):
                session.get(Customer, i)
        """
    )
    project.makepyprojecttoml(
        '[tool.pytest.ini_options]\nqueryguard_repeat_threshold = "3"\n'
    )
    project.runpytest().assert_outcomes(failed=1)


def test_threshold_from_pyproject_ini_options(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=NPLUSONE_TEST)
    project.makepyprojecttoml(
        '[tool.pytest.ini_options]\nqueryguard_relationship_threshold = "4"\n'
    )
    project.runpytest().assert_outcomes(passed=1)


def test_non_integer_threshold_is_usage_error(project: pytest.Pytester) -> None:
    project.makeini("[pytest]\nqueryguard_repeat_threshold = many\n")
    result = project.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*queryguard_repeat_threshold*"])


BROKEN_CALLSITE = """
import pytest
from pytest_queryguard import listeners


@pytest.fixture(autouse=True)
def _broken(monkeypatch):
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(listeners, "find_callsite", boom)
"""


def test_internal_error_warned_once_even_if_test_fails(
    project: pytest.Pytester,
) -> None:
    project.makeconftest(BROKEN_CALLSITE)
    project.makepyfile(
        test_a="""
        from qgmodels import Customer, make_sync_session


        def test_first():
            make_sync_session().get(Customer, 1)
            assert False


        def test_second():
            make_sync_session().get(Customer, 1)
        """
    )
    result = project.runpytest()
    result.assert_outcomes(passed=1, failed=1, warnings=1)
    result.stdout.fnmatch_lines(["*QueryGuardInternalWarning*boom*"])


def test_detection_error_does_not_fail_test(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise ValueError("detect broke")

    monkeypatch.setattr(plugin, "detect", boom)
    project.makepyfile(test_a=NPLUSONE_TEST)
    result = project.runpytest()
    result.assert_outcomes(passed=1, warnings=1)
    result.stdout.fnmatch_lines(["*QueryGuardInternalWarning*detect broke*"])


def test_inner_internal_error_does_not_leak_into_outer_session(
    project: pytest.Pytester,
) -> None:
    from pytest_queryguard import listeners

    project.makeconftest(BROKEN_CALLSITE)
    project.makepyfile(test_a=NPLUSONE_TEST)
    project.runpytest()
    assert listeners.internal_error() is None


def test_warn_mode_points_at_the_test(project: pytest.Pytester) -> None:
    project.makepyfile(test_a=NPLUSONE_TEST)
    result = project.runpytest("--queryguard-mode=warn")
    result.stdout.fnmatch_lines(["*test_a.py:*: QueryGuardWarning*"])
    result.stdout.no_fnmatch_line("*plugin.py*")


def test_outer_error_survives_nested_session(
    project: pytest.Pytester,
) -> None:
    from pytest_queryguard import listeners

    outer = RuntimeError("outer")
    listeners.record_error(outer)
    project.makepyfile(test_a=NPLUSONE_TEST)
    project.runpytest().assert_outcomes(failed=1)  # the inner session is unaffected
    assert listeners.internal_error() is outer
    listeners.swap_error(None)
