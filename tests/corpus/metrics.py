"""Precision and recall from corpus results, per finding kind and per scenario."""

from __future__ import annotations

from dataclasses import dataclass, field

from .runner import Result, Scenario, scope


@dataclass
class Tally:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None


@dataclass
class Mismatch:
    scenario: str
    verdict: str
    expected: frozenset[str]
    observed: frozenset[str]
    known: str | None  # the documented reason, or None for an unexplained mismatch


@dataclass
class Score:
    kinds: dict[str, Tally] = field(default_factory=dict)
    # Scenario level: a BUG is found when any in-scope finding exists, a CLEAN
    # scenario is clean when there is none.
    bug_found: int = 0
    bug_missed: int = 0
    clean_passed: int = 0
    clean_flagged: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)

    @property
    def scenarios(self) -> int:
        return self.bug_found + self.bug_missed + self.clean_passed + self.clean_flagged


# Configurations that switch several opt-in rules on at once.
COMPOSITE = {
    "all-opt-in": (
        "small-repeat",
        "write-loop",
        "column-load-2",
        "unused-eager-load",
        "unused-columns",
    )
}


def known_reason(scenario: Scenario, config: str) -> str | None:
    """Why the plugin may disagree with the verdict in this configuration.

    A scenario lists configurations in ``KNOWN`` (``"*"`` for all of them, an
    explicit ``None`` for one where the limitation does not apply). For a
    composite configuration without its own entry, the reason of any rule it
    switches on counts.
    """
    known = scenario.known
    if config in known:
        return known[config]
    for part in COMPOSITE.get(config, ()):
        if known.get(part):
            return known[part]
    return known.get("*")


def score(config: str, scenarios: list[Scenario], results: dict[str, Result]) -> Score:
    """Compare what the tool reported with the verdicts, within the config's scope."""
    kinds_in_scope = scope(config)
    out = Score(kinds={kind: Tally() for kind in sorted(kinds_in_scope)})
    for scenario in scenarios:
        expected = scenario.kinds & kinds_in_scope
        observed = results[scenario.name].kinds & kinds_in_scope
        for kind in kinds_in_scope:
            tally = out.kinds[kind]
            if kind in expected and kind in observed:
                tally.tp += 1
            elif kind in observed:
                tally.fp += 1
            elif kind in expected:
                tally.fn += 1
        bug = bool(expected)
        if bug and observed:
            out.bug_found += 1
        elif bug:
            out.bug_missed += 1
        elif observed:
            out.clean_flagged += 1
        else:
            out.clean_passed += 1
        if expected != observed:
            out.mismatches.append(
                Mismatch(
                    scenario.name,
                    scenario.verdict,
                    expected,
                    observed,
                    known_reason(scenario, config),
                )
            )
    return out
