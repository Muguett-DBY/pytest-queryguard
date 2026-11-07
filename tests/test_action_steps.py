"""The shell steps of action/action.yml, run with crafted inputs and a fake gh."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ACTION = Path(__file__).resolve().parent.parent / "action" / "action.yml"

pytestmark = [
    pytest.mark.allow_nplusone,
    pytest.mark.skipif(not ACTION.is_file(), reason="action/ is not available"),
]

# Contexts whose values someone outside the repository's writers can choose
# (a pull request's title, body or branch, a commit message, a comment).
UNTRUSTED = re.compile(
    r"github\.event\.(pull_request|issue|comment|review|review_comment|"
    r"head_commit|commits|discussion|pages|workflow_run)\b|github\.head_ref"
)


def steps() -> list[tuple[str, str]]:
    """(name, text) of each step of the composite action."""
    text = ACTION.read_text(encoding="utf-8")
    body = text.split("\n  steps:\n", 1)[1]
    parts = re.split(r"^    - name: ", body, flags=re.MULTILINE)[1:]
    return [(part.split("\n", 1)[0], part) for part in parts]


def run_block(step: str) -> str:
    """The `run: |` script of a step, dedented as YAML does."""
    match = re.search(r"^      run: \|\n((?:(?: {8}.*)?\n)+)", step, re.MULTILINE)
    assert match, "the step has no run block"
    return "\n".join(line[8:] for line in match.group(1).splitlines()) + "\n"


def script(name: str) -> str:
    for step_name, text in steps():
        if step_name == name:
            return run_block(text)
    raise AssertionError(f"no step {name!r} in action.yml")


def test_no_expression_inside_a_run_block() -> None:
    # A ${{ }} inside run: is pasted into the script before bash reads it, so
    # a branch name or title like a"; curl ... would run. Every value must
    # come in through env:.
    shell_steps = [(n, t) for n, t in steps() if "\n      run: |" in t]
    assert len(shell_steps) >= 8
    for name, text in shell_steps:
        assert "${{" not in run_block(text), name


def test_untrusted_contexts_only_reach_env() -> None:
    for name, text in steps():
        for line in text.splitlines():
            if UNTRUSTED.search(line):
                # an `if:` condition or an env value, never a script line
                stripped = line.strip()
                assert stripped.startswith(("&&", "||", "!(", "if:")) or re.match(
                    r"^[A-Z_]+: \$\{\{", stripped
                ), (name, line)


def test_every_shell_step_uses_bash_and_no_step_runs_after_a_cancel() -> None:
    text = ACTION.read_text(encoding="utf-8")
    # always() keeps a step running after the run was cancelled; !cancelled()
    # still runs it after a failed step
    assert "always()" not in text
    for name, step in steps():
        if "\n      run: |" in step:
            assert "\n      shell: bash\n" in step, name


def test_actions_are_pinned_by_commit() -> None:
    for line in ACTION.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("uses:"):
            assert re.search(r"@[0-9a-f]{40} # v\d", line), line


def run_script(
    name: str,
    tmp_path: Path,
    env: dict[str, str],
    *,
    check: bool = True,
    bin_dir: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    output = tmp_path / "github_output"
    output.write_text("")
    environment = {
        **os.environ,
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        **env,
    }
    if bin_dir is not None:
        environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
    # GitHub runs `bash --noprofile --norc -eo pipefail {0}`
    path = tmp_path / "step.sh"
    path.write_text(script(name))
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(path)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    outputs = dict(
        line.split("=", 1) for line in output.read_text().splitlines() if "=" in line
    )
    return result, outputs


def inputs(**overrides: str) -> dict[str, str]:
    values = {
        "QG_FAIL": "true",
        "QG_UPLOAD": "true",
        "QG_COMPARE": "true",
        "QG_COMMENT": "true",
        "QG_TREND": "false",
        "QG_CATEGORY": "pytest-queryguard",
    }
    values.update(overrides)
    return values


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
class TestInputs:
    def test_the_defaults_pass(self, tmp_path: Path) -> None:
        _, outputs = run_script("Check the inputs", tmp_path, inputs())
        assert outputs["artifact"] == "queryguard-json-pytest-queryguard"

    @pytest.mark.parametrize("value", ["True", "FALSE", "false"])
    def test_any_case_of_true_and_false(self, tmp_path: Path, value: str) -> None:
        run_script("Check the inputs", tmp_path, inputs(QG_FAIL=value))

    @pytest.mark.parametrize(
        "variable", ["QG_FAIL", "QG_UPLOAD", "QG_COMPARE", "QG_COMMENT"]
    )
    @pytest.mark.parametrize("value", ["yes", "1", "", "true "])
    def test_a_switch_that_is_not_true_or_false_stops(
        self, tmp_path: Path, variable: str, value: str
    ) -> None:
        # `inputs.fail == 'true'` would read "yes" as false: a failed test run
        # would pass the job.
        result, _ = run_script(
            "Check the inputs", tmp_path, inputs(**{variable: value}), check=False
        )
        assert result.returncode == 1
        name = variable[3:].lower()
        assert f"::error::input {name} must be" in result.stdout

    @pytest.mark.parametrize(
        ("value", "runs"),
        [
            ("false", ""),
            ("False", ""),
            ("", ""),
            ("true", "10"),
            ("TRUE", "10"),
            ("2", "2"),
            ("08", "8"),
            ("100", "100"),
        ],
    )
    def test_valid_trend(self, tmp_path: Path, value: str, runs: str) -> None:
        _, outputs = run_script("Check the inputs", tmp_path, inputs(QG_TREND=value))
        assert outputs["trend-runs"] == runs

    # 2**64 + 10 would wrap around to 10 in bash's arithmetic
    @pytest.mark.parametrize(
        "value",
        ["1", "0", "101", "0010", "18446744073709551626", "ten", "5 ", "$(id)"],
    )
    def test_invalid_trend(self, tmp_path: Path, value: str) -> None:
        result, _ = run_script(
            "Check the inputs", tmp_path, inputs(QG_TREND=value), check=False
        )
        assert result.returncode == 1
        assert "::error::input trend must be" in result.stdout

    def test_an_empty_category_stops(self, tmp_path: Path) -> None:
        result, _ = run_script(
            "Check the inputs", tmp_path, inputs(QG_CATEGORY=""), check=False
        )
        assert result.returncode == 1

    def test_the_artifact_name_has_no_forbidden_characters(
        self, tmp_path: Path
    ) -> None:
        category = '/language:python|db="pg"*?<>\\x\r\nend'
        _, outputs = run_script(
            "Check the inputs", tmp_path, inputs(QG_CATEGORY=category)
        )
        artifact = outputs["artifact"]
        assert artifact == "queryguard-json--language-python-db=-pg------x--end"
        assert not set('":<>|*?\\/\r\n') & set(artifact)


def fake_python(tmp_path: Path, code: int) -> Path:
    """A `python` that runs `-c` with this interpreter and exits `code` for pytest."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python"
    fake.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-c" ]; then exec "{sys.executable}" "$@"; fi\n'
        'echo "pytest $*" >> "$QG_LOG"\n'
        f"exit {code}\n"
    )
    fake.chmod(0o755)
    return bin_dir


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
class TestRunPytest:
    def run(
        self, tmp_path: Path, code: int, args: str = ""
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, str], str]:
        log = tmp_path / "log"
        log.write_text("")
        result, outputs = run_script(
            "Run pytest",
            tmp_path,
            {
                "QG_ARGS": args,
                "QG_BASELINE": "",
                "QG_SARIF": "queryguard.sarif",
                "QG_JSON": "queryguard.json",
                "QG_LOG": str(log),
            },
            check=False,
            bin_dir=fake_python(tmp_path, code),
        )
        return result, outputs, log.read_text()

    @pytest.mark.parametrize("code", [0, 1, 2, 3, 4, 5])
    def test_the_exit_code_is_an_output_and_the_step_passes(
        self, tmp_path: Path, code: int
    ) -> None:
        result, outputs, log = self.run(tmp_path, code)
        # the step itself passes; "Fail when pytest failed" fails the job
        assert result.returncode == 0, result.stderr
        assert outputs["exit-code"] == str(code)
        assert "--queryguard-sarif=queryguard.sarif" in log
        warned = "::warning::pytest" in result.stdout.replace(
            "::warning::pytest did not write", ""
        )
        assert warned == (code >= 2)

    def test_an_unclosed_quote_does_not_run_pytest(self, tmp_path: Path) -> None:
        result, outputs, log = self.run(tmp_path, 0, args="-k 'oops tests/")
        assert result.returncode == 1
        assert log == ""
        assert "exit-code" not in outputs

    def test_a_shell_payload_is_one_pytest_argument(self, tmp_path: Path) -> None:
        marker = tmp_path / "pwned"
        result, _, log = self.run(
            tmp_path, 0, args=f"-k \"x; touch {marker}\" '$(touch {marker})'"
        )
        assert result.returncode == 0
        assert not marker.exists()
        assert f"-k x; touch {marker} $(touch {marker})" in log


