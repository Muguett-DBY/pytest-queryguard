from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pytest_queryguard import baseline, sarif
from pytest_queryguard.findings import KIND_RELATIONSHIP, KIND_REPEAT, Finding, docs_url

# pytester runs sessions in-process, so the outer plugin sees their queries too.
pytestmark = pytest.mark.allow_nplusone

MODELS = (Path(__file__).parent / "models.py").read_text()

LOOP = """
import pytest
from qgmodels import Customer, Order, make_sync_session


def test_loop():
    session = make_sync_session()
    for order in session.query(Order).all():
        _ = order.customer.name


def test_other_loop():
    session = make_sync_session()
    for customer in session.query(Customer).all():
        _ = customer.orders
"""


def _finding(**kwargs: Any) -> Finding:
    values: dict[str, Any] = {
        "kind": KIND_RELATIONSHIP,
        "label": "Order.customer",
        "file": "app/orders.py",
        "function": "list_orders",
        "message": "  Order.customer was lazy-loaded 3 times\n  fix: joinedload",
        "line": 12,
        "count": 3,
    }
    values.update(kwargs)
    return Finding(**values)


def _doc(*findings: Finding, level: str = "error") -> dict[str, Any]:
    store: sarif.Collected = {}
    for f in findings:
        sarif.add(store, f, level)
    return sarif.build(store, "1.2.3")[0]


# --- a structural check of the SARIF 2.1.0 requirements ---
# The OASIS schema is not vendored (its terms are not an SPDX license), so the
# properties the specification and GitHub require are checked by hand.


def check_sarif(document: dict[str, Any]) -> None:
    assert document["version"] == "2.1.0"
    assert document["$schema"].endswith("sarif-2.1.0.json")
    (run,) = document["runs"]
    driver = run["tool"]["driver"]
    assert driver["name"] and driver["version"]
    rules = driver["rules"]
    ids = [rule["id"] for rule in rules]
    assert len(ids) == len(set(ids))
    for rule in rules:
        assert rule["shortDescription"]["text"]
        assert rule["help"]["text"] and rule["help"]["markdown"]
        assert rule["defaultConfiguration"]["level"] in {
            "none",
            "note",
            "warning",
            "error",
        }
    for result in run["results"]:
        assert rules[result["ruleIndex"]]["id"] == result["ruleId"]
        assert result["level"] in {"none", "note", "warning", "error"}
        assert result["message"]["text"]
        (location,) = result["locations"]
        physical = location["physicalLocation"]
        uri = physical["artifactLocation"]["uri"]
        assert not uri.startswith(("/", "file:")) and "\\" not in uri
        assert physical["region"]["startLine"] >= 1
        assert result["partialFingerprints"]["primaryLocationLineHash"]
        assert result["partialFingerprints"][sarif.FINGERPRINT_KEY]


def test_document_shape() -> None:
    document = _doc(_finding())
    check_sarif(document)
    run = document["runs"][0]
    assert [r["id"] for r in run["tool"]["driver"]["rules"]] == [
        f"queryguard/{kind}" for kind in sarif.KINDS
    ]
    (result,) = run["results"]
    assert result["ruleId"] == "queryguard/relationship-load"
    assert (
        result["message"]["text"]
        == "Order.customer was lazy-loaded 3 times\nfix: joinedload"
    )
    location = result["locations"][0]
    assert location["physicalLocation"]["artifactLocation"] == {"uri": "app/orders.py"}
    assert location["physicalLocation"]["region"] == {"startLine": 12}
    assert location["logicalLocations"] == [{"name": "list_orders", "kind": "function"}]


def test_every_rule_has_a_fix_hint() -> None:
    for rule in _doc()["runs"][0]["tool"]["driver"]["rules"]:
        assert "Fix:" in rule["help"]["text"]
        assert "**Fix:**" in rule["help"]["markdown"]


def test_fingerprint_follows_the_identity_only() -> None:
    a = _doc(_finding(line=12, count=3))["runs"][0]["results"][0]
    b = _doc(_finding(line=99, count=50, message="other"))["runs"][0]["results"][0]
    assert a["partialFingerprints"] == b["partialFingerprints"]
    c = _doc(_finding(function="other"))["runs"][0]["results"][0]
    assert c["partialFingerprints"] != a["partialFingerprints"]
    assert len(a["partialFingerprints"][sarif.FINGERPRINT_KEY]) == 64


