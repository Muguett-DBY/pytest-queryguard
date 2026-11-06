from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from .detectors import (
    ColumnLoadFinding,
    Detection,
    RelationshipFinding,
    WriteLoopFinding,
)
from .events import (
    CAUSE_DEFERRED,
    CAUSE_REFRESH,
    Advice,
    CallSite,
    ScanEvent,
    ScanIssue,
    StatementEvent,
    Verification,
)
from .findings import (
    KIND_CARTESIAN_LOAD,
    KIND_COLUMN_LOAD,
    KIND_MISSING_INDEX,
    KIND_RELATIONSHIP,
    KIND_REPEAT,
    KIND_UNUSED_COLUMNS,
    KIND_UNUSED_EAGER_LOAD,
    KIND_WRITE_LOOP,
    Finding,
    IndexAdvice,
    Stack,
    docs_url,
)
from .indexes import table_label
from .listeners import Failure

_SQL_LIMIT = 200
_REPEAT_FIX = (
    "fix: fetch all rows in one query (WHERE id IN (...)) instead of in a loop"
)
_BATCHED_FIX = (
    "fix: this query already fetches several rows with IN (...), so it is the "
    "calls that repeat: call it once with all the ids, or combine the calls"
)
_IN_LIST = "IN (...)"


def _across(finding: Detection, noun: str) -> str:
    """ " across N different <noun>" when a per-test count spans several scopes."""
    scopes = getattr(finding, "scopes", 1)
    return f" across {scopes} different {noun}" if scopes > 1 else ""


def _in_one(finding: Detection, noun: str) -> str:
    return f" in one {noun}" if getattr(finding, "scoped", False) else ""


def _scope_hint(finding: Detection) -> list[str]:
    """The note for a count that adds up several Sessions, which may be requests."""
    if getattr(finding, "scopes", 1) <= 1:
        return []
    return [
        "  note: if each belongs to a separate request or unit of work, this is not a",
        "  loop; set queryguard_repeat_scope = session to count each Session on its",
        "  own (QueryLog.assert_no_nplusone: repeat_scope='session')",
    ]


_root: Path | None = None


def swap_root(new: Path | None) -> Path | None:
    """Set the directory paths are shown relative to; return the previous one.

    The plugin sets pytest's rootdir for the session, so messages built
    outside the plugin's own hooks (``QueryLog.assert_no_nplusone()``) show
    the same paths. Without it, they are relative to the working directory.
    """
    global _root
    old, _root = _root, new
    return old


def display_root() -> Path:
    return _root if _root is not None else Path.cwd()


def _display_path(filename: str, cwd: Path | None) -> str:
    base = cwd if cwd is not None else Path.cwd()
    path = Path(filename)
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return filename


def _location(site: CallSite, cwd: Path | None) -> str:
    return f"{_display_path(site.filename, cwd)}:{site.lineno} in {site.function}"


def _stack(site: CallSite | None, cwd: Path | None) -> Stack:
    """The callers of ``site`` as (file, line, function), innermost first."""
    if site is None:
        return ()
    return tuple(
        (_display_path(c.filename, cwd), c.lineno, c.function) for c in site.callers
    )


def _first_at(site: CallSite | None, stack: Stack, cwd: Path | None) -> list[str]:
    """The ``first at`` line, and a ``called from`` line for each caller."""
    where = "<unknown location>" if site is None else _location(site, cwd)
    return [
        f"  first at {where}",
        *(f"    called from {file}:{line} in {func}" for file, line, func in stack),
    ]


def kind_line(kind: str, label: str) -> str:
    """The line that names a finding the way ignore patterns and the baseline do."""
    return f"  kind: {kind}  label: {label}"


def docs_line(kind: str) -> str:
    """The line with the documentation of a finding kind, after its kind line."""
    return f"  docs: {docs_url(kind)}"


def _kind_label(detection: Detection) -> tuple[str, str]:
    if isinstance(detection, RelationshipFinding):
        return KIND_RELATIONSHIP, detection.relationship
    if isinstance(detection, ColumnLoadFinding):
        return KIND_COLUMN_LOAD, detection.label
    if isinstance(detection, WriteLoopFinding):
        return KIND_WRITE_LOOP, detection.sql
    return KIND_REPEAT, detection.sql