def fake_gh(tmp_path: Path) -> Path:
    """A `gh` that answers from files, applies --jq with the real jq and logs writes."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gh"
    fake.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, shutil, subprocess, sys
args = sys.argv[1:]
log = os.environ["FAKE_LOG"]
if args[:2] == ["api", "user"]:
    login = os.environ.get("FAKE_LOGIN", "")
    if login == "!":
        sys.stderr.write("gh: connection reset by peer\n")
        sys.exit(1)
    if not login:
        sys.stderr.write("gh: Resource not accessible by integration (HTTP 403)\n")
        sys.exit(1)
    print(login)
    sys.exit(0)
if "--paginate" in args:
    if os.environ.get("FAKE_LIST_FAIL"):
        sys.stderr.write("gh: Not Found (HTTP 404)\n")
        sys.exit(1)
    jq = args[args.index("--jq") + 1]
    with open(os.environ["FAKE_COMMENTS"]) as handle:
        pages = json.load(handle)
    for page in pages:
        out = subprocess.run(["jq", "-r", jq], input=json.dumps(page), text=True,
                             capture_output=True, check=True)
        sys.stdout.write(out.stdout)
    sys.exit(0)
with open(log, "a") as handle:
    handle.write(" ".join(a for a in args if not a.startswith("body=")) + "\n")
for a in args:
    if a.startswith("body=@"):
        shutil.copy(a[len("body=@"):], os.environ["FAKE_BODY"])
refused = os.environ.get("FAKE_PATCH_REFUSED", "").split()
if "PATCH" in args and args[args.index("PATCH") + 1].rsplit("/", 1)[1] in refused:
    sys.stderr.write("gh: Forbidden (HTTP 403)\n")
    sys.exit(1)
if os.environ.get("FAKE_WRITE_FAIL"):
    sys.stderr.write("gh: Resource not accessible by integration (HTTP 403)\n")
    sys.exit(1)
"""
    )
    fake.chmod(0o755)
    return bin_dir