def test_output_is_deterministic_and_sorted() -> None:
    one = _finding(label="B.b", file="z.py")
    two = _finding(label="A.a", file="a.py")
    assert sarif.dumps(_doc(one, two)) == sarif.dumps(_doc(two, one))
    labels = [r["properties"]["label"] for r in _doc(one, two)["runs"][0]["results"]]
    assert labels == ["A.a", "B.b"]


def test_one_result_per_identity_with_counts_added() -> None:
    first = _finding(line=30, count=2)
    second = _finding(line=10, count=5, message="  earlier")
    (result,) = _doc(first, second)["runs"][0]["results"]
    assert result["properties"]["count"] == 7
    assert result["locations"][0]["physicalLocation"]["region"]["startLine"] == 10
    assert result["message"]["text"].endswith("\nearlier")


def test_the_message_and_the_properties_tell_the_same_totals() -> None:
    findings = [
        _finding(line=30, count=2, message="  ran 2 times"),
        _finding(line=10, count=10, message="  ran 10 times"),
        _finding(line=20, count=7, message="  ran 7 times"),
    ]
    (result,) = _doc(*findings)["runs"][0]["results"]
    assert result["properties"] == {
        "count": 19,
        "label": "Order.customer",
        "tests": 3,
        "peakCount": 10,
    }
    assert result["message"]["text"] == (
        "Seen in 3 tests: 19 times in total, up to 10 times in one test. "
        "The test with the earliest line:\nran 10 times"
    )


def test_a_finding_from_one_test_keeps_its_own_message_and_properties() -> None:
    (result,) = _doc(_finding(message="  ran 4 times", count=4))["runs"][0]["results"]
    assert result["message"]["text"] == "ran 4 times"
    assert result["properties"] == {"count": 4, "label": "Order.customer"}


def test_the_merge_does_not_depend_on_the_order() -> None:
    one = [_finding(line=5, count=c, message=f"  ran {c}") for c in (3, 9, 4)]
    forward = _doc(*one)["runs"][0]["results"]
    backward = _doc(*reversed(one))["runs"][0]["results"]
    assert forward == backward


def test_merged_findings_keep_their_identity() -> None:
    store: sarif.Collected = {}
    for count in (3, 9):
        sarif.add(store, _finding(count=count), "error")
    (identity,) = store
    assert identity == _finding().identity


def test_error_level_wins_when_merging() -> None:
    store: sarif.Collected = {}
    sarif.add(store, _finding(), "warning")
    sarif.add(store, _finding(), "error")
    sarif.add(store, _finding(), "warning")
    assert next(iter(store.values()))[1] == "error"


@pytest.mark.parametrize(
    "file", [None, "/usr/lib/python3/site-packages/x.py", "C:\\x\\y.py", ""]
)
def test_findings_without_a_repository_location_are_left_out(file: str | None) -> None:
    store: sarif.Collected = {}
    sarif.add(store, _finding(file=file), "error")
    sarif.add(store, _finding(label="B.b"), "error")
    document, omitted = sarif.build(store, "1")
    assert omitted == 1
    assert len(document["runs"][0]["results"]) == 1


def test_a_missing_line_becomes_line_one() -> None:
    (result,) = _doc(_finding(line=None))["runs"][0]["results"]
    assert result["locations"][0]["physicalLocation"]["region"]["startLine"] == 1


def test_wire_round_trip() -> None:
    store: sarif.Collected = {}
    sarif.add(store, _finding(), "warning")
    sarif.add(
        store,
        _finding(kind=KIND_REPEAT, label="SELECT 1", file=None, function=None),
        "error",
    )
    sarif.add(store, _finding(count=9), "warning")  # merged: 2 tests, peak 9
    other: sarif.Collected = {}
    sarif.merge_wire(other, json.loads(json.dumps(sarif.to_wire(store))))
    assert other == store
    merged = other[_finding().identity][0]
    assert (merged.count, merged.tests, merged.peak) == (12, 2, 9)


def test_unknown_kind_is_left_out_not_a_crash() -> None:
    _, omitted = sarif.build(
        {("nope", "x", "a.py", "f"): (_finding(kind="nope"), "error")}, "1"
    )
    assert omitted == 1


def test_backslash_paths_become_posix() -> None:
    assert sarif.repo_path("app\\models.py") == "app/models.py"


# --- what the document says exactly ---

