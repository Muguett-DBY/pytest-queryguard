"""What the head-to-head runs share: the tools, the verdict of one scenario, the scores.

Everything here is plain Python with no dependency, so the parsing and the
scoring are tested in the project's own suite (``tests/test_head_to_head.py``)
without any competitor installed.

A tool is measured on the scenarios of ``tests/corpus`` the way its own
documentation says to make it fail a test on an N+1, with its defaults. Per
scenario the result is one of:

* ``flagged``: the tool reported a problem (its exception or failure message
  is in the test's report, or, for pytest-queryguard, a finding was recorded);
* ``passed``: the test passed;
* ``error``: the test failed for another reason (the tool crashed, the
  scenario's code broke under the tool's patches, a timeout) or never ran.
  An error is never counted as a pass: for a BUG scenario it is a miss, for
  a CLEAN one it is not a clean pass either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

REL = "relationship-load"
REPEAT = "repeated-statement"
COLUMN = "column-load"
WRITE = "write-loop"
UNUSED = "unused-eager-load"
CARTESIAN = "cartesian-load"
COLUMNS = "unused-columns"
KINDS = (REL, REPEAT, COLUMN, WRITE, UNUSED, CARTESIAN, COLUMNS)

# A tool that flags a scenario without saying which kind (or with a message
# this parser does not know) is credited at the scenario level only.
UNKNOWN_KIND = "?"

FLAGGED = "flagged"
PASSED = "passed"
ERROR = "error"


@dataclass(frozen=True)
class Tool:
    key: str
    label: str
    # what to install next to the corpus's dependencies; empty for none
    requirement: str
    # the extra pytest arguments of its corpus session
    args: tuple[str, ...]
    # the corpus kinds the tool says it detects; the others are "n/a"
    supported: frozenset[str]
    # whether its report says which kind of problem it found
    reports_kinds: bool
    # text in a failed test's report that means the tool failed the test
    markers: tuple[str, ...] = ()
    # (regex, kind) read from that report when reports_kinds
    kind_patterns: tuple[tuple[str, str], ...] = ()
    # the pytest-queryguard configuration (tests/corpus/runner.py CONFIGS)
    config: str | None = None
    notes: str = ""
    # installed with it: a pin that replaces one of the corpus's dependencies
    extra: tuple[str, ...] = ()

    @property
    def install(self) -> tuple[str, ...]:
        return (self.requirement, *self.extra) if self.requirement else self.extra


QUERYGUARD_DEFAULT_KINDS = frozenset({REL, REPEAT, COLUMN, CARTESIAN})

TOOLS: dict[str, Tool] = {
    tool.key: tool
    for tool in (
        Tool(
            key="none",
            label="no tool (baseline)",
            requirement="",
            args=("-p", "no:queryguard"),
            supported=frozenset(),
            reports_kinds=False,
            notes="Every plugin off: a scenario that fails here fails on its own.",
        ),
        Tool(
            key="queryguard",
            label="pytest-queryguard, defaults",
            requirement="",
            args=(),
            supported=QUERYGUARD_DEFAULT_KINDS,
            reports_kinds=True,
            config="default",
            notes="No option set. cartesian-load only warns by default; a warning counts.",
        ),
        Tool(
            key="queryguard-all",
            label="pytest-queryguard, all opt-in rules",
            requirement="",
            args=(),
            supported=frozenset(KINDS),
            reports_kinds=True,
            config="all-opt-in",
            notes="Every opt-in rule switched on (tests/corpus/runner.py all-opt-in).",
        ),
        Tool(
            key="queryspy",
            label="queryspy",
            requirement="queryspy==0.6.0",
            args=("--queryspy-strict", "-p", "no:queryguard"),
            supported=frozenset({REL, COLUMN, REPEAT}),
            reports_kinds=True,
            markers=("NPlusOneError", "N+1 detected:"),
            kind_patterns=(
                (r"N\+1 detected: \d+ queries for .* \(lazy load\)", REL),
                (r"N\+1 detected: \d+ column loads for ", COLUMN),
                (r"(?m)^(?:E\s+)?\d+ identical queries\s*$", REPEAT),
            ),
            notes="`--queryspy-strict` (fails on lazy_load, column_load and repeated_statement).",
        ),
        Tool(
            key="querycount",
            label="pytest-querycount",
            requirement="pytest-querycount==0.4.0",
            args=("-p", "no:queryguard"),
            supported=frozenset({REL, REPEAT, COLUMN}),
            reports_kinds=False,
            markers=("DuplicateQueryError",),
            notes="`@pytest.mark.no_n_plus_one` (default threshold, SELECTs only) on every scenario.",
        ),
        Tool(
            key="sniffer",
            label="n-plus-one-sniffer",
            requirement=(
                "n-plus-one-sniffer @ git+https://github.com/async-workflows/"
                "n-plus-one-sniffer@d4790f830457a5a2100171c3e24b3d17da13778a"
            ),
            args=("-p", "no:queryguard"),
            supported=frozenset({REL, REPEAT, COLUMN, WRITE}),
            reports_kinds=False,
            markers=("suspected N+1 query pattern", "NPlusOneError"),
            notes=(
                "`@pytest.mark.no_n_plus_one` (default threshold 3) on every scenario, "
                "`nplusone_engine` fixture returning the `Engine` class (every engine)."
            ),
        ),
        Tool(
            key="nplusone",
            label="nplusone (SQLAlchemy)",
            requirement="nplusone==1.0.0",
            args=("-p", "no:queryguard"),
            supported=frozenset({REL, UNUSED}),
            reports_kinds=True,
            markers=(
                "NPlusOneError",
                "Potential n+1 query",
                "Potential unnecessary eager load",
            ),
            kind_patterns=(
                (r"Potential n\+1 query detected", REL),
                (r"Potential unnecessary eager load detected", UNUSED),
            ),
            notes=(
                "`import nplusone.ext.sqlalchemy` and `profiler.Profiler()` around each "
                "test call. On SQLAlchemy 2.1 the import fails (`LazyLoader` moved)."
            ),
        ),
    )
}

# nplusone does not import on SQLAlchemy 2.1 (``strategies.LazyLoader``
# moved), so it also runs on the last 2.0, with a baseline of its own.
SQLALCHEMY_20 = ("sqlalchemy==2.0.54",)
TOOLS["none-sa20"] = replace(
    TOOLS["none"],
    key="none-sa20",
    label="no tool, SQLAlchemy 2.0 (baseline)",
    notes="The baseline of the SQLAlchemy 2.0 runs.",
    extra=SQLALCHEMY_20,
)
TOOLS["nplusone-sa20"] = replace(
    TOOLS["nplusone"],
    key="nplusone-sa20",
    label="nplusone on SQLAlchemy 2.0",
    notes=(
        "`import nplusone.ext.sqlalchemy` and `profiler.Profiler()` around each "
        "test call, with SQLAlchemy 2.0.54 instead of 2.1."
    ),
    extra=SQLALCHEMY_20,
)

# The tools the summary compares, in this order; "none" is the baseline.
COMPARED = (
    "queryguard",
    "queryguard-all",
    "queryspy",
    "querycount",
    "sniffer",
    "nplusone",
    "nplusone-sa20",
)


@dataclass(frozen=True)
class Verdict:
    status: str
    kinds: frozenset[str] = frozenset()
    detail: str = ""


def first_line(text: str, limit: int = 200) -> str:
    """The most telling line of a failure report: the last ``E`` line, else the last line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    errors = [line[1:].strip() for line in lines if line.startswith("E ")]
    pick = errors[-1] if errors else (lines[-1] if lines else "")
    return pick[:limit]


