"""The static check (``pytest_queryguard.lint``) measured on the accuracy corpus.

Each scenario module is linted on its own, with the models a project would
pass as ``--models``: the shared domain modules the scenario imports and the
scenario module itself (some define their own mapped classes). The ground
truth is the scenario's: a BUG scenario whose ``KINDS`` has relationship-load
is a positive, every other scenario (CLEAN, or a BUG of another kind) a
negative. A scenario counts as reported when the lint has any finding in it.

Findings in a scenario's ``setup`` are left out: ``setup`` is the test's
seeding code, which the corpus does not measure (``clean_97``), not the code
under test.

``MISSES`` documents every scenario where the lint disagrees with the
verdict, with the reason; ``tests/corpus/test_lint_accuracy.py`` keeps it
exact, so a change in the lint shows up as a changed measurement.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from pathlib import Path

from pytest_queryguard import lint

from . import kit
from .runner import Scenario

# Functions of a scenario whose findings are not measured.
OUT_OF_SCOPE = frozenset({"setup"})

_CROSS = "crosses a function: "
# scenario -> why the lint misses it (positives) or reports it (negatives)
MISSES: dict[str, str] = {
    "bug_03_serializer_nested": _CROSS + "the reads are in a serializer function",
    "bug_04_pydantic_from_attributes": _CROSS + "Pydantic model_validate reads them",
    "bug_06_fastapi_response_model": _CROSS + "FastAPI's response_model reads them",
    "bug_11_property_hides_query": _CROSS + "a @property reads the relationship",
    "bug_12_str_hides_query": _CROSS + "__str__ reads the relationship",
    "bug_13_repr_hides_query": _CROSS + "__repr__ reads the relationship",
    "bug_14_hybrid_python_side": _CROSS + "a hybrid property's Python side reads it",
    "bug_37_recursive_tree_walk": _CROSS
    + "a recursive function loops over its parameter",
    "bug_38_jinja_template_loop": "the read is in a Jinja template, not in Python",
    "bug_50_cascade_delete_loop": (
        "no read: session.delete() loads the cascade's children"
    ),
    "bug_89_ignore_block_leaks": _CROSS + "the loop is over a helper's result",
    "bug_90_parents_loaded_in_fixture": _CROSS + "the query runs in the fixture",
    "bug_93_nested_schema_serializer": _CROSS + "a nested schema reads them",
    "bug_95_selfref_tree_lazy_replies": _CROSS + "a recursive function reads them",
    "bug_122_generic_foreign_key_lazy": _CROSS + "a @property reads the relationship",
    "bug_125_service_loops_over_repository_result": (
        _CROSS + "the query is in a repository method, the loop in a service"
    ),
}


@dataclass
class Disagreement:
    scenario: str
    positive: bool  # the scenario is a relationship-load BUG
    labels: list[str]  # what the lint reported
    reason: str | None  # from MISSES, or None when unexplained


@dataclass
class LintScore:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    disagreements: list[Disagreement] = field(default_factory=list)
    left_out: list[tuple[str, str]] = field(default_factory=list)  # (scenario, label)

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def scenarios(self) -> int:
        return self.tp + self.fp + self.fn + self.tn


def positive(scenario: Scenario) -> bool:
    return scenario.verdict == kit.BUG and kit.REL in scenario.kinds


def model_modules(scenario: Scenario) -> list[str]:
    """What a project would pass as --models: the domains used, and the module."""
    domains = {
        value.__name__
        for value in vars(scenario.module).values()
        if isinstance(value, types.ModuleType)
        and value.__name__.startswith(f"{__package__}.domains.")
    }
    return [*sorted(domains), scenario.module.__name__]


def findings(scenario: Scenario) -> list[lint.Finding]:
    models = lint.load_models(model_modules(scenario))
    path = Path(str(scenario.module.__file__))
    source = path.read_text(encoding="utf-8")
    return lint.lint_source(source, path.name, models)


def measure(scenarios: list[Scenario]) -> LintScore:
    score = LintScore()
    for scenario in scenarios:
        labels: list[str] = []
        for finding in findings(scenario):
            if finding.function in OUT_OF_SCOPE:
                score.left_out.append((scenario.name, finding.relationship))
            else:
                labels.append(finding.relationship)
        expected, reported = positive(scenario), bool(labels)
        if expected and reported:
            score.tp += 1
        elif expected:
            score.fn += 1
        elif reported:
            score.fp += 1
        else:
            score.tn += 1
        if expected != reported:
            score.disagreements.append(
                Disagreement(
                    scenario.name,
                    expected,
                    sorted(set(labels)),
                    MISSES.get(scenario.name),
                )
            )
    return score
