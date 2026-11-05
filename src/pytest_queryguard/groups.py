"""The same finding in several tests, for one line each in the terminal summary.

A finding in a shared helper or fixture is reported by every test that runs
it, and each test prints the same block. The summary names the finding once,
with the number of tests and of places in the code, so a cause shared by many
tests is fixed once. Findings are grouped by kind and label
(``relationship-load Order.customer``), the pattern an ignore rule or
``allow_nplusone`` would use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .findings import Finding

_LABEL_LIMIT = 100


@dataclass(slots=True)
class Group:
    # node id -> count in that test; a test run again (a rerun plugin) keeps
    # its last count instead of counting as another test
    tests: dict[str, int] = field(default_factory=dict)
    # (file, function) of the call sites the finding came from
    sites: set[tuple[str | None, str | None]] = field(default_factory=set)


# (kind, label) -> group
Groups = dict[tuple[str, str], Group]


def add(groups: Groups, findings: list[Finding], nodeid: str) -> None:
    """Note the findings one test reported."""
    counts: dict[tuple[str, str], int] = {}
    for finding in findings:
        key = (finding.kind, finding.label)
        counts[key] = counts.get(key, 0) + finding.count
        groups.setdefault(key, Group()).sites.add((finding.file, finding.function))
    for key, count in counts.items():
        groups[key].tests[nodeid] = count


def to_wire(groups: Groups) -> list[list[Any]]:
    """The groups as plain data a pytest-xdist worker can send."""
    return [
        [kind, label, sorted(g.tests.items()), sorted(g.sites, key=_site_key)]
        for (kind, label), g in groups.items()
    ]


def merge_wire(groups: Groups, wire: list[list[Any]]) -> None:
    """Add a worker's groups; nothing is merged when any entry is malformed."""
    parsed = [
        (
            (str(kind), str(label)),
            {str(nodeid): int(count) for nodeid, count in tests},
            {(file, function) for file, function in sites},
        )
        for kind, label, tests, sites in wire
    ]
    for key, tests, sites in parsed:
        group = groups.setdefault(key, Group())
        group.tests.update(tests)
        group.sites.update(sites)


def _site_key(site: tuple[str | None, str | None]) -> tuple[str, str]:
    return site[0] or "", site[1] or ""


def summary_lines(groups: Groups) -> list[str]:
    """One line per finding of two tests or more, the most tests first."""
    shared = [(key, g) for key, g in groups.items() if len(g.tests) > 1]
    shared.sort(key=lambda item: (-len(item[1].tests), item[0]))
    lines = []
    for (kind, label), group in shared:
        label = " ".join(label.split())
        if len(label) > _LABEL_LIMIT:
            label = label[:_LABEL_LIMIT] + "*"
        places = len(group.sites)
        where = "one place in the code" if places == 1 else f"{places} places"
        lines.append(
            f"{len(group.tests):>5} tests  {kind}  {label}  "
            f"({sum(group.tests.values())} times in total, from {where}; "
            f"e.g. {min(group.tests)})"
        )
    return lines