def _statement_line(detection: Detection) -> str:
    """The normalized statement, which is the label of a repeat or a write loop.

    The kind goes in front of it instead of on a line of its own. A statement
    longer than the report's limit is cut and ends with ``*`` instead of
    ``...``, so that the label as printed is still a pattern that matches it.
    """
    kind, label = _kind_label(detection)
    if len(label) > _SQL_LIMIT:
        label = label[:_SQL_LIMIT] + "*"
    return kind_line(kind, label)


def _loader(scalar: bool) -> str:
    return "joinedload" if scalar else "selectinload"


def loader_query(root: str, path: Sequence[tuple[str, bool]]) -> str:
    """``select(root).options(...)`` loading each ``(label, scalar)`` step.

    ``joinedload`` for a many-to-one or one-to-one step, ``selectinload`` for
    a collection; the fix line of a ``relationship-load`` finding, also used
    by the static check (``pytest_queryguard.lint``).
    """
    steps = ".".join(f"{_loader(scalar)}({label})" for label, scalar in path)
    return f"select({root}).options({steps})"


def _parent_query(finding: RelationshipFinding) -> str:
    """The parent query with the loader, through the chain that loaded the parents.

    Without a known chain, the query of the relationship's own class.
    """
    step = (finding.relationship, finding.scalar)
    if finding.parent_root is None:
        return loader_query(finding.relationship.split(".", 1)[0], [step])
    return loader_query(finding.parent_root, [*finding.parent_path, step])


def _relationship_block(
    finding: RelationshipFinding, stack: Stack, cwd: Path | None
) -> list[str]:
    lines = [
        (
            f"  {finding.relationship} was lazy-loaded {finding.count} times"
            f"{_across(finding, 'Sessions')}{_in_one(finding, 'Session')}"
        ),
        *_first_at(finding.first, stack, cwd),
    ]
    if finding.parent_site is not None:
        others = finding.other_parent_sites
        more = ""
        if others:
            more = f" and {others} other place{'s' if others > 1 else ''}"
        lines.append(f"  parents loaded at {_location(finding.parent_site, cwd)}{more}")
    kind, label = _kind_label(finding)
    lines.extend((kind_line(kind, label), docs_line(kind)))
    if finding.sample_sql is not None:
        lines.append(f"  sql: {_truncate(finding.sample_sql)}")
    return [
        *lines,
        *_scope_hint(finding),
        "  fix: load it with the parent query, e.g.",
        f"       {_parent_query(finding)}",
    ]


def _block(finding: Detection, stack: Stack, cwd: Path | None) -> list[str]:
    if isinstance(finding, RelationshipFinding):
        return _relationship_block(finding, stack, cwd)
    if isinstance(finding, ColumnLoadFinding):
        return _column_block(finding, stack, cwd)
    if isinstance(finding, WriteLoopFinding):
        return _write_block(finding, stack, cwd)
    how = " with different parameters" if finding.varying else ""
    where = _across(finding, "Sessions or connection checkouts") + _in_one(
        finding, "Session or connection checkout"
    )
    return [
        f"  the same statement ran {finding.count} times{how}{where}",
        *_first_at(finding.first, stack, cwd),
        _statement_line(finding),
        docs_line(KIND_REPEAT),
        *_scope_hint(finding),
        f"  {_BATCHED_FIX if _IN_LIST in finding.sql else _REPEAT_FIX}",
    ]


_WRITE_FIX = {
    "INSERT": [
        "  fix: write the rows together: session.add_all(rows) and one flush, or",
        "       session.execute(insert(Model), [{...}, {...}]) for a bulk insert",
    ],
    "UPDATE": [
        "  fix: update the rows together, as a bulk UPDATE by primary key:",
        "       session.execute(update(Model), [{'id': 1, ...}, {'id': 2, ...}])",
        "       or with one UPDATE ... WHERE id IN (...)",
    ],
    "DELETE": [
        "  fix: delete the rows together: one DELETE ... WHERE id IN (...)",
    ],
}


