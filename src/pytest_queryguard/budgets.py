"""The budget file: a query budget per test, written from a run's counts.

``queryguard_budgets = queryguard-budgets.json`` gives every test listed in
the file a budget, as if it had ``@pytest.mark.max_queries(n)``; a marker on
the test wins. ``--queryguard-update-budgets`` writes the counts of the run as
the new budgets, so a legacy suite locks today's numbers in one step and a
test that starts running more queries fails until the file is updated.

    {"format": 1, "budgets": {"tests/test_orders.py::test_list": 3, ...}}
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Any

from .fileio import atomic_write_text

FORMAT_VERSION = 1


class BudgetFileError(Exception):
    pass


def load(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BudgetFileError(
            f"budget file {path} does not exist; create it with "
            "--queryguard-update-budgets"
        ) from None
    except (OSError, ValueError) as exc:
        raise BudgetFileError(f"cannot read budget file {path}: {exc}") from None
    budgets = data.get("budgets") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("format") != FORMAT_VERSION
        or not isinstance(budgets, dict)
    ):
        raise BudgetFileError(
            f"{path} is not a pytest-queryguard budget file (format {FORMAT_VERSION})"
        )
    for nodeid, limit in budgets.items():
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise BudgetFileError(
                f"{path}: the budget of {nodeid} must be a non-negative integer, "
                f"got {limit!r}"
            )
    return dict(budgets)


def updated(
    before: Mapping[str, int],
    counts: Mapping[str, int],
    *,
    narrowed: bool,
    collected: AbstractSet[str] = frozenset(),
    marked: AbstractSet[str] = frozenset(),
) -> dict[str, int]:
    """The new budgets: the counts of the tests that ran.

    A test that was collected but has no count (skipped, failed, errored in
    setup) keeps its entry. A full run drops the entries of tests that were
    not collected (deleted or renamed); a narrowed run (-k, a path, --lf)
    keeps them. A test with its own max_queries marker has no entry.
    """
    if narrowed:
        result = dict(before)
    else:
        result = {k: v for k, v in before.items() if k in collected}
    result.update(counts)
    for nodeid in marked:
        result.pop(nodeid, None)
    return result


def write(path: Path, budgets: Mapping[str, int]) -> None:
    document = {
        "format": FORMAT_VERSION,
        "budgets": {nodeid: budgets[nodeid] for nodeid in sorted(budgets)},
    }
    atomic_write_text(path, json.dumps(document, indent=2) + "\n")


def describe(before: Mapping[str, int], after: Mapping[str, int]) -> str:
    """'N entries (a added, r removed, u raised, l lowered)'."""
    added = len(set(after) - set(before))
    removed = len(set(before) - set(after))
    common = set(before) & set(after)
    raised = sum(1 for k in common if after[k] > before[k])
    lowered = sum(1 for k in common if after[k] < before[k])
    return (
        f"{len(after)} {'entry' if len(after) == 1 else 'entries'} "
        f"({added} added, {removed} removed, "
        f"{raised} raised, {lowered} lowered)"
    )


def counts_of(tests: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    """node id -> statement count, from the per-test entries of the run."""
    return {nodeid: int(entry["statements"]) for nodeid, entry in tests.items()}
