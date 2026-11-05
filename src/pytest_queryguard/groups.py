"""The same finding in several tests, for one line each in the terminal summary.

A finding in a shared helper or fixture fails every test that runs it, and
each test prints the same block. The summary names the finding once, with the
number of tests and the first of them, so the cause is fixed once. Findings
are grouped by kind and label (``relationship-load Order.customer``), the
pattern an ignore rule or ``allow_nplusone`` would use, wherever they are.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .findings import Finding

_LABEL_LIMIT = 100


@dataclass(slots=True)
class Group:
    tests: int
    count: int
    first: str  # the smallest node id, so the line does not depend on the order


# (kind, label) -> group
Groups = dict[tuple[str, str], Group]


def add(groups: Groups, findings: list[Finding], nodeid: str) -> None:
    """Count the findings one test reported, each (kind, label) once per test."""
    seen: dict[tuple[str, str], int] = {}
    for finding in findings:
        key = (finding.kind, finding.label)
        seen[key] = seen.get(key, 0) + finding.count
    for key, count in seen.items():
        _merge(groups, key, Group(1, count, nodeid))


def _merge(groups: Groups, key: tuple[str, str], other: Group) -> None:
    group = groups.get(key)
    if group is None:
        groups[key] = Group(other.tests, other.count, other.first)
        return
    group.tests += other.tests
    group.count += other.count
    group.first = min(group.first, other.first)


def to_wire(groups: Groups) -> list[list[Any]]:
    """The groups as plain data a pytest-xdist worker can send."""
    return [[k, label, g.tests, g.count, g.first] for (k, label), g in groups.items()]


def merge_wire(groups: Groups, wire: list[list[Any]]) -> None:
    for kind, label, tests, count, first in wire:
        _merge(groups, (kind, label), Group(int(tests), int(count), str(first)))


def summary_lines(groups: Groups) -> list[str]:
    """One line per finding seen in two tests or more, the most tests first."""
    shared = [(key, g) for key, g in groups.items() if g.tests > 1]
    shared.sort(key=lambda item: (-item[1].tests, item[0]))
    lines = []
    for (kind, label), group in shared:
        label = " ".join(label.split())
        if len(label) > _LABEL_LIMIT:
            label = label[:_LABEL_LIMIT] + "*"
        lines.append(
            f"{group.tests:>5} tests  {kind}  {label}  "
            f"({group.count} times in total, first in {group.first})"
        )
    return lines
