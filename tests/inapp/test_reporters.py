"""The ready-made OpenTelemetry and Sentry reporters (#278).

Neither library is a test dependency: fakes in ``sys.modules`` stand in for
the calls the reporters make.
"""

from __future__ import annotations

import logging
import sys
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pytest_queryguard import app as queryguard
from pytest_queryguard.sarif import fingerprint

from .conftest import CUSTOMERS, Order


def _lazy_loop(engine: Engine) -> None:
    with Session(engine) as session:
        for order in session.scalars(select(Order)):
            _ = order.customer.name  # a loop: a comprehension is <listcomp> before 3.12


class FakeSpan:
    def __init__(self, recording: bool = True) -> None:
        self.recording = recording
        self.attributes: dict[str, Any] = {}
        self.events: list[tuple[str, dict[str, Any]]] = []

    def is_recording(self) -> bool:
        return self.recording

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        self.attributes.update(attributes)

    def add_event(self, name: str, attributes: dict[str, Any]) -> None:
        self.events.append((name, attributes))


def _fake_opentelemetry(monkeypatch: pytest.MonkeyPatch, span: FakeSpan) -> None:
    trace = ModuleType("opentelemetry.trace")
    trace.get_current_span = lambda: span  # type: ignore[attr-defined]
    package = ModuleType("opentelemetry")
    package.trace = trace  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry", package)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", trace)


def _fake_sentry(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    sentry_sdk = ModuleType("sentry_sdk")

    def capture_message(message: str, **kwargs: Any) -> None:
        sent.append({"message": message, **kwargs})

    sentry_sdk.capture_message = capture_message  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", sentry_sdk)
    return sent


def test_opentelemetry_reporter_adds_events_to_the_current_span(
    watched: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    span = FakeSpan()
    _fake_opentelemetry(monkeypatch, span)
    reporter = queryguard.opentelemetry_reporter()
    with queryguard.track("job", default_budget=2, report=reporter) as report:
        _lazy_loop(watched)
    assert span.attributes == {
        "queryguard.statement_count": 1 + CUSTOMERS,
        "queryguard.db_time": report.db_time,
        "queryguard.findings": 1,
        "queryguard.budget": 2,
        "queryguard.over_budget": True,
    }
    finding = report.findings[0]
    assert span.events == [
        (
            "queryguard.finding",
            {
                "queryguard.kind": "relationship-load",
                "queryguard.label": "Order.customer",
                "queryguard.file": "tests/inapp/test_reporters.py",
                "queryguard.line": finding.line,
                "queryguard.function": "_lazy_loop",
                "queryguard.count": CUSTOMERS,
                "queryguard.fingerprint": fingerprint(finding.identity),
            },
        ),
        (
            "queryguard.budget",
            {"queryguard.budget": 2, "queryguard.statement_count": 1 + CUSTOMERS},
        ),
    ]


def test_opentelemetry_reporter_leaves_out_none_and_idle_spans(
    watched: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    span = FakeSpan()
    _fake_opentelemetry(monkeypatch, span)
    with queryguard.track(report=queryguard.opentelemetry_reporter()):
        _lazy_loop(watched)
    assert "queryguard.budget" not in span.attributes
    assert [name for name, _ in span.events] == ["queryguard.finding"]

    idle = FakeSpan(recording=False)
    _fake_opentelemetry(monkeypatch, idle)
    with queryguard.track(report=queryguard.opentelemetry_reporter()):
        _lazy_loop(watched)
    assert idle.attributes == {}
    assert idle.events == []


def test_sentry_reporter_sends_one_message_per_finding(
    watched: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _fake_sentry(monkeypatch)
    reporter = queryguard.sentry_reporter(level="info")
    with queryguard.track("job", default_budget=2, report=reporter) as report:
        _lazy_loop(watched)
    finding = report.findings[0]
    assert [m["message"] for m in sent] == [
        "queryguard: relationship-load Order.customer",
        "queryguard: job ran 6 statements, budget 2",
    ]
    first, budget = sent
    assert first["level"] == "info"
    assert first["fingerprint"] == ["queryguard", fingerprint(finding.identity)]
    assert first["tags"] == {"queryguard.kind": "relationship-load"}
    context = first["contexts"]["queryguard"]
    assert context["name"] == "job"
    assert context["statement_count"] == 1 + CUSTOMERS
    assert context["file"] == "tests/inapp/test_reporters.py"
    assert context["message"] == finding.message
    # No method and no route: a track() unit is grouped by its kind alone.
    assert budget["fingerprint"] == ["queryguard", "budget", "", ""]
    assert budget["tags"] == {"queryguard.kind": "budget"}


def test_sentry_reporter_checks_the_level(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_sentry(monkeypatch)
    with pytest.raises(ValueError, match="level must be one of"):
        queryguard.sentry_reporter(level="warn")


def test_the_same_finding_keeps_its_sentry_fingerprint(
    watched: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _fake_sentry(monkeypatch)
    reporter = queryguard.sentry_reporter()
    for name in ("first", "second"):
        with queryguard.track(name, report=reporter):
            _lazy_loop(watched)
    assert len(sent) == 2
    assert sent[0]["fingerprint"] == sent[1]["fingerprint"]
    assert sent[0]["level"] == "warning"


@pytest.mark.parametrize(
    ("factory", "module", "package"),
    [
        ("opentelemetry_reporter", "opentelemetry.trace", "opentelemetry-api"),
        ("sentry_reporter", "sentry_sdk", "sentry-sdk"),
    ],
)
def test_a_missing_library_fails_when_the_reporter_is_made(
    monkeypatch: pytest.MonkeyPatch, factory: str, module: str, package: str
) -> None:
    monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(ImportError, match=f"pip install {package}"):
        getattr(queryguard, factory)()


def test_a_failing_reporter_does_not_break_the_unit(
    watched: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentry_sdk = ModuleType("sentry_sdk")

    def capture_message(message: str, **kwargs: Any) -> None:
        raise ConnectionError("sentry is down")

    sentry_sdk.capture_message = capture_message  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", sentry_sdk)
    caplog.set_level(logging.WARNING, logger="pytest_queryguard.app")
    with queryguard.track(report=[queryguard.sentry_reporter(), "log"]) as report:
        _lazy_loop(watched)
    assert report.findings
    assert "sentry is down" in caplog.text
    assert "Order.customer was lazy-loaded" in caplog.text