def _write_block(
    finding: WriteLoopFinding, stack: Stack, cwd: Path | None
) -> list[str]:
    return [
        (
            f"  the same {finding.verb} ran {finding.count} times, one row at a "
            f"time{_across(finding, 'Sessions or connection checkouts')}"
            f"{_in_one(finding, 'Session or connection checkout')}"
        ),
        *_first_at(finding.first, stack, cwd),
        _statement_line(finding),
        docs_line(KIND_WRITE_LOOP),
        *_scope_hint(finding),
        *_WRITE_FIX.get(finding.verb, _WRITE_FIX["INSERT"]),
    ]


def _column_block(
    finding: ColumnLoadFinding, stack: Stack, cwd: Path | None
) -> list[str]:
    owner = finding.owner
    count = (
        f"{finding.count} times for {finding.instances} instances"
        f"{_across(finding, 'Sessions')}{_in_one(finding, 'Session')}"
    )
    if finding.cause == CAUSE_DEFERRED:
        names = ", ".join(finding.attributes)
        head = f"  {owner}.{names} was loaded on its own {count} (deferred column)"
        undefer = ", ".join(f"undefer({owner}.{name})" for name in finding.attributes)
        fix = [
            "  fix: load it with the query, e.g.",
            f"       select({owner}).options({undefer})",
            (
                "       or remove defer(), load_only() or deferred() if it is "
                "usually needed"
            ),
        ]
    elif finding.cause == CAUSE_REFRESH:
        head = f"  {owner} was reloaded with session.refresh() {count}"
        fix = [
            "  fix: reload them with one query, e.g.",
            (
                f"       select({owner}).where({owner}.id.in_(ids))"
                ".execution_options(populate_existing=True)"
            ),
        ]
    else:
        head = (
            f"  {owner} had its columns reloaded {count}, "
            "after commit(), expire() or expire_all()"
        )
        fix = [
            (
                "  fix: create the Session with expire_on_commit=False, "
                "or load the instances again with one query"
            ),
        ]
    kind, label = _kind_label(finding)
    lines = [
        head,
        *_first_at(finding.first, stack, cwd),
        kind_line(kind, label),
        docs_line(kind),
    ]
    if finding.sample_sql is not None:
        lines.append(f"  sql: {_truncate(finding.sample_sql)}")
    return [*lines, *_scope_hint(finding), *fix]


def make_finding(detection: Detection, *, cwd: Path | None = None) -> Finding:
    """The finding for one detector result, its message rendered for ``cwd``."""
    site = detection.first
    stack = _stack(site, cwd)
    kind, label = _kind_label(detection)
    return Finding(
        kind=kind,
        label=label,
        file=None if site is None else _display_path(site.filename, cwd),
        function=None if site is None else site.function,
        message="\n".join(_block(detection, stack, cwd)),
        line=None if site is None else site.lineno,
        count=detection.count,
        stack=stack,
    )


def make_findings(
    detections: Iterable[Detection], *, cwd: Path | None = None
) -> list[Finding]:
    return [make_finding(d, cwd=cwd) for d in detections]


def format_strict(finding: Finding, parents: int) -> str:
    """The text of the exception queryguard_strict raises at the access."""
    return "\n".join(
        [
            (
                f"queryguard strict: {finding.label} was lazy-loaded for {parents} "
                "parents loaded at one line; this access is the N+1"
            ),
            "",
            finding.message,
            "",
            "  (queryguard_strict raises at the lazy load; allow_nplusone,",
            "  queryguard_ignore or an ignore() block lets it pass, as for the report)",
        ]
    )


_UNUSED_KINDS = (KIND_UNUSED_EAGER_LOAD, KIND_UNUSED_COLUMNS)