def parse_kinds(tool: Tool, text: str) -> frozenset[str]:
    """The kinds a kind-reporting tool names in a failure report."""
    found = {kind for pattern, kind in tool.kind_patterns if re.search(pattern, text)}
    return frozenset(found)


def classify(
    tool: Tool, phases: list[dict[str, str]], findings: frozenset[str] | None = None
) -> Verdict:
    """The verdict of one scenario from the JSON lines ``session_plugin`` wrote.

    ``phases`` are that scenario's lines; ``findings`` the kinds
    pytest-queryguard recorded (only for its own runs). A scenario with no
    line never ran: an error.
    """
    if not phases:
        return Verdict(ERROR, detail="did not run (the session stopped before it)")
    failed = [p for p in phases if p.get("outcome") == "failed"]
    text = "\n".join(p.get("longrepr", "") for p in failed)
    if tool.config is not None:  # pytest-queryguard: what its findings say
        if findings:
            return Verdict(FLAGGED, findings)
        if failed:
            return Verdict(ERROR, detail=first_line(text))
        return Verdict(PASSED)
    if failed:
        if tool.markers and any(marker in text for marker in tool.markers):
            kinds = parse_kinds(tool, text) if tool.reports_kinds else frozenset()
            return Verdict(
                FLAGGED, kinds or frozenset({UNKNOWN_KIND}), first_line(text)
            )
        return Verdict(ERROR, detail=first_line(text))
    if not any(p.get("when") == "call" for p in phases):
        return Verdict(ERROR, detail="no result for the test call")
    return Verdict(PASSED)


