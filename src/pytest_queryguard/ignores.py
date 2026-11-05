from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase

from .findings import (
    KIND_CARTESIAN_LOAD,
    KIND_COLUMN_LOAD,
    KIND_MISSING_INDEX,
    KIND_RELATIONSHIP,
    KIND_REPEAT,
    KIND_UNUSED_EAGER_LOAD,
    KIND_WRITE_LOOP,
    Finding,
)
from .tablenames import table_matches

KINDS = (
    KIND_RELATIONSHIP,
    KIND_REPEAT,
    KIND_COLUMN_LOAD,
    KIND_WRITE_LOOP,
    KIND_UNUSED_EAGER_LOAD,
    KIND_CARTESIAN_LOAD,
    KIND_MISSING_INDEX,
)
_OPTION = re.compile(r"\s+(file|function)=(\S+)$")


@dataclass(frozen=True, slots=True)
class IgnoreRule:
    """One pattern for findings to leave out of the report.

    ``kind``, ``label``, ``file`` and ``function`` are ``fnmatch`` patterns
    (case-sensitive) for the fields of a ``Finding`` of the same names, and a
    label also matches itself written out exactly, and for ``missing-index``
    any spelling of the table name (see ``tablenames``); None
    for ``file`` or ``function`` means any value, including an unknown one.
    """

    kind: str
    label: str
    file: str | None = None
    function: str | None = None

    def matches(self, finding: Finding) -> bool:
        if not fnmatchcase(finding.kind, self.kind):
            return False
        # A label copied from the report matches even when it holds [ or ],
        # which fnmatch reads as a character class (ARRAY[?, ?], [dbo].[t]).
        if not self._label_matches(finding):
            return False
        if self.file is not None and not (
            finding.file is not None and fnmatchcase(finding.file, self.file)
        ):
            return False
        return self.function is None or (
            finding.function is not None
            and fnmatchcase(finding.function, self.function)
        )

    def _label_matches(self, finding: Finding) -> bool:
        if finding.label == self.label or fnmatchcase(finding.label, self.label):
            return True
        # A table is also matched by any spelling of its name (bare, schema
        # qualified, quoted or not), as in queryguard_index_ignore_tables.
        return finding.table is not None and table_matches(self.label, *finding.table)

    def __str__(self) -> str:
        text = f"{self.kind} {self.label}"
        if self.file is not None:
            text += f" file={self.file}"
        if self.function is not None:
            text += f" function={self.function}"
        return text


def check_kind(kind: str) -> None:
    """Raise ValueError when no finding kind can match the pattern."""
    if not any(fnmatchcase(known, kind) for known in KINDS):
        raise ValueError(
            f"{kind!r} matches no finding kind (kinds: {', '.join(KINDS)})"
        )


def parse_rule(line: str) -> IgnoreRule:
    """Parse ``KIND LABEL [file=GLOB] [function=GLOB]``.

    The label is everything after the kind, so it may contain spaces (a
    normalized statement). ``file=`` and ``function=`` are read from the end.
    """
    rest = line.strip()
    options: dict[str, str] = {}
    while (match := _OPTION.search(rest)) is not None:
        options.setdefault(match.group(1), match.group(2))
        rest = rest[: match.start()]
    kind, _, label = rest.strip().partition(" ")
    label = label.strip()
    if not kind or not label:
        raise ValueError(
            f"expected 'KIND LABEL [file=GLOB] [function=GLOB]', got {line.strip()!r}"
        )
    check_kind(kind)
    return IgnoreRule(kind, label, options.get("file"), options.get("function"))


def parse_rules(lines: Iterable[str]) -> tuple[IgnoreRule, ...]:
    """The rules of an ini list; blank lines and lines starting with # are skipped."""
    return tuple(
        parse_rule(line)
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    )


def apply_rules(
    findings: Sequence[Finding], rules: Sequence[IgnoreRule]
) -> tuple[list[Finding], set[IgnoreRule]]:
    """The findings no rule matches, and the rules that matched at least one."""
    kept: list[Finding] = []
    used: set[IgnoreRule] = set()
    for finding in findings:
        hits = [rule for rule in rules if rule.matches(finding)]
        if hits:
            used.update(hits)
        else:
            kept.append(finding)
    return kept, used
