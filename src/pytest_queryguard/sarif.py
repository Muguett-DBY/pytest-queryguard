"""SARIF 2.1.0 output for GitHub code scanning (``--queryguard-sarif=PATH``).

One rule per finding kind, one result per finding identity. Findings the
baseline accepts are left out of the file: GitHub's SARIF documentation does
not describe ``suppressions``, so leaving them out is the only way that is
certain to keep them from showing up as alerts.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, NamedTuple

from .fileio import atomic_write_text
from .findings import (
    KIND_CARTESIAN_LOAD,
    KIND_COLUMN_LOAD,
    KIND_MISSING_INDEX,
    KIND_RELATIONSHIP,
    KIND_REPEAT,
    KIND_UNUSED_EAGER_LOAD,
    KIND_WRITE_LOOP,
    Finding,
    docs_url,
)

SARIF_VERSION = "2.1.0"
SCHEMA_URI = "https://json.schemastore.org/sarif-2.1.0.json"
INFORMATION_URI = "https://github.com/MaciejZiel/pytest-queryguard"
FINGERPRINT_KEY = "queryguardIdentity/v1"
LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

Identity = tuple[str, str, str | None, str | None]
Collected = dict[Identity, tuple[Finding, str]]


class _Rule(NamedTuple):
    name: str
    short: str
    full: str
    fix: str


_RULES: dict[str, _Rule] = {
    KIND_RELATIONSHIP: _Rule(
        "RelationshipLoad",
        "A relationship is lazy-loaded once per row (N+1)",
        "A relationship was lazy-loaded for many parent rows, one query each. "
        "The queries grow with the number of rows.",
        "Load the relationship with the parent query: joinedload() for a "
        "many-to-one or one-to-one relationship, selectinload() for a collection.",
    ),
    KIND_REPEAT: _Rule(
        "RepeatedStatement",
        "The same statement runs many times with different parameters",
        "One statement ran many times from one source line, usually in a loop "
        "that fetches one row per iteration.",
        "Fetch all rows in one query, for example WHERE id IN (...), instead of "
        "querying in a loop.",
    ),
    KIND_COLUMN_LOAD: _Rule(
        "ColumnLoad",
        "Deferred or expired columns are loaded instance by instance",
        "Instances loaded their deferred columns, or columns expired by "
        "commit(), expire() or refresh(), one query each.",
        "Use undefer(Model.column) in the query (or drop defer(), load_only() "
        "or deferred()); for expired attributes create the Session with "
        "expire_on_commit=False or reload the instances with one query.",
    ),
    KIND_WRITE_LOOP: _Rule(
        "WriteLoop",
        "Rows are written one statement at a time",
        "The same INSERT, UPDATE or DELETE ran row by row from one source line "
        "where one statement could write many rows.",
        "Write the rows together: session.add_all() and one flush, a bulk "
        "insert() or update() with a list of parameter sets, or one "
        "DELETE ... WHERE id IN (...).",
    ),
    KIND_UNUSED_EAGER_LOAD: _Rule(
        "UnusedEagerLoad",
        "A relationship is eager-loaded and never read (experimental)",
        "A query loaded a relationship with selectinload(), joinedload(), "
        "subqueryload() or immediateload() and the test never read it.",
        "Remove the eager option from the query, or load the relationship "
        "only in the queries whose result uses it.",
    ),
    KIND_CARTESIAN_LOAD: _Rule(
        "CartesianLoad",
        "One query joins two collections side by side",
        "A SELECT loads two collections with joinedload() where neither is on "
        "the path of the other, so each parent row repeats per combination of "
        "their items and the rows grow as their product.",
        "Load all but one of the collections with selectinload(): one more "
        "query each, and no repeated rows.",
    ),
    KIND_MISSING_INDEX: _Rule(
        "MissingIndex",
        "A query reads a table in full where an index could be used",
        "EXPLAIN shows a sequential scan that filters or joins on columns "
        "without a usable index.",
        "Add an index on the filtered or joined columns (see the finding for "
        "the columns), or ignore the table if it is small.",
    ),
}
KINDS = tuple(_RULES)


def rule_id(kind: str) -> str:
    return f"queryguard/{kind}"


# --- collecting findings over a session ---


def _rank(finding: Finding) -> tuple[int, str]:
    # the same finding from several tests: keep the earliest line, then the
    # smallest message, so the result does not depend on the order tests ran in
    return (finding.line if finding.line is not None else 1 << 31, finding.message)


def _peak(finding: Finding) -> int:
    return finding.count if finding.peak is None else finding.peak


def add(store: Collected, finding: Finding, level: str) -> None:
    """Remember a reported finding; one entry per identity."""
    key = finding.identity
    if key not in store:
        store[key] = (finding, level)
        return
    old, old_level = store[key]
    best = finding if _rank(finding) < _rank(old) else old
    merged = Finding(
        kind=best.kind,
        label=best.label,
        file=best.file,
        function=best.function,
        message=best.message,
        severity=best.severity,
        line=best.line,
        count=old.count + finding.count,
        tests=old.tests + finding.tests,
        peak=max(_peak(old), _peak(finding)),
    )
    store[key] = (merged, LEVEL_ERROR if LEVEL_ERROR in (level, old_level) else level)


def to_wire(store: Collected) -> list[dict[str, Any]]:
    """The store as plain data a pytest-xdist worker can send."""
    return [
        {
            "kind": f.kind,
            "label": f.label,
            "file": f.file,
            "function": f.function,
            "message": f.message,
            "severity": f.severity,
            "line": f.line,
            "count": f.count,
            "tests": f.tests,
            "peak": f.peak,
            "level": level,
        }
        for f, level in store.values()
    ]


def merge_wire(store: Collected, wire: Iterable[Mapping[str, Any]]) -> None:
    for entry in wire:
        finding = Finding(
            kind=entry["kind"],
            label=entry["label"],
            file=entry["file"],
            function=entry["function"],
            message=entry["message"],
            severity=entry["severity"],
            line=entry["line"],
            count=entry["count"],
            tests=entry.get("tests", 1),
            peak=entry.get("peak"),
        )
        add(store, finding, entry["level"])


# --- the document ---


def fingerprint(identity: Identity) -> str:
    """A stable hash of the identity: same finding, same value, on every run."""
    kind, label, file, function = identity
    text = "\x1f".join((kind, label, file or "", function or ""))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def repo_path(file: str | None) -> str | None:
    """The rootdir-relative path for a result location, or None.

    Files outside the rootdir are reported by the interpreter as absolute
    paths, which mean nothing in a repository.
    """
    if not file:
        return None
    if PurePosixPath(file).is_absolute() or PureWindowsPath(file).is_absolute():
        return None
    return PurePosixPath(file.replace("\\", "/")).as_posix()


def workspace_prefix(rootdir: Path | None, workspace: str | None) -> str:
    """Path of the rootdir below the repository root, as posix text ("" if none).

    GitHub reads ``artifactLocation.uri`` relative to the repository root. The
    findings carry rootdir-relative paths, so when pytest runs in a
    subdirectory the prefix has to be added. ``workspace`` is the value of
    ``GITHUB_WORKSPACE``; both paths are resolved, so symlinks on either side
    do not matter. Rootdir outside the workspace (or no workspace): "".
    """
    if rootdir is None or not workspace or not workspace.strip():
        return ""
    try:
        base = Path(os.path.realpath(workspace.strip()))
        relative = Path(os.path.realpath(rootdir)).relative_to(base)
    except (ValueError, OSError):
        return ""
    return "" if relative == Path() else relative.as_posix()


def _rule(kind: str) -> dict[str, Any]:
    name, short, full, fix = _RULES[kind]
    text = f"{full}\n\nFix: {fix}"
    return {
        "id": rule_id(kind),
        "name": name,
        "shortDescription": {"text": short},
        "fullDescription": {"text": full},
        "help": {"text": text, "markdown": f"{full}\n\n**Fix:** {fix}"},
        "helpUri": docs_url(kind),
        "defaultConfiguration": {"level": LEVEL_ERROR},
        "properties": {"tags": ["performance", "database"], "kind": kind},
    }


def _message(finding: Finding) -> str:
    lines = [line.removeprefix("  ") for line in finding.message.splitlines()]
    text = "\n".join(lines).strip() or finding.label
    if finding.tests <= 1:
        return text
    # The result merges the finding from several tests: say how the counts
    # relate, because the text below is the one test that has the earliest line.
    return (
        f"Seen in {finding.tests} tests: {finding.count} times in total, up to "
        f"{_peak(finding)} times in one test. The test with the earliest line:\n"
        f"{text}"
    )


def _properties(finding: Finding) -> dict[str, Any]:
    properties: dict[str, Any] = {"count": finding.count, "label": finding.label}
    if finding.tests > 1:
        properties["tests"] = finding.tests
        properties["peakCount"] = _peak(finding)
    return properties


def _result(finding: Finding, level: str, prefix: str = "") -> dict[str, Any] | None:
    path = repo_path(finding.file)
    if path is None:
        return None  # GitHub shows a result only with a location
    if prefix:
        path = f"{prefix}/{path}"
    region: dict[str, Any] = {"startLine": finding.line if finding.line else 1}
    location: dict[str, Any] = {
        "physicalLocation": {
            "artifactLocation": {"uri": path},
            "region": region,
        }
    }
    if finding.function:
        location["logicalLocations"] = [{"name": finding.function, "kind": "function"}]
    digest = fingerprint(finding.identity)
    return {
        "ruleId": rule_id(finding.kind),
        "ruleIndex": KINDS.index(finding.kind),
        "level": level,
        "message": {"text": _message(finding)},
        "locations": [location],
        "partialFingerprints": {
            # GitHub de-duplicates on primaryLocationLineHash; the upload action
            # computes it from the source line only when it is missing
            "primaryLocationLineHash": f"{digest[:32]}:1",
            FINGERPRINT_KEY: digest,
        },
        "properties": _properties(finding),
    }


def build(
    store: Collected, version: str, prefix: str = ""
) -> tuple[dict[str, Any], int]:
    """(SARIF document, number of findings left out for lack of a location).

    ``prefix`` is put before every location URI (see ``workspace_prefix``); the
    fingerprints are computed from the rootdir-relative identity and do not
    depend on it.
    """
    results: list[dict[str, Any]] = []
    omitted = 0
    for identity in sorted(store, key=_sort_key):
        finding, level = store[identity]
        if finding.kind not in _RULES:
            omitted += 1
            continue
        result = _result(finding, level, prefix)
        if result is None:
            omitted += 1
        else:
            results.append(result)
    document = {
        "$schema": SCHEMA_URI,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "pytest-queryguard",
                        "version": version,
                        "informationUri": INFORMATION_URI,
                        "rules": [_rule(kind) for kind in KINDS],
                    }
                },
                "columnKind": "utf16CodeUnits",
                "results": results,
            }
        ],
    }
    return document, omitted


def _sort_key(identity: Identity) -> tuple[str, str, str, str, bool, bool]:
    kind, label, file, function = identity
    return (kind, label, file or "", function or "", file is None, function is None)


def dumps(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def write(path: Path, document: Mapping[str, Any]) -> None:
    atomic_write_text(path, dumps(document))
