from __future__ import annotations

from dataclasses import dataclass, field

# Finding kinds. They are part of the identity of a finding, so they never change.
KIND_RELATIONSHIP = "relationship-load"
KIND_REPEAT = "repeated-statement"
KIND_MISSING_INDEX = "missing-index"
KIND_COLUMN_LOAD = "column-load"
KIND_WRITE_LOOP = "write-loop"
KIND_UNUSED_EAGER_LOAD = "unused-eager-load"
KIND_CARTESIAN_LOAD = "cartesian-load"

SEVERITY_ERROR = "error"


@dataclass(frozen=True, slots=True, kw_only=True)
class IndexAdvice:
    """The index the advisor suggests for a ``missing-index`` finding.

    ``create_index`` is the statement and ``alembic`` the matching
    ``op.create_index`` call; both are None when no new index is suggested,
    and ``skipped`` says why (an index for these columns exists but is
    unusable). ``verified`` is None when hypopg did not check the suggestion,
    True when it says the planner would use the index and False when it would
    not. The costs are the planner's estimates for the whole statement, with
    sequential scans allowed, before and after adding the index.
    """

    create_index: str | None = None
    alembic: str | None = None
    columns: tuple[str, ...] = ()
    where: str | None = None
    skipped: str | None = None
    verified: bool | None = None
    cost_before: float | None = None
    cost_after: float | None = None
    size_bytes: int | None = None
    verification_error: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Finding:
    """One problem a detector found, with its report text already rendered.

    ``kind`` and ``label`` say what was found: for ``relationship-load`` the
    relationship (``Order.customer``), for ``repeated-statement`` the
    normalized statement, for ``column-load`` the class and the deferred
    attributes (``Child.blob``) or its cause (``Parent (expired)``), for ``write-loop`` the normalized
    INSERT, UPDATE or DELETE, for ``unused-eager-load`` the relationship
    (``Order.customer``), for ``missing-index`` the table. ``file`` and
    ``function`` say where it first happened: ``file`` is relative to pytest's
    rootdir, with forward slashes, or as the interpreter reports it when the
    file is outside the rootdir; both are None when the call site is unknown.

    ``(kind, label, file, function)`` is the identity: it stays the same from
    run to run, whatever the line numbers and counts, and whatever directory
    the suite runs from. ``line`` and ``count`` are details of one run.
    """

    kind: str
    label: str
    file: str | None
    function: str | None
    message: str
    severity: str = SEVERITY_ERROR
    line: int | None = None
    count: int = 1
    # Set when the same finding was merged over several tests (SARIF): how
    # many tests it came from, and the largest count in any one of them.
    # ``count`` is then the total. Not part of the identity.
    tests: int = 1
    peak: int | None = None
    # For ``missing-index`` findings, with the index advisor on; not part of
    # the identity.
    advice: IndexAdvice | None = None
    # For ``missing-index`` findings: (schema, table) as the plan names them,
    # which the label may leave out or quote; lets ignore patterns match any
    # spelling of the table. Not part of the identity.
    table: tuple[str | None, str] | None = field(default=None, compare=False)
    # The application frames that called the first location, innermost first,
    # as (file, line, function) with ``file`` shown like ``file`` above. Empty
    # when unknown or turned off (queryguard_stack_depth = 0). Not part of the
    # identity.
    stack: tuple[tuple[str, int, str], ...] = field(default=(), compare=False)

    @property
    def identity(self) -> tuple[str, str, str | None, str | None]:
        return (self.kind, self.label, self.file, self.function)