RULE_NAMES = {
    "relationship-load": "RelationshipLoad",
    "repeated-statement": "RepeatedStatement",
    "column-load": "ColumnLoad",
    "write-loop": "WriteLoop",
    "unused-eager-load": "UnusedEagerLoad",
    "unused-columns": "UnusedColumns",
    "cartesian-load": "CartesianLoad",
    "missing-index": "MissingIndex",
}


def test_every_rule_is_fully_described() -> None:
    rules = _doc()["runs"][0]["tool"]["driver"]["rules"]
    assert [rule["properties"]["kind"] for rule in rules] == list(RULE_NAMES)
    for rule in rules:
        kind = rule["properties"]["kind"]
        assert set(rule) == {
            "id",
            "name",
            "shortDescription",
            "fullDescription",
            "help",
            "helpUri",
            "defaultConfiguration",
            "properties",
        }
        assert rule["id"] == f"queryguard/{kind}"
        assert rule["name"] == RULE_NAMES[kind]
        full = rule["fullDescription"]["text"]
        assert set(rule["fullDescription"]) == {"text"} and full
        assert rule["help"]["text"].startswith(f"{full}\n\nFix: ")
        assert rule["help"]["markdown"].startswith(f"{full}\n\n**Fix:** ")
        assert rule["helpUri"] == docs_url(kind)
        assert rule["defaultConfiguration"] == {"level": "error"}
        assert rule["properties"] == {
            "tags": ["performance", "database"],
            "kind": kind,
        }


def test_the_tool_driver_and_the_run_are_described() -> None:
    document = _doc(_finding())
    assert document["$schema"] == "https://json.schemastore.org/sarif-2.1.0.json"
    (run,) = document["runs"]
    assert run["columnKind"] == "utf16CodeUnits"
    driver = run["tool"]["driver"]
    assert driver["name"] == "pytest-queryguard"
    assert driver["version"] == "1.2.3"
    assert driver["informationUri"] == "https://github.com/MaciejZiel/pytest-queryguard"


def test_the_text_is_indented_by_two_and_keeps_non_ascii_characters() -> None:
    assert sarif.dumps({"a": ["ż"]}) == '{\n  "a": [\n    "ż"\n  ]\n}\n'


@pytest.mark.parametrize(
    ("identity", "digest"),
    [
        (
            ("relationship-load", "Order.customer", "app/orders.py", "list_orders"),
            "a318aaa9df6e8b71f8fccf9fda916d7992fa43fcb6230fc38d6f0f6b5e2c9af2",
        ),
        (
            ("missing-index", "public.orders", None, None),
            "2f078e04f4fa3ca9023ddb3062c2ed3121ff004fd5c0fb8bf15ead7d96013137",
        ),
        (
            ("repeated-statement", "SELECT * FROM t WHERE a = ?", "a.py", None),
            "89aa611fd67efb334b2f7be3f2a2925549685bb225363867c0344b9675203056",
        ),
    ],
)
def test_fingerprints_do_not_change_between_releases(
    identity: sarif.Identity, digest: str
) -> None:
    # GitHub keeps alerts by these values, so a change would reopen every alert.
    assert sarif.fingerprint(identity) == digest


def test_the_fingerprint_depends_on_every_part_of_the_identity() -> None:
    base = ("k", "l", "a.py", "f")
    digests = {
        sarif.fingerprint(base),
        sarif.fingerprint(("k2", "l", "a.py", "f")),
        sarif.fingerprint(("k", "l2", "a.py", "f")),
        sarif.fingerprint(("k", "l", "b.py", "f")),
        sarif.fingerprint(("k", "l", "a.py", "g")),
    }
    assert len(digests) == 5
    # The parts are separated, so moving text between them changes the hash.
    assert sarif.fingerprint(("ab", "c", None, None)) != sarif.fingerprint(
        ("a", "bc", None, None)
    )


def test_the_line_hash_is_the_first_32_digits_of_the_fingerprint() -> None:
    (result,) = _doc(_finding())["runs"][0]["results"]
    digest = result["partialFingerprints"][sarif.FINGERPRINT_KEY]
    assert (
        result["partialFingerprints"]["primaryLocationLineHash"] == f"{digest[:32]}:1"
    )
    assert result["partialFingerprints"]["primaryLocationLineHash"].index(":") == 32


