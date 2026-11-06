"""Ready-made ``report`` callables: OpenTelemetry spans and Sentry events.

Each imports its library when it is created, so a missing one fails at
startup rather than in a request. Like any ``report`` callable, an exception
inside one is logged and never reaches the request.
"""

from __future__ import annotations

import importlib
from typing import Any

from ..sarif import fingerprint
from ._core import Reporter, RequestReport


def _require(module: str, user: str, package: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ImportError(
            f"{user} needs the {package} package: pip install {package}"
        ) from exc


def _where(report: RequestReport) -> str:
    return report.route or report.path or report.name


def opentelemetry_reporter() -> Reporter:
    """Add each reported request to the current OpenTelemetry span.

    The span gets ``queryguard.*`` attributes (statement count, database
    time, number of findings, budget), one ``queryguard.finding`` event per
    finding and a ``queryguard.budget`` event when over budget. Nothing is
    added when no span is recording. Needs ``opentelemetry-api``.
    """
    trace = _require(
        "opentelemetry.trace", "opentelemetry_reporter()", "opentelemetry-api"
    )

    def report_to_span(report: RequestReport) -> None:
        span = trace.get_current_span()
        if not span.is_recording():
            return
        span.set_attributes(
            _attributes(
                {
                    "queryguard.statement_count": report.statement_count,
                    "queryguard.db_time": report.db_time,
                    "queryguard.findings": len(report.findings),
                    "queryguard.budget": report.budget,
                    "queryguard.over_budget": report.over_budget,
                }
            )
        )
        for finding in report.findings:
            span.add_event(
                "queryguard.finding",
                _attributes(
                    {
                        "queryguard.kind": finding.kind,
                        "queryguard.label": finding.label,
                        "queryguard.file": finding.file,
                        "queryguard.line": finding.line,
                        "queryguard.function": finding.function,
                        "queryguard.count": finding.count,
                        "queryguard.fingerprint": fingerprint(finding.identity),
                    }
                ),
            )
        if report.over_budget:
            span.add_event(
                "queryguard.budget",
                _attributes(
                    {
                        "queryguard.budget": report.budget,
                        "queryguard.statement_count": report.statement_count,
                    }
                ),
            )

    return report_to_span


def _attributes(values: dict[str, Any]) -> dict[str, Any]:
    """OpenTelemetry attributes cannot be None: leave those out."""
    return {key: value for key, value in values.items() if value is not None}


_SENTRY_LEVELS = ("fatal", "error", "warning", "info", "debug")


def sentry_reporter(*, level: str = "warning") -> Reporter:
    """Send each finding to Sentry as a message, one issue per finding.

    The fingerprint is ``["queryguard", <hash of the finding's identity>]``,
    the hash the SARIF file uses, so Sentry groups the same N+1 across
    requests whatever its count and line. A request over budget sends one
    more message, grouped by method and route (by method alone without one).
    Each message has a ``queryguard.kind`` tag and a ``queryguard`` context
    with the request. Needs ``sentry-sdk``.
    """
    if level not in _SENTRY_LEVELS:
        raise ValueError(
            f"level must be one of {', '.join(_SENTRY_LEVELS)}, got {level!r}"
        )
    sentry_sdk = _require("sentry_sdk", "sentry_reporter()", "sentry-sdk")

    def report_to_sentry(report: RequestReport) -> None:
        request = {
            "name": report.name,
            "method": report.method,
            "path": report.path,
            "route": report.route,
            "status": report.status,
            "statement_count": report.statement_count,
            "db_time": report.db_time,
            "budget": report.budget,
        }
        for finding in report.findings:
            sentry_sdk.capture_message(
                f"queryguard: {finding.kind} {finding.label}",
                level=level,
                fingerprint=["queryguard", fingerprint(finding.identity)],
                tags={"queryguard.kind": finding.kind},
                contexts={
                    "queryguard": {
                        **request,
                        "kind": finding.kind,
                        "label": finding.label,
                        "file": finding.file,
                        "line": finding.line,
                        "function": finding.function,
                        "count": finding.count,
                        "message": finding.message,
                    }
                },
            )
        if report.over_budget:
            sentry_sdk.capture_message(
                f"queryguard: {_where(report)} ran {report.statement_count} "
                f"statements, budget {report.budget}",
                level=level,
                fingerprint=[
                    "queryguard",
                    "budget",
                    report.method or "",
                    report.route or "",
                ],
                tags={"queryguard.kind": "budget"},
                contexts={"queryguard": request},
            )

    return report_to_sentry
