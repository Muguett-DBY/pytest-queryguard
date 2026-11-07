"""pytest plugin for one head-to-head corpus session (``-p session_plugin``).

``run_corpus.py`` copies this file next to the corpus session and loads it
with ``-p``. It hooks the tool under measurement up the way that tool's own
documentation says to (``H2H_TOOL``) and appends one JSON line per test phase
that did not pass, and one per finished test, to ``H2H_OUT``. Lines are
appended as they happen, so a session that crashes half way still leaves the
verdicts of the scenarios it ran.

Only standard pytest and the tool's public API are used here: this file runs
in an environment where the tool is installed, never in the project's tests.
"""

from __future__ import annotations

import json
import os

import pytest

TOOL = os.environ.get("H2H_TOOL", "none")
OUT = os.environ["H2H_OUT"]


def _name(nodeid: str) -> str:
    return nodeid.split("[", 1)[-1].rstrip("]")


def _write(entry: dict[str, object]) -> None:
    with open(OUT, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call" or report.failed:
        _write(
            {
                "scenario": _name(report.nodeid),
                "when": report.when,
                "outcome": report.outcome,
                "longrepr": "" if report.passed else str(report.longrepr),
            }
        )


# pytest-querycount and n-plus-one-sniffer check the tests that carry their
# ``no_n_plus_one`` marker (each tool has its own marker of that name), with
# the tool's default threshold. Every corpus scenario gets it.
if TOOL in ("querycount", "sniffer"):

    def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
        for item in items:
            item.add_marker(pytest.mark.no_n_plus_one)


if TOOL == "sniffer":
    from sqlalchemy.engine import Engine

    @pytest.fixture
    def nplusone_engine(prepared: object) -> type[Engine]:
        """The engine n-plus-one-sniffer watches.

        Its plugin attaches a ``before_cursor_execute`` listener to whatever
        this returns. The scenarios create their own engines (some inside the
        code under test), so this is the ``Engine`` class: every engine. It
        depends on the scenario's setup fixture so that the sniffer starts
        after the seed data is written, as a project's engine fixture would.
        """
        return Engine


if TOOL == "nplusone":
    # nplusone's documented use outside a web framework: import the ORM
    # extension (it patches SQLAlchemy) and run the code in a Profiler, which
    # raises NPlusOneError. Only the test call is profiled, not the setup.
    import nplusone.ext.sqlalchemy  # noqa: F401
    from nplusone.core import profiler

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(item: pytest.Item) -> object:
        with profiler.Profiler():
            return (yield)