def test_results_are_ordered_by_file_then_function() -> None:
    findings = [
        _finding(file="b.py", function="f"),
        _finding(file="a.py", function="g"),
        _finding(file="a.py", function="Alpha"),
        _finding(file="a.py", function=None),
    ]
    results = _doc(*findings)["runs"][0]["results"]
    uris = [
        r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for r in results
    ]
    functions = [
        r["locations"][0].get("logicalLocations", [{"name": None}])[0]["name"]
        for r in results
    ]
    assert list(zip(uris, functions, strict=True)) == [
        ("a.py", None),
        ("a.py", "Alpha"),
        ("a.py", "g"),
        ("b.py", "f"),
    ]


def test_the_ordering_of_findings_without_a_location_does_not_fail() -> None:
    store: sarif.Collected = {}
    sarif.add(store, _finding(file=None), "error")
    sarif.add(store, _finding(file="a.py"), "error")
    sarif.add(store, _finding(file="a.py", function=None), "error")
    document, omitted = sarif.build(store, "1")
    assert omitted == 1
    assert len(document["runs"][0]["results"]) == 2


def test_every_left_out_finding_is_counted() -> None:
    store: sarif.Collected = {}
    sarif.add(store, _finding(kind="nope", label="a"), "error")
    sarif.add(store, _finding(kind="nope", label="b"), "error")
    sarif.add(store, _finding(file=None, label="c"), "error")
    sarif.add(store, _finding(file=None, label="d"), "error")
    sarif.add(store, _finding(label="kept"), "error")
    document, omitted = sarif.build(store, "1")
    assert omitted == 4
    assert [r["properties"]["label"] for r in document["runs"][0]["results"]] == [
        "kept"
    ]


def test_an_unknown_kind_does_not_hide_the_findings_after_it() -> None:
    store: sarif.Collected = {}
    sarif.add(store, _finding(kind="aaa", label="first"), "error")
    sarif.add(store, _finding(label="second"), "error")
    document, omitted = sarif.build(store, "1")
    assert omitted == 1
    assert len(document["runs"][0]["results"]) == 1


# --- merging the same finding over several tests ---


def test_merging_a_finding_without_a_line_keeps_the_one_with_a_line() -> None:
    for first, second in (
        (_finding(line=None), _finding(line=40)),
        (_finding(line=40), _finding(line=None)),
    ):
        (result,) = _doc(first, second)["runs"][0]["results"]
        assert result["locations"][0]["physicalLocation"]["region"]["startLine"] == 40


def test_the_merged_finding_takes_its_severity_from_the_earliest_line() -> None:
    store: sarif.Collected = {}
    sarif.add(store, _finding(line=30, severity="late"), "error")
    sarif.add(store, _finding(line=10, severity="early"), "error")
    (merged, _) = store[_finding().identity]
    assert (merged.severity, merged.line) == ("early", 10)


def test_warning_level_stays_a_warning_when_every_test_warned() -> None:
    store: sarif.Collected = {}
    sarif.add(store, _finding(), "warning")
    sarif.add(store, _finding(), "warning")
    assert next(iter(store.values()))[1] == "warning"


def test_two_tests_are_reported_in_the_properties_and_the_message() -> None:
    (result,) = _doc(_finding(count=2, line=5), _finding(count=6, line=9))["runs"][0][
        "results"
    ]
    assert result["properties"]["tests"] == 2
    assert result["properties"]["peakCount"] == 6
    assert result["message"]["text"].startswith("Seen in 2 tests: 8 times in total")


def test_wire_entries_from_an_older_worker_default_to_one_test() -> None:
    entry = {
        "kind": KIND_RELATIONSHIP,
        "label": "Order.customer",
        "file": "app/orders.py",
        "function": "list_orders",
        "message": "m",
        "severity": "custom",
        "line": 3,
        "count": 4,
        "level": "error",
    }
    store: sarif.Collected = {}
    sarif.merge_wire(store, [entry])
    (finding, level) = next(iter(store.values()))
    assert (finding.tests, finding.peak, finding.severity, level) == (
        1,
        None,
        "custom",
        "error",
    )
    sarif.merge_wire(store, [{**entry, "count": 9}])
    (finding, _) = next(iter(store.values()))
    assert (finding.count, finding.tests, finding.peak) == (13, 2, 9)


# --- the plugin ---


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(qgmodels=MODELS, test_a=LOOP)
    return pytester


def _results(path: Path) -> list[dict[str, Any]]:
    document = json.loads(path.read_text())
    check_sarif(document)
    return document["runs"][0]["results"]  # type: ignore[no-any-return]