def group_lines(lines: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    for line in lines:
        out.setdefault(line["scenario"], []).append(line)
    return out


# --- scores -------------------------------------------------------------


@dataclass
class KindScore:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    # binary tools: BUG scenarios of this kind that were flagged / that exist
    flagged: int = 0
    total: int = 0

    @staticmethod
    def ratio(a: int, b: int) -> float | None:
        return a / b if b else None


@dataclass
class ToolScore:
    tool: str
    bug_flagged: int = 0
    bug_missed: int = 0
    bug_error: int = 0
    clean_passed: int = 0
    clean_flagged: int = 0
    clean_error: int = 0
    # BUG scenarios with at least one kind the tool says it detects
    in_scope_bugs: int = 0
    in_scope_flagged: int = 0
    kinds: dict[str, KindScore | None] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)

    @property
    def bugs(self) -> int:
        return self.bug_flagged + self.bug_missed + self.bug_error

    @property
    def cleans(self) -> int:
        return self.clean_passed + self.clean_flagged + self.clean_error

    @property
    def precision(self) -> float | None:
        return KindScore.ratio(self.bug_flagged, self.bug_flagged + self.clean_flagged)

    @property
    def recall(self) -> float | None:
        return KindScore.ratio(self.bug_flagged, self.bugs)

    @property
    def in_scope_recall(self) -> float | None:
        return KindScore.ratio(self.in_scope_flagged, self.in_scope_bugs)

    @property
    def clean_pass_rate(self) -> float | None:
        return KindScore.ratio(self.clean_passed, self.cleans)


def score(
    tool: Tool,
    scenarios: dict[str, dict[str, object]],
    verdicts: dict[str, Verdict],
) -> ToolScore:
    """Scenario-level and per-kind scores of one tool.

    ``scenarios`` maps a name to ``{"verdict": "BUG"|"CLEAN", "kinds": [...]}``.
    Per kind, a tool that reports kinds gets TP/FP/FN as in
    ``tests/corpus/metrics.py`` (a kind it reported that the scenario does not
    have is a false positive of that kind); a tool that only fails the test
    gets a recall (flagged BUG scenarios of that kind) and no precision. A
    kind the tool does not claim to detect is ``None`` (n/a), not zero.
    """
    out = ToolScore(tool.key)
    for kind in KINDS:
        out.kinds[kind] = KindScore() if kind in tool.supported else None
    for name in sorted(scenarios):
        meta = scenarios[name]
        expected = frozenset(str(k) for k in meta["kinds"])  # type: ignore[attr-defined]
        bug = meta["verdict"] == "BUG"
        verdict = verdicts.get(name, Verdict(ERROR, detail="no result"))
        flagged = verdict.status == FLAGGED
        if bug:
            if flagged:
                out.bug_flagged += 1
            elif verdict.status == ERROR:
                out.bug_error += 1
            else:
                out.bug_missed += 1
            if expected & tool.supported:
                out.in_scope_bugs += 1
                out.in_scope_flagged += flagged
                if verdict.status == PASSED:  # an error is listed with the errors
                    out.misses.append(name)
        elif flagged:
            out.clean_flagged += 1
            out.false_positives.append(name)
        elif verdict.status == ERROR:
            out.clean_error += 1
        else:
            out.clean_passed += 1
        if verdict.status == ERROR:
            out.errors.append((name, verdict.detail))
        observed = verdict.kinds & tool.supported
        for kind, tally in out.kinds.items():
            if tally is None:
                continue
            if tool.reports_kinds:
                if kind in expected and kind in observed:
                    tally.tp += 1
                elif kind in observed:
                    tally.fp += 1
                elif kind in expected:
                    tally.fn += 1
            elif bug and kind in expected:
                tally.total += 1
                tally.flagged += flagged
    return out