def format_findings(
    findings: Sequence[Finding | Detection], *, cwd: Path | None = None
) -> str:
    """The N+1 report; detector results are turned into findings first."""
    made = [f if isinstance(f, Finding) else make_finding(f, cwd=cwd) for f in findings]
    if made and all(f.kind == KIND_UNUSED_EAGER_LOAD for f in made):
        lines = ["queryguard: unused eager load detected (experimental)"]
    elif made and all(f.kind == KIND_UNUSED_COLUMNS for f in made):
        lines = ["queryguard: unused columns detected (experimental)"]
    elif made and all(f.kind in _UNUSED_KINDS for f in made):
        lines = ["queryguard: unused eager loads and columns detected (experimental)"]
    elif made and all(f.kind == KIND_CARTESIAN_LOAD for f in made):
        lines = ["queryguard: cartesian join detected"]
    else:
        lines = ["queryguard: N+1 detected"]
    for finding in made:
        lines.append("")
        lines.append(finding.message)
    return "\n".join(lines)


def _truncate(sql: str) -> str:
    sql = " ".join(sql.split())
    if len(sql) > _SQL_LIMIT:
        return sql[:_SQL_LIMIT] + "..."
    return sql


def format_truncation(max_events: int) -> str:
    """The note for a test that ran more statements than the plugin stores."""
    return (
        f"queryguard: only the first {max_events} statements and relationship "
        "loads of this test were stored (queryguard_max_events); the counts "
        "above are lower bounds"
    )


def format_duration(seconds: float) -> str:
    """A duration for a report: ``37 us`` below a millisecond, ``0.4 ms`` below a
    second, ``1.25 s`` above."""
    if seconds >= 1:
        return f"{seconds:.2f} s"
    if seconds >= 0.001:
        return f"{seconds * 1000:.1f} ms"
    return f"{seconds * 1_000_000:.0f} us"


def _timing_lines(
    statements: list[StatementEvent],
    db_time: float | None,
    cwd: Path | None,
    top: int,
) -> list[str]:
    """Total database time and the slowest statements; empty without timings."""
    timed = [s for s in statements if s.duration is not None]
    if not timed:
        return []
    total = db_time if db_time is not None else sum(s.duration or 0.0 for s in timed)
    lines = [f"  database time: {format_duration(total)}", "  slowest:"]
    for stmt in sorted(timed, key=lambda s: s.duration or 0.0, reverse=True)[:top]:
        line = f"    {format_duration(stmt.duration or 0.0):>9}  {_truncate(stmt.sql)}"
        if stmt.callsite is not None:
            line += f"  ({_location(stmt.callsite, cwd)})"
        lines.append(line)
    return lines


def format_budget(
    limit: int,
    statements: list[StatementEvent],
    *,
    cwd: Path | None = None,
    total: int | None = None,
    db_time: float | None = None,
    source: str | None = None,
    exact: bool = False,
) -> str:
    """The budget failure; ``total`` is the real count when not all are stored.

    ``db_time`` is the real database time when not all statements are stored;
    ``source`` says where the limit comes from when it is not a marker;
    ``exact`` is for a budget that fewer statements fail too.
    """
    shown = len(statements)
    got = shown if total is None else total
    noun = "query" if limit == 1 else "queries"
    bound = "exactly" if exact else "at most"
    lines = [f"queryguard: expected {bound} {limit} {noun}, got {got}"]
    if source is not None:
        lines.append(f"  (the limit is {source})")
    if got:  # an exact budget fails with none too
        lines.append("")
    for number, stmt in enumerate(statements, start=1):
        line = f"  {number}. {_truncate(stmt.sql)}"
        if stmt.callsite is not None:
            line += f"  ({_location(stmt.callsite, cwd)})"
        lines.append(line)
    if total is not None and total > shown:
        lines.append(
            f"  ... and {total - shown} more, not stored (queryguard_max_events)"
        )
    timing = _timing_lines(statements, db_time, cwd, top=3)
    if timing:
        lines.append("")
        lines.extend(timing)
    return "\n".join(lines)


