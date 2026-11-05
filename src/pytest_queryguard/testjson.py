"""Per-test query counts and findings as JSON (``--queryguard-json``).

The file is the input for comparing two runs: a pull request against its base
branch, or today's counts against the ones committed last time. One entry per
test node id:

    {
      "statements": 12,             # every statement the test ran
      "by_verb": {"select": 10, "insert": 2},
      "db_time": 0.0042,            # seconds, or null when no statement was timed
      "truncated": false,           # by_verb counts only the stored statements
      "findings": [{"kind": ..., "label": ..., "count": ..., "file": ...,
                    "function": ..., "line": ..., "stack": [...]}]
    }

``stack`` lists the application frames that called ``file:line``, innermost
first, each as ``{"file", "function", "line"}`` (``queryguard_stack_depth``).

Findings are the ones the test found after ignores; those in the baseline
have ``"baselined": true``, so a file written while updating the baseline
still lists them. ``complete`` is false when the run was interrupted or
measurement stopped, so a consumer should not treat missing tests as gone.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .collector import QueryCollector
from .fileio import atomic_write_text
from .findings import Finding
from .sqltext import _WORD, QUOTED, is_transaction_control, skip_prefix

FORMAT_VERSION = 1

_VERBS = {
    "SELECT": "select",
    "WITH": "select",
    "INSERT": "insert",
    "UPDATE": "update",
    "DELETE": "delete",
}
_QUOTED = re.compile(QUOTED, re.VERBOSE | re.DOTALL)
_MAIN_VERBS = frozenset(("SELECT", "INSERT", "UPDATE", "DELETE"))

# node id -> entry
Tests = dict[str, dict[str, Any]]


def verb(sql: str) -> str:
    """select, insert, update, delete, transaction or other."""
    start = skip_prefix(sql)
    word = _WORD.match(sql, start) if start is not None else None
    if word is None:
        return "other"
    first = word[0].upper()
    if first == "WITH":
        return _with_verb(sql)
    found = _VERBS.get(first)
    if found is not None:
        return found
    return "transaction" if is_transaction_control(sql) else "other"


def _with_verb(sql: str) -> str:
    """The verb of the statement after a WITH: the first verb outside parentheses.

    ``WITH moved AS (DELETE ... RETURNING *) INSERT INTO b SELECT ...`` is an
    insert; the statements of the CTEs are inside parentheses.
    """
    text = _QUOTED.sub(" ", sql)
    depth = 0
    for match in re.finditer(r"[()]|[A-Za-z]+", text):
        token = match[0]
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(depth - 1, 0)
        elif depth == 0 and token.upper() in _MAIN_VERBS:
            return _VERBS[token.upper()]
    return "select"


def _entry(tests: Tests, nodeid: str) -> dict[str, Any]:
    entry = tests.get(nodeid)
    if entry is None:
        entry = tests[nodeid] = {
            "statements": 0,
            "by_verb": {},
            "db_time": None,
            "truncated": False,
            "findings": [],
        }
    return entry


def record_counts(tests: Tests, nodeid: str, collector: QueryCollector) -> None:
    """The counts of a finished test; a test run again keeps its last run."""
    by_verb: dict[str, int] = {}
    for statement in collector.statements:
        name = verb(statement.sql)
        by_verb[name] = by_verb.get(name, 0) + 1
    entry = _entry(tests, nodeid)
    entry["statements"] = collector.statement_count
    entry["by_verb"] = dict(sorted(by_verb.items()))
    timed = any(s.duration is not None for s in collector.statements)
    entry["db_time"] = collector.db_time if timed or not collector.statements else None
    entry["truncated"] = collector.statement_count > len(collector.statements)
    entry["findings"] = []  # filled by record_findings for this run


def record_findings(
    tests: Tests,
    nodeid: str,
    findings: list[Finding],
    baseline: frozenset[Any] | None = None,
) -> None:
    """The findings of a test after ignores, those in ``baseline`` marked."""
    entry = _entry(tests, nodeid)
    entry["findings"].extend(
        {
            "kind": f.kind,
            "label": f.label,
            "count": f.count,
            "file": f.file,
            "function": f.function,
            "line": f.line,
            "stack": [
                {"file": file, "function": function, "line": line}
                for file, line, function in f.stack
            ],
            "baselined": baseline is not None and f.identity in baseline,
        }
        for f in findings
    )


def merge(tests: Tests, other: Tests) -> None:
    """Add a pytest-xdist worker's entries (already plain JSON data)."""
    for nodeid, entry in other.items():
        tests[str(nodeid)] = entry


def document(tests: Tests, version: str, complete: bool) -> dict[str, Any]:
    return {
        "format": FORMAT_VERSION,
        "tool": {"name": "pytest-queryguard", "version": version},
        "complete": complete,
        "tests": {nodeid: tests[nodeid] for nodeid in sorted(tests)},
    }


def write(path: Path, doc: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(doc, indent=2, sort_keys=False) + "\n")
