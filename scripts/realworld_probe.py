"""A pytest plugin that records what pytest-queryguard reports about itself.

Used by .github/workflows/realworld.yml with ``-p realworld_probe`` (the file
is copied to a directory on PYTHONPATH). It appends one JSON object per line
to the file named by ``QUERYGUARD_PROBE_OUT``: every ``QueryGuardInternalWarning``
(a bug in the plugin) and every pytest internal error. Read from the hook
rather than from the terminal output, which a project may silence with
``--disable-warnings`` or ``-p no:warnings``.

Standard library and pytest only; does nothing without the variable.
"""

from __future__ import annotations

import json
import os
import warnings
from typing import Any

INTERNAL = "QueryGuardInternalWarning"


def _write(record: dict[str, Any]) -> None:
    path = os.environ.get("QUERYGUARD_PROBE_OUT")
    if not path:
        return
    # one short line per write in append mode, so pytest-xdist workers can
    # share the file
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def pytest_warning_recorded(
    warning_message: warnings.WarningMessage,
    when: str,
    nodeid: str,
    location: tuple[str, int, str] | None,
) -> None:
    if warning_message.category.__name__ != INTERNAL:
        return
    _write(
        {
            "event": "internal-warning",
            "message": str(warning_message.message),
            "file": warning_message.filename,
            "line": warning_message.lineno,
            "nodeid": nodeid,
            "when": when,
        }
    )


def pytest_internalerror(excrepr: object, excinfo: object) -> None:
    _write({"event": "internal-error", "message": str(excrepr)[-2000:]})
