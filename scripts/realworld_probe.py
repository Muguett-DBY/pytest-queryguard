"""A pytest plugin that records what pytest-queryguard reports about itself.

Used by .github/workflows/realworld.yml with ``-p realworld_probe`` (the file
is copied to a directory on PYTHONPATH). It appends one JSON object per line
to the file named by ``QUERYGUARD_PROBE_OUT``: every ``QueryGuardInternalWarning``
(a bug in the plugin) and every pytest internal error.

Internal warnings arrive two ways. Inside a test (and collection), pytest's
warnings plugin captures them and calls ``pytest_warning_recorded``. Outside
its windows (``pytest_unconfigure``, or with ``-p no:warnings``) they go to
``warnings.showwarning``, which the probe wraps. With pytest-xdist a worker's
warning is recorded by the worker; the controller's replay of it is skipped.

Standard library and pytest only; does nothing without the variable.
"""

from __future__ import annotations

import json
import os
import warnings
from typing import Any, TextIO

import pytest

INTERNAL = "QueryGuardInternalWarning"

_config: pytest.Config | None = None


def _write(record: dict[str, Any]) -> None:
    path = os.environ.get("QUERYGUARD_PROBE_OUT")
    if not path:
        return
    # one short line per write in append mode, so pytest-xdist workers can
    # share the file
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _record(message: Warning | str, filename: str, lineno: int, nodeid: str) -> None:
    _write(
        {
            "event": "internal-warning",
            "message": str(message),
            "file": filename,
            "line": lineno,
            "nodeid": nodeid,
        }
    )


def pytest_configure(config: pytest.Config) -> None:
    global _config
    _config = config


def _is_xdist_controller() -> bool:
    # asked late: xdist registers its dsession plugin in its own pytest_configure
    return (
        _config is not None
        and _config.pluginmanager.hasplugin("dsession")
        and not hasattr(_config, "workerinput")
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    # Not in pytest_configure: pytest runs that hook inside catch_warnings(),
    # which would undo the filter and the wrapper when it returns.
    try:
        from pytest_queryguard import QueryGuardInternalWarning
    except ImportError:
        return
    # outside the warnings plugin's windows, show every occurrence
    warnings.simplefilter("always", QueryGuardInternalWarning)
    original = warnings.showwarning

    def showwarning(
        message: Warning | str,
        category: type[Warning],
        filename: str,
        lineno: int,
        file: TextIO | None = None,
        line: str | None = None,
    ) -> None:
        if category.__name__ == INTERNAL:
            _record(message, filename, lineno, "")
        original(message, category, filename, lineno, file, line)

    warnings.showwarning = showwarning


def pytest_warning_recorded(
    warning_message: warnings.WarningMessage,
    when: str,
    nodeid: str,
    location: tuple[str, int, str] | None,
) -> None:
    if warning_message.category.__name__ != INTERNAL:
        return
    if nodeid and _is_xdist_controller():
        return  # replayed from a worker, which recorded it already
    _record(
        warning_message.message,
        warning_message.filename,
        warning_message.lineno,
        nodeid,
    )


def pytest_internalerror(excrepr: object, excinfo: object) -> None:
    _write({"event": "internal-error", "message": str(excrepr)[-2000:]})