# --- rendering ----------------------------------------------------------


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.0f}%"


def kind_cell(tool: Tool, tally: KindScore | None) -> str:
    if tally is None:
        return "n/a"
    if tool.reports_kinds:
        p = KindScore.ratio(tally.tp, tally.tp + tally.fp)
        r = KindScore.ratio(tally.tp, tally.tp + tally.fn)
        # no finding of that kind at all: no precision to give
        return f"{'-' if p is None else pct(p)} / {pct(r)}"
    return f"- / {pct(KindScore.ratio(tally.flagged, tally.total))}"


def accuracy_tables(scores: dict[str, ToolScore]) -> list[str]:
    lines = [
        (
            "| tool | BUG flagged | BUG missed | BUG error | CLEAN passed | CLEAN flagged "
            "| CLEAN error | precision | recall (all BUG) | recall (BUG of kinds it claims) |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, s in scores.items():
        lines.append(
            f"| {TOOLS[key].label} | {s.bug_flagged} | {s.bug_missed} | {s.bug_error} "
            f"| {s.clean_passed} | {s.clean_flagged} | {s.clean_error} "
            f"| {pct(s.precision)} | {pct(s.recall)} | {pct(s.in_scope_recall)} |"
        )
    lines += [
        "",
        (
            "Per kind, precision / recall. `n/a`: the tool does not claim that kind "
            "(for pytest-queryguard defaults: the rule is off by default). `-`: the tool "
            "does not say which kind it found (or reported no finding of it), so only the share of BUG scenarios of "
            "that kind it flagged (for any reason) is given."
        ),
        "",
        "| tool | " + " | ".join(f"`{k}`" for k in KINDS) + " |",
        "|---|" + "---|" * len(KINDS),
    ]
    for key, s in scores.items():
        tool = TOOLS[key]
        lines.append(
            f"| {tool.label} | "
            + " | ".join(kind_cell(tool, s.kinds[k]) for k in KINDS)
            + " |"
        )
    return lines


def overhead_table(overhead: dict[str, object]) -> list[str]:
    """Rows of added cost against the run with no tool, best of N runs."""
    micro = overhead.get("micro", {})  # type: ignore[union-attr]
    suite = overhead.get("suite", {})  # type: ignore[union-attr]
    tools = [t for t in COMPARED if t in micro or t in suite]  # type: ignore[operator]
    if not tools or "off" not in micro:  # type: ignore[operator]
        return ["No overhead results."]
    labels = {"queryguard": "pytest-queryguard"}
    head = [labels.get(t, TOOLS[t].label) for t in tools]
    lines = ["| | " + " | ".join(head) + " |", "|---|" + "---|" * len(tools)]
    tests = overhead.get("tests", "?")
    for key, how in (("suite", "best"), ("suite_median", "median")):
        runs = overhead.get(key) or {}
        off_suite = runs.get("off")  # type: ignore[union-attr]
        if not off_suite:
            continue
        cells = []
        for t in tools:
            v = runs.get(t)  # type: ignore[union-attr]
            cells.append(
                "failed" if v is None else f"{(v - off_suite) / off_suite:+.1%}"
            )
        lines.append(
            f"| {tests}-test suite, added wall time ({how}) | "
            + " | ".join(cells)
            + " |"
        )
    for loop, label in (
        ("core", "Core `select`"),
        ("orm", "ORM `select`"),
        ("lazy", "lazy load"),
    ):
        base = micro["off"].get(loop)  # type: ignore[index]
        cells = []
        for t in tools:
            v = micro.get(t, {}).get(loop)  # type: ignore[union-attr]
            cells.append(
                "failed" if v is None or base is None else f"{v - base:+.1f} us"
            )
        lines.append(f"| {label}, added per statement | " + " | ".join(cells) + " |")
    return lines
