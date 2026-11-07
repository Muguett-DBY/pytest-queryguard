"""The corpus as ordinary tests: every scenario against every configuration.

One pytest session per configuration runs all scenarios in-process (pytester);
each (scenario, configuration) is then one test here. A scenario whose module
lists the configuration in ``KNOWN`` is a documented limitation and is an
``xfail(strict=True)``: it must keep disagreeing with its verdict, so the entry
is removed when the plugin changes.
"""

from __future__ import annotations

import pytest

from . import kit, runner
from .metrics import known_reason

pytest.importorskip("greenlet")
pytest.importorskip("aiosqlite")
pytest.importorskip("fastapi")
pytest.importorskip("flask_sqlalchemy")
pytest.importorskip("factory")

# pytester sessions run in this process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone

if kit.pg_url_from_env():
    # pytester restores sys.modules after each in-process session. asyncpg's
    # compiled protocol module, first imported inside a session and imported
    # again in the next one, crashed the process (a segmentation fault).
    # Imported here, it is part of the restored snapshot.
    import asyncpg  # noqa: F401

SCENARIOS = runner.load()
BY_NAME = {s.name: s for s in SCENARIOS}
_cache: dict[str, dict[str, runner.Result]] = {}


@pytest.fixture
def results(
    request: pytest.FixtureRequest,
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
):
    config: str = request.param

    def get() -> dict[str, runner.Result]:
        if config not in _cache:
            names = [s.name for s in SCENARIOS]
            ini, _ = runner.CONFIGS[config]
            out = runner.write_session(pytester.path, ini, names)
            for key, value in runner.session_env(names, out, "queryguard").items():
                monkeypatch.setenv(key, value)
            pytester.inline_run("-p", "no:cacheprovider", "-W", "ignore", "-q")
            _cache[config] = runner.read_results(out)
        return _cache[config]

    return get


def _cases() -> list[object]:
    cases = []
    for config in runner.CONFIGS:
        for scenario in SCENARIOS:
            reason = known_reason(scenario, config)
            marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
            cases.append(
                pytest.param(
                    config, scenario.name, marks=marks, id=f"{config}-{scenario.name}"
                )
            )
    return cases


@pytest.mark.parametrize("config, name", _cases())
def test_plugin_agrees_with_the_verdict(
    config: str,
    name: str,
    request: pytest.FixtureRequest,
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = BY_NAME[name]
    if config not in _cache:
        names = [s.name for s in SCENARIOS]
        ini, _ = runner.CONFIGS[config]
        out = runner.write_session(pytester.path, ini, names)
        for key, value in runner.session_env(names, out, "queryguard").items():
            monkeypatch.setenv(key, value)
        pytester.inline_run("-p", "no:cacheprovider", "-W", "ignore", "-q")
        _cache[config] = runner.read_results(out)
    result = _cache[config][name]
    # Checked here as well as in the test below: under pytest-xdist that test
    # sees only the configurations its own worker ran, so it would catch a
    # scenario that fails without findings only on some runs.
    assert result.failed == bool(result.findings), (
        "the scenario's test failed without a finding"
        if result.failed
        else "a finding did not fail the scenario's test"
    )
    scope = runner.scope(config)
    observed = result.kinds & scope
    expected = scenario.kinds & scope
    assert observed == expected, (
        f"{scenario.verdict}: expected {sorted(expected)}, the plugin reported {sorted(observed)}"
    )


def test_a_failing_test_has_findings_and_a_passing_one_has_none() -> None:
    for config, results in _cache.items():
        for name, result in results.items():
            assert result.failed == bool(result.findings), (config, name)


def test_the_corpus_is_large_and_balanced() -> None:
    bugs = [s for s in SCENARIOS if s.verdict == kit.BUG]
    clean = [s for s in SCENARIOS if s.verdict == kit.CLEAN]
    assert len(SCENARIOS) >= 60
    assert min(len(bugs), len(clean)) / max(len(bugs), len(clean)) >= 0.6


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_scenario_documents_its_verdict(scenario: runner.Scenario) -> None:
    first = scenario.doc.splitlines()[0]
    assert scenario.name.startswith(scenario.verdict.lower() + "_")
    assert first.startswith(
        scenario.verdict + (" " if scenario.verdict == kit.BUG else ":")
    )
    if scenario.verdict == kit.BUG:
        assert scenario.kinds, "a BUG names the finding kinds"
        assert all(kind in first for kind in scenario.kinds), first
    else:
        assert not scenario.kinds
    assert len(scenario.doc.splitlines()) >= 1