def test_the_run_writes_sarif(project: pytest.Pytester) -> None:
    result = project.runpytest("--queryguard-sarif=out/qg.sarif")
    result.assert_outcomes(failed=2)
    result.stdout.fnmatch_lines(["*queryguard: SARIF written*", "*qg.sarif: 2 results"])
    results = _results(project.path / "out" / "qg.sarif")
    assert [(r["properties"]["label"], r["level"]) for r in results] == [
        ("Customer.orders", "error"),
        ("Order.customer", "error"),
    ]
    first = results[1]["locations"][0]["physicalLocation"]
    assert first["artifactLocation"]["uri"] == "test_a.py"
    assert first["region"]["startLine"] == 8


def test_no_flag_writes_nothing(project: pytest.Pytester) -> None:
    project.runpytest()
    assert list(project.path.glob("*.sarif")) == []


def test_a_clean_run_writes_an_empty_valid_file(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_a="def test_ok():\n    pass\n")
    result = pytester.runpytest("--queryguard-sarif=qg.sarif")
    result.assert_outcomes(passed=1)
    assert _results(pytester.path / "qg.sarif") == []


def test_warn_mode_gives_warning_level(project: pytest.Pytester) -> None:
    project.runpytest("--queryguard-sarif=qg.sarif", "--queryguard-mode=warn")
    assert {r["level"] for r in _results(project.path / "qg.sarif")} == {"warning"}


def test_fail_on_decides_the_level_per_kind(project: pytest.Pytester) -> None:
    project.makeini("[pytest]\nqueryguard_fail_on = relationship-load\n")
    project.runpytest("--queryguard-sarif=qg.sarif")
    levels = {
        r["properties"]["label"]: r["level"]
        for r in _results(project.path / "qg.sarif")
    }
    assert levels == {"Customer.orders": "error", "Order.customer": "error"}
    project.makeini("[pytest]\nqueryguard_fail_on = write-loop\n")
    project.runpytest("--queryguard-sarif=qg.sarif")
    assert {r["level"] for r in _results(project.path / "qg.sarif")} == {"warning"}


def test_ignored_findings_are_not_in_the_file(project: pytest.Pytester) -> None:
    project.makeini(
        "[pytest]\nqueryguard_ignore =\n    relationship-load Order.customer\n"
    )
    project.runpytest("--queryguard-sarif=qg.sarif")
    assert [r["properties"]["label"] for r in _results(project.path / "qg.sarif")] == [
        "Customer.orders"
    ]


def test_baselined_findings_are_left_out_new_ones_stay(
    project: pytest.Pytester,
) -> None:
    project.runpytest("--queryguard-baseline=b.json", "--queryguard-update-baseline")
    path = project.path / "b.json"
    kept = [i for i in baseline.load(path) if i[1] != "Customer.orders"]
    path.write_text(baseline.dumps(kept))
    project.runpytest("--queryguard-baseline=b.json", "--queryguard-sarif=qg.sarif")
    assert [r["properties"]["label"] for r in _results(project.path / "qg.sarif")] == [
        "Customer.orders"
    ]


def test_same_file_on_two_runs(project: pytest.Pytester) -> None:
    project.runpytest("--queryguard-sarif=a.sarif")
    project.runpytest("--queryguard-sarif=b.sarif")
    assert (project.path / "a.sarif").read_text() == (
        project.path / "b.sarif"
    ).read_text()


def test_empty_path_is_a_usage_error(project: pytest.Pytester) -> None:
    result = project.runpytest("--queryguard-sarif=")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--queryguard-sarif needs a path*"])


