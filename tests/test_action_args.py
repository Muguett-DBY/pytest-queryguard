"""The action splits `pytest-args` with shlex and never evaluates it in a shell."""

from __future__ import annotations

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
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required"),
    # the action runs on Linux and macOS; symlinks need privileges on Windows
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell"),
]


def splitting_snippet() -> str:
    """The lines of the real action that turn QG_ARGS into the `args` array."""
    text = ACTION.read_text(encoding="utf-8")
    # from `args=()` to the `fi` at the same indentation
    match = re.search(r"^( *)args=\(\)\n.*?^\1fi\n", text, re.MULTILINE | re.DOTALL)
    assert match, "the args splitting block is missing from action.yml"
    run_pytest = text.split("- name: Run pytest", 1)[1].split("- name: Find the SARIF")[
        0
    ]
    assert not re.search(r"\beval\b", run_pytest)
    return match.group(0)


def split_with_bash(qg_args: str, tmp_path: Path) -> tuple[list[str], str]:
    script = splitting_snippet() + 'printf "%s\\n" "${args[@]}"\n'
    environment = {**os.environ, "QG_ARGS": qg_args}
    # `python` in the action is whatever is on PATH; use this interpreter
    link = tmp_path / "bin"
    link.mkdir()
    (link / "python").symlink_to(sys.executable)
    environment["PATH"] = f"{link}{os.pathsep}{environment['PATH']}"
    result = subprocess.run(
        ["bash", "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines(), result.stderr


def test_quoted_words_stay_together(tmp_path: Path) -> None:
    words, _ = split_with_bash('-k "a or b" -x tests/', tmp_path)
    assert words == ["-k", "a or b", "-x", "tests/"]


def test_empty_input_gives_no_arguments(tmp_path: Path) -> None:
    words, _ = split_with_bash("", tmp_path)
    assert [w for w in words if w] == []


def test_command_substitution_and_backticks_are_not_executed(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    payload = f'-k "a or b" $(touch {marker}) `touch {marker}` $HOME'
    words, stderr = split_with_bash(payload, tmp_path)
    assert not marker.exists()
    assert words == [
        "-k",
        "a or b",
        "$(touch",
        f"{marker})",
        "`touch",
        f"{marker}`",
        "$HOME",
    ]
    assert stderr == ""


def install_snippet() -> str:
    """The shell of the real action's install step."""
    text = ACTION.read_text(encoding="utf-8")
    match = re.search(
        r"- name: Install pytest-queryguard\n.*?run: \|\n(.*?)\n\n    - name:",
        text,
        re.DOTALL,
    )
    assert match, "the install step is missing from action.yml"
    return "\n".join(line[8:] for line in match.group(1).splitlines())


def run_install(tmp_path: Path, package: str, action_path: Path) -> list[str]:
    """Run the install step with a `python` that only records its arguments."""
    link = tmp_path / "bin"
    link.mkdir()
    fake = link / "python"
    fake.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
    fake.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", install_snippet()],
        env={
            **os.environ,
            "PATH": f"{link}{os.pathsep}{os.environ['PATH']}",
            "QG_PACKAGE": package,
            "QG_ACTION_PATH": str(action_path),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def test_default_package_is_the_checkout_of_the_action(tmp_path: Path) -> None:
    # The default must not be a name pip resolves on its own: an older release
    # has no --queryguard-sarif (issue 106).
    assert re.search(r"^    default: \"\"$", package_input(), re.MULTILINE)
    action_path = ACTION.parent
    words = run_install(tmp_path, "", action_path)
    assert words[:4] == ["-m", "pip", "install", "--disable-pip-version-check"]
    installed = Path(words[4]).resolve()
    assert installed == action_path.parent.resolve()
    assert (installed / "pyproject.toml").is_file()
    assert (installed / "src" / "pytest_queryguard" / "__init__.py").is_file()


def test_an_explicit_package_is_installed_as_given(tmp_path: Path) -> None:
    words = run_install(tmp_path, "pytest-queryguard==0.4.0", ACTION.parent)
    assert words[4:] == ["pytest-queryguard==0.4.0"]


def package_input() -> str:
    text = ACTION.read_text(encoding="utf-8")
    return text.split("  package:", 1)[1].split("\n  baseline:", 1)[0]


def test_an_unclosed_quote_stops_the_step(tmp_path: Path) -> None:
    # Before, the failure of the splitting went unnoticed and pytest ran
    # without any of the arguments: the whole suite, without -k or paths.
    with pytest.raises(subprocess.CalledProcessError) as raised:
        split_with_bash('-k "a or b tests/', tmp_path)
    assert "::error::pytest-args cannot be split" in raised.value.stdout


def test_non_ascii_words_survive(tmp_path: Path) -> None:
    words, _ = split_with_bash("-k 'zażółć or b' tests/", tmp_path)
    assert words == ["-k", "zażółć or b", "tests/"]