def format_time_budget(
    limit: float,
    statements: list[StatementEvent],
    *,
    cwd: Path | None = None,
    total: int | None = None,
    db_time: float | None = None,
) -> str:
    """The database-time budget failure, with the slowest statements."""
    got = len(statements) if total is None else total
    spent = (
        db_time if db_time is not None else sum(s.duration or 0.0 for s in statements)
    )
    noun = "statement" if got == 1 else "statements"
    lines = [
        (
            f"queryguard: database time {format_duration(spent)} exceeded the "
            f"budget of {format_duration(limit)} ({got} {noun})"
        ),
        "",
    ]
    lines.extend(_timing_lines(statements, spent, cwd, top=5))
    if total is not None and total > len(statements):
        lines.append(
            f"  ... {total - len(statements)} more statements were not stored "
            "(queryguard_max_events), so the slowest list may be incomplete"
        )
    return "\n".join(lines)


def _verification_line(verification: Verification) -> str:
    if verification.error is not None:
        return f"  hypopg: could not verify this index: {_truncate(verification.error)}"
    costs = ""
    before, after = verification.cost_before, verification.cost_after
    if before is not None and after is not None:
        costs = f"; estimated cost {before:.2f} -> {after:.2f}"
        if round(after, 2) >= round(before, 2):
            costs += " (no gain at the current table size)"
    if verification.used:
        return f"  hypopg: the planner uses it ({verification.scan}){costs}"
    return (
        "  hypopg: the planner would not use it for this statement, "
        f"even with sequential scans off{costs}"
    )


def _advice_lines(advice: Advice) -> list[str]:
    if advice.create_sql is None:
        return [f"  fix: no new index: {advice.skipped}"]
    lines = [f"  fix: {advice.create_sql}"]
    if advice.alembic is not None:
        first, *rest = advice.alembic.splitlines()
        lines.append(f"  alembic: {first}")
        lines.extend(f"  {line}" for line in rest)
    if advice.verification is not None:
        lines.append(_verification_line(advice.verification))
    return lines


def _scan_block(scan: ScanEvent, stack: Stack, cwd: Path | None) -> list[str]:
    issue = scan.issue
    table = table_label(issue)
    where = f"with {issue.kind}"
    if issue.advice is not None:
        fix = _advice_lines(issue.advice)
    elif issue.column is None:
        text = "add an index that matches this condition"
        if issue.expression:
            text += " (an expression index may be needed)"
        fix = [f"  fix: {text}"]
    else:
        fix = [f"  fix: CREATE INDEX ON {table} ({issue.column})"]
    condition = _truncate(issue.condition)
    if issue.index is not None:
        # An existing index is walked end to end: the table is not read in
        # full, and the filter cannot use that index.
        head = (
            f"  {table} scans the whole {issue.index}: the {issue.kind} "
            f"{condition} does not match the start of any index"
        )
    else:
        head = f"  {table} is read in full: {issue.node} {where} {condition}"
    return [
        head,
        *_first_at(scan.callsite, stack, cwd),
        kind_line(KIND_MISSING_INDEX, table),
        docs_line(KIND_MISSING_INDEX),
        f"  sql: {_truncate(scan.sql)}",
        *fix,
    ]


def _index_advice(issue: ScanIssue) -> IndexAdvice | None:
    advice = issue.advice
    if advice is None:
        return None
    verification = advice.verification
    return IndexAdvice(
        create_index=advice.create_sql,
        alembic=advice.alembic,
        columns=tuple(k.column + k.suffix for k in advice.keys),
        where=advice.where,
        skipped=advice.skipped,
        verified=None if verification is None else verification.used,
        cost_before=None if verification is None else verification.cost_before,
        cost_after=None if verification is None else verification.cost_after,
        size_bytes=None if verification is None else verification.size,
        verification_error=None if verification is None else verification.error,
    )


def make_index_finding(scan: ScanEvent, *, cwd: Path | None = None) -> Finding:
    """The finding for one missing-index scan, its message rendered for ``cwd``."""
    site = scan.callsite
    stack = _stack(site, cwd)
    return Finding(
        kind=KIND_MISSING_INDEX,
        label=table_label(scan.issue),
        file=None if site is None else _display_path(site.filename, cwd),
        function=None if site is None else site.function,
        message="\n".join(_scan_block(scan, stack, cwd)),
        line=None if site is None else site.lineno,
        advice=_index_advice(scan.issue),
        table=(scan.issue.schema, scan.issue.table),
        stack=stack,
    )