def test_a_write_error_is_reported_not_raised(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(path: Path, text: str) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(sarif, "atomic_write_text", fail)
    result = project.runpytest("--queryguard-sarif=qg.sarif")
    result.assert_outcomes(failed=2)
    result.stdout.fnmatch_lines(
        ["*queryguard SARIF could not write *qg.sarif: read-only*"]
    )


def test_findings_without_a_location_are_counted_in_the_summary(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pytest_queryguard import plugin
    from pytest_queryguard.collector import QueryCollector
    from pytest_queryguard.events import CallSite, ScanEvent, ScanIssue

    issue = ScanIssue("public", "orders", "Seq Scan", "filter", "(a = $1)", "a")
    # a call site in a file outside the rootdir: no repository location
    outside = CallSite(
        filename=str(pytester.path.parent / "elsewhere" / "app.py"),
        lineno=3,
        function="handler",
    )
    real = QueryCollector

    def make() -> QueryCollector:
        collector = real()
        collector.scans.append(ScanEvent(issue, "SELECT 1", outside))
        return collector

    monkeypatch.setattr(plugin, "QueryCollector", make)
    pytester.makepyfile(test_a="def test_ok():\n    pass\n")
    result = pytester.runpytest(
        "--queryguard-index-check", "--queryguard-sarif=qg.sarif"
    )
    result.stdout.fnmatch_lines(
        ["*qg.sarif: 0 results (1 left out: no location inside the rootdir)"]
    )


# --- URIs relative to the repository root (GITHUB_WORKSPACE) ---


def _uri(document: dict[str, Any]) -> str:
    location = document["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
    return location["artifactLocation"]["uri"]  # type: ignore[no-any-return]


def test_prefix_for_a_rootdir_below_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "validation"
    root.mkdir()
    assert sarif.workspace_prefix(root, str(tmp_path)) == "validation"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    assert sarif.workspace_prefix(nested, str(tmp_path)) == "validation/a/b"


def test_no_prefix_when_rootdir_is_the_workspace(tmp_path: Path) -> None:
    assert sarif.workspace_prefix(tmp_path, str(tmp_path)) == ""


@pytest.mark.parametrize("workspace", [None, "", "  "])
def test_no_prefix_without_a_workspace(tmp_path: Path, workspace: str | None) -> None:
    assert sarif.workspace_prefix(tmp_path, workspace) == ""


def test_no_prefix_for_a_rootdir_outside_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    outside = tmp_path / "elsewhere"
    workspace.mkdir()
    outside.mkdir()
    assert sarif.workspace_prefix(outside, str(workspace)) == ""
    # a sibling whose name starts like the workspace is still outside
    sibling = tmp_path / "repo-other"
    sibling.mkdir()
    assert sarif.workspace_prefix(sibling, str(workspace)) == ""


def test_prefix_resolves_symlinks_on_both_sides(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "validation").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    # workspace given through the link, rootdir through the real path, and back
    assert sarif.workspace_prefix(real / "validation", str(link)) == "validation"
    assert sarif.workspace_prefix(link / "validation", str(real)) == "validation"
    # a rootdir that is a link to a directory inside the workspace
    inner = tmp_path / "inner"
    inner.symlink_to(real / "validation", target_is_directory=True)
    assert sarif.workspace_prefix(inner, str(real)) == "validation"


def test_no_rootdir_means_no_prefix(tmp_path: Path) -> None:
    assert sarif.workspace_prefix(None, str(tmp_path)) == ""


def test_the_prefix_changes_the_uri_and_not_the_fingerprints() -> None:
    store: sarif.Collected = {}
    sarif.add(store, _finding(), "error")
    plain, _ = sarif.build(store, "1.2.3")
    prefixed, _ = sarif.build(store, "1.2.3", "validation")
    assert _uri(plain) == "app/orders.py"
    assert _uri(prefixed) == "validation/app/orders.py"
    assert (
        plain["runs"][0]["results"][0]["partialFingerprints"]
        == prefixed["runs"][0]["results"][0]["partialFingerprints"]
    )


def test_the_prefix_does_not_rescue_absolute_paths() -> None:
    _, omitted = sarif.build(
        {
            _finding(file="/usr/lib/x.py").identity: (
                _finding(file="/usr/lib/x.py"),
                "error",
            )
        },
        "1",
        "validation",
    )
    assert omitted == 1


def test_a_run_below_the_workspace_writes_repository_relative_uris(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the rootdir is the pytester directory; the "repository" is its parent
    monkeypatch.setenv("GITHUB_WORKSPACE", str(project.path.parent))
    project.runpytest("--queryguard-sarif=qg.sarif")
    results = _results(project.path / "qg.sarif")
    uris = {
        r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for r in results
    }
    assert uris == {f"{project.path.name}/test_a.py"}


def test_a_run_with_the_workspace_at_the_rootdir_is_unchanged(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_WORKSPACE", str(project.path))
    project.runpytest("--queryguard-sarif=qg.sarif")
    (first, *_) = _results(project.path / "qg.sarif")
    assert _uri({"runs": [{"results": [first]}]}) == "test_a.py"


def test_a_run_outside_the_workspace_falls_back_to_the_rootdir(
    project: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path_factory.mktemp("elsewhere")))
    project.runpytest("--queryguard-sarif=qg.sarif")
    (first, *_) = _results(project.path / "qg.sarif")
    assert _uri({"runs": [{"results": [first]}]}) == "test_a.py"


OFFLOADED = """
import asyncio

from sqlalchemy import select
from sqlalchemy.orm import Session
from qgmodels import Customer, make_sync_session


def test_session_execute_in_a_worker_thread():
    session = make_sync_session()

    async def handler():
        for customer_id in range(1, 6):
            # the library callable runs in a worker thread: no application frame
            await asyncio.to_thread(
                session.execute, select(Customer).where(Customer.id == customer_id)
            )

    asyncio.run(handler())
"""


def test_a_finding_without_a_call_site_is_located_at_the_test(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(qgmodels=MODELS, test_offload=OFFLOADED)
    result = pytester.runpytest("--queryguard-sarif=qg.sarif")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*first at <unknown location>*"])
    (found,) = _results(pytester.path / "qg.sarif")
    assert found["ruleId"] == "queryguard/repeated-statement"
    physical = found["locations"][0]["physicalLocation"]
    assert physical["artifactLocation"]["uri"] == "test_offload.py"
    assert physical["region"]["startLine"] == 8
    assert found["locations"][0]["logicalLocations"][0]["name"] == (
        "test_session_execute_in_a_worker_thread"
    )
    assert "reported at the test" in found["message"]["text"]
    result.stdout.fnmatch_lines(["*qg.sarif: 1 result*"])
    assert "left out" not in result.stdout.str()


def test_the_test_location_is_only_for_sarif(pytester: pytest.Pytester) -> None:
    # the baseline entry keeps the unknown call site it always had
    pytester.makepyfile(qgmodels=MODELS, test_offload=OFFLOADED)
    pytester.runpytest("--queryguard-baseline=b.json", "--queryguard-update-baseline")
    entries = json.loads((pytester.path / "b.json").read_text())["findings"]
    assert [(e["file"], e["function"]) for e in entries] == [(None, None)]


def test_wrapping_the_call_in_a_lambda_restores_the_call_site(
    pytester: pytest.Pytester,
) -> None:
    # the workaround in docs/limitations.md
    wrapped = OFFLOADED.replace(
        "session.execute, select(Customer).where(Customer.id == customer_id)",
        "lambda: session.execute(select(Customer).where(Customer.id == customer_id))",
    )
    assert wrapped != OFFLOADED
    pytester.makepyfile(qgmodels=MODELS, test_offload=wrapped)
    result = pytester.runpytest("--queryguard-sarif=qg.sarif")
    result.stdout.fnmatch_lines(["*first at test_offload.py:*"])
    assert "<unknown location>" not in result.stdout.str()


EAGER_UNUSED = """
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from qgmodels import Customer, make_sync_session


def test_eager_unused():
    session = make_sync_session()
    for customer in session.scalars(select(Customer).options(selectinload(Customer.orders))):
        _ = customer.name
"""


@pytest.mark.parametrize(("mode", "level"), [("warn", "warning"), ("fail", "error")])
def test_the_level_follows_the_unused_eager_load_mode(
    pytester: pytest.Pytester, mode: str, level: str
) -> None:
    pytester.makepyfile(qgmodels=MODELS, test_eager=EAGER_UNUSED)
    pytester.makeini(f"[pytest]\nqueryguard_unused_eager_load = {mode}\n")
    result = pytester.runpytest("--queryguard-sarif=out.sarif", "-W", "default")
    assert result.ret == (0 if mode == "warn" else 1)
    document = json.loads((pytester.path / "out.sarif").read_text())
    results = document["runs"][0]["results"]
    assert [r["ruleId"] for r in results] == ["queryguard/unused-eager-load"]
    assert results[0]["level"] == level


def test_no_sarif_is_written_after_an_interrupted_run(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(qgmodels=MODELS, test_loops=LOOP)
    pytester.makepyfile(test_broken="import no_such_module_anywhere\n")
    path = pytester.path / "out.sarif"
    path.write_text("previous")
    result = pytester.runpytest("--queryguard-sarif=out.sarif")
    assert result.ret == pytest.ExitCode.INTERRUPTED
    assert path.read_text() == "previous"
    result.stdout.fnmatch_lines(["*queryguard SARIF not written: the run was*"])