MARK = "<!-- pytest-queryguard compare -->"


def comment(id_: int, login: str, body: str, kind: str = "Bot") -> dict[str, object]:
    return {"id": id_, "user": {"login": login, "type": kind}, "body": body}


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
class TestComment:
    def run(
        self,
        tmp_path: Path,
        *,
        report: str = f"{MARK}\n### compared\n\nrows\n",
        trend: str | None = None,
        pages: list[list[dict[str, object]]] | None = None,
        **env: str,
    ) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
        report_file = tmp_path / "report.md"
        report_file.write_text(report, encoding="utf-8")
        trend_file = tmp_path / "trend.md"
        if trend is not None:
            trend_file.write_text(trend, encoding="utf-8")
        comments = tmp_path / "comments.json"
        comments.write_text(json.dumps(pages if pages is not None else [[]]))
        log = tmp_path / "gh.log"
        log.write_text("")
        body = tmp_path / "posted.md"
        tmp = tmp_path / "temp"
        tmp.mkdir()
        result, _ = run_script(
            "Comment the comparison on the pull request",
            tmp_path,
            {
                "QG_REPO": "owner/repo",
                "QG_PR": "7",
                "QG_REPORT": str(report_file),
                "QG_TREND": str(trend_file) if trend is not None else "",
                "QG_TMP": str(tmp),
                "FAKE_LOG": str(log),
                "FAKE_COMMENTS": str(comments),
                "FAKE_BODY": str(body),
                **env,
            },
            check=False,
            bin_dir=fake_gh(tmp_path),
        )
        posted = body.read_text(encoding="utf-8") if body.exists() else ""
        return result, log.read_text().splitlines(), posted

    def test_first_run_posts(self, tmp_path: Path) -> None:
        result, calls, posted = self.run(tmp_path, trend="<details>trend</details>\n")
        assert result.returncode == 0, result.stderr
        assert calls == ["api -X POST repos/owner/repo/issues/7/comments -F"]
        assert posted.startswith(MARK + "\n")
        assert posted.endswith("rows\n\n<details>trend</details>\n")

    def test_a_later_run_updates_its_own_comment(self, tmp_path: Path) -> None:
        pages = [
            [
                # a person quoting the marker, another category, then ours
                comment(1, "alice", MARK + "\nquoted", kind="User"),
                comment(
                    2, "github-actions[bot]", "<!-- pytest-queryguard compare pg -->"
                ),
            ],
            [comment(3, "github-actions[bot]", MARK + "\nold")],
        ]
        result, calls, _ = self.run(tmp_path, pages=pages)
        assert result.returncode == 0, result.stderr
        assert calls == ["api -X PATCH repos/owner/repo/issues/comments/3 -F"]

    def test_a_personal_token_finds_the_comment_of_its_user(
        self, tmp_path: Path
    ) -> None:
        # Before, only github-actions[bot] was looked for, so a custom token
        # posted a new comment on every push.
        pages = [
            [
                comment(4, "github-actions[bot]", MARK + "\nother"),
                comment(5, "ci-user", MARK + "\nold", kind="User"),
            ]
        ]
        result, calls, _ = self.run(tmp_path, pages=pages, FAKE_LOGIN="ci-user")
        assert result.returncode == 0, result.stderr
        assert calls == ["api -X PATCH repos/owner/repo/issues/comments/5 -F"]

    def test_an_app_token_finds_the_comment_of_its_bot(self, tmp_path: Path) -> None:
        pages = [[comment(6, "my-app[bot]", MARK + "\nold")]]
        result, calls, _ = self.run(tmp_path, pages=pages)
        assert result.returncode == 0, result.stderr
        assert calls == ["api -X PATCH repos/owner/repo/issues/comments/6 -F"]

    def test_a_comment_this_token_may_not_edit_is_passed_over(
        self, tmp_path: Path
    ) -> None:
        # an earlier run with another bot's token; newest first, then a new one
        pages = [
            [
                comment(8, "github-actions[bot]", MARK + "\nold"),
                comment(9, "my-app[bot]", MARK + "\nnewer"),
            ]
        ]
        result, calls, _ = self.run(tmp_path, pages=pages, FAKE_PATCH_REFUSED="9 8")
        assert result.returncode == 0, result.stderr
        assert calls == [
            "api -X PATCH repos/owner/repo/issues/comments/9 -F",
            "api -X PATCH repos/owner/repo/issues/comments/8 -F",
            "api -X POST repos/owner/repo/issues/7/comments -F",
        ]
        assert "::warning" not in result.stdout

    def test_a_failed_user_lookup_writes_nothing(self, tmp_path: Path) -> None:
        # not a 403: a personal token whose user is unknown for the moment
        # must not fall back to bots and post a second comment
        pages = [[comment(5, "ci-user", MARK + "\nold", kind="User")]]
        result, calls, _ = self.run(tmp_path, pages=pages, FAKE_LOGIN="!")
        assert result.returncode == 0, result.stderr
        assert calls == []
        assert "::warning::the token's user could not be looked up" in result.stdout

    def test_a_refused_write_is_a_warning(self, tmp_path: Path) -> None:
        result, _, _ = self.run(tmp_path, FAKE_WRITE_FAIL="1")
        assert result.returncode == 0, result.stderr
        assert "::warning::the pull-request comment was not written" in result.stdout
        assert "pull-requests: write" in result.stdout

    def test_a_refused_listing_is_a_warning(self, tmp_path: Path) -> None:
        result, calls, _ = self.run(tmp_path, FAKE_LIST_FAIL="1")
        assert result.returncode == 0, result.stderr
        assert calls == []
        assert "::warning::the pull-request comments could not be listed" in (
            result.stdout
        )

    def test_a_trend_that_does_not_fit_is_left_out(self, tmp_path: Path) -> None:
        trend = "<details>\n" + "| `t` | ▁▂▃ |\n" * 6000 + "</details>\n"
        assert len(trend) > 65536
        result, _, posted = self.run(tmp_path, trend=trend)
        assert result.returncode == 0, result.stderr
        assert len(posted) <= 65536
        assert "rows\n" in posted
        assert "The trend is in the job summary" in posted
        assert "▁▂▃" not in posted

    def test_a_huge_comparison_is_cut_at_a_line(self, tmp_path: Path) -> None:
        line = "| `tests/test_x.py::test[ż" + "x" * 80 + "]` | 1 | 2 | +1 |\n"
        report = f"{MARK}\n### compared\n\n" + line * 2000
        assert len(report) > 65536
        result, calls, posted = self.run(tmp_path, report=report)
        assert result.returncode == 0, result.stderr
        assert len(posted) <= 65536
        assert posted.startswith(MARK + "\n")
        kept = posted.split("\n\n_The whole comparison", 1)[0]
        assert kept.endswith("+1 |\n")
        assert "The whole comparison is in the job summary" in posted
        assert calls == ["api -X POST repos/owner/repo/issues/7/comments -F"]

    def test_a_cut_inside_details_closes_it(self, tmp_path: Path) -> None:
        line = "| `tests/test_x.py::test_" + "y" * 100 + "` | 3 | 2 | -1 |\n"
        report = (
            f"{MARK}\n### compared\n\n<details><summary>fewer</summary>\n\n"
            + line * 1000
            + "\n</details>\n"
        )
        result, _, posted = self.run(tmp_path, report=report)
        assert result.returncode == 0, result.stderr
        assert len(posted) <= 65536
        assert posted.count("<details>") == posted.count("</details>") == 1
        assert posted.index("</details>") < posted.index("_The whole comparison")
