"""The static check measured on the accuracy corpus (see ``lint_accuracy.py``).

Every disagreement with a verdict must be documented in ``MISSES``, and every
documented one must still disagree, so a change in the lint, good or bad,
changes this test and the numbers in docs/static-check.md.
"""

from __future__ import annotations

from . import lint_accuracy, runner

SCENARIOS = runner.load()


def test_the_lint_disagrees_only_where_documented() -> None:
    score = lint_accuracy.measure(SCENARIOS)
    unexplained = [
        (d.scenario, d.labels) for d in score.disagreements if d.reason is None
    ]
    assert unexplained == []
    loaded = {s.name for s in SCENARIOS}
    stale = sorted(
        set(lint_accuracy.MISSES) & loaded - {d.scenario for d in score.disagreements}
    )
    assert stale == [], "documented in MISSES but the lint now agrees"
    # no false positives: a lint people run in CI must not cry wolf
    assert score.fp == 0
    assert score.left_out == [
        ("clean_97_n_plus_one_only_in_fixture", "Customer.orders")
    ]