def format_index_findings(scans: list[ScanEvent], *, cwd: Path | None = None) -> str:
    lines = ["queryguard: missing index"]
    for scan in scans:
        lines.append("")
        lines.append(make_index_finding(scan, cwd=cwd).message)
    # A whole index walked is no sequential scan: say what the note is about.
    walked_only = all(scan.issue.index is not None for scan in scans)
    note = (
        "a whole-index scan is fine on a small lookup table:"
        if walked_only
        else "a sequential scan is fine on a small lookup table:"
    )
    lines.extend(["", f"  {note}", "  list it in queryguard_index_ignore_tables"])
    return "\n".join(lines)


def format_index_notes(
    notices: Iterable[str],
    errors: int,
    first_error: tuple[str, str] | None,
    partial_skipped: Sequence[Sequence[str]] = (),
) -> list[str]:
    """The session summary lines for statements the index check skipped."""
    lines = list(notices)
    if partial_skipped:
        count = len(partial_skipped)
        tables = ", ".join(sorted({t for tables in partial_skipped for t in tables}))
        noun, verb = ("statement", "it") if count == 1 else ("statements", "them")
        lines.append(
            f"{count} {noun} skipped: a partial index on {tables} may serve {verb}; "
            "the generic plan cannot tell"
        )
    if errors and first_error is not None:
        message, sql = first_error
        noun = "statement" if errors == 1 else "statements"
        lines.append(
            f"{errors} {noun} could not be explained; the first: {_truncate(message)}"
        )
        lines.append(f"  {_truncate(sql)}")
    return lines


def format_failure(failure: Failure) -> list[str]:
    """The session summary for a plugin error that stopped the measurement."""
    lines = [
        f"pytest-queryguard stopped measuring after an error in the {failure.source}:",
        f"  {failure.summary}",
    ]
    if failure.raised_at is not None:
        lines.append(f"  raised at {failure.raised_at}")
    lines.extend(
        [
            "Your tests were not affected. Tests that ran after the error were not",
            "checked for N+1 queries, budgets or missing indexes. Please report it at",
            "https://github.com/MaciejZiel/pytest-queryguard/issues",
        ]
    )
    return lines


def format_stale_ignores(
    patterns: Sequence[object],
    markers: Sequence[tuple[str, object]],
    blocks: Sequence[tuple[str, str]] = (),
) -> list[str]:
    """The session summary lines for ignores that matched no finding.

    ``blocks`` are ignore() blocks as ("file:line", "ignore('kind')").
    """
    lines: list[str] = []
    if patterns:
        lines.append("queryguard_ignore patterns that matched no finding in this run:")
        lines.extend(f"  {pattern}" for pattern in patterns)
    if markers:
        lines.append("allow_nplusone patterns that matched no finding in their test:")
        lines.extend(f"  {nodeid}: {rule}" for nodeid, rule in markers)
    if blocks:
        lines.append("ignore() blocks that left out no finding in this run:")
        lines.extend(f"  {where}: {text}" for where, text in blocks)
    if lines:
        lines.append("Remove them if the finding is fixed, or fix a mistyped pattern.")
    return lines


_STALE_BASELINE_SHOWN = 20


def format_stale_baseline(
    stale: Sequence[tuple[str, str, str | None, str | None]], path: Path
) -> list[str]:
    """The session summary lines for baseline entries no finding matched."""
    if not stale:
        return []
    noun = "entry" if len(stale) == 1 else "entries"
    lines = [f"{len(stale)} baseline {noun} in {path} matched no finding in this run:"]
    for kind, label, file, function in stale[:_STALE_BASELINE_SHOWN]:
        where = "" if file is None else f" ({file}"
        if where and function is not None:
            where += f", {function}"
        where += ")" if where else ""
        lines.append(f"  {kind} {_truncate(label)}{where}")
    if len(stale) > _STALE_BASELINE_SHOWN:
        lines.append(f"  ... and {len(stale) - _STALE_BASELINE_SHOWN} more")
    lines.append(
        "They are fixed, or this run did not cover them. After a full run, "
        "prune them with --queryguard-update-baseline."
    )
    return lines
