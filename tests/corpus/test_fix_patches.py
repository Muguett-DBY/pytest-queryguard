"""The patch of ``--queryguard-write-patch`` on the corpus: it applies and fixes.

Every BUG scenario with a relationship-load runs once with
``--queryguard-write-patch``, in a copy of the corpus inside the session's
rootdir (the patch only touches files there). The patch is applied with
``git apply`` and the same scenarios run again. A scenario the patch changed
must then have no relationship-load and no finding it did not have before
(a ``joinedload`` next to another joined collection would be a
cartesian-load). Which scenarios get a patch is part of the corpus: a module
with ``PATCH = True`` expects one, every other one expects none, so a patch
that appears or disappears shows up here.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from . import kit, runner

pytest.importorskip("greenlet")
pytest.importorskip("aiosqlite")
pytest.importorskip("fastapi")
pytest.importorskip("flask_sqlalchemy")
pytest.importorskip("factory")

_GIT = shutil.which("git")
_FILES = re.compile(r"^\+\+\+ b/(\S+)$", re.MULTILINE)
_SCENARIO = re.compile(r"tests/corpus/scenarios/(\w+)\.py")


def _copy_corpus(directory: Path) -> None:
    tests = directory / "tests"
    tests.mkdir(parents=True)
    shutil.copy(runner.ROOT / "tests" / "__init__.py", tests / "__init__.py")
    shutil.copytree(
        runner.ROOT / "tests" / "corpus",
        tests / "corpus",
        ignore=shutil.ignore_patterns("__pycache__", "test_*.py"),
    )


def _git(directory: Path, *args: str) -> None:
    assert _GIT is not None
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(directory.parent)}
    subprocess.run(
        [_GIT, *args], cwd=directory, check=True, env=env, capture_output=True
    )


@pytest.mark.skipif(_GIT is None, reason="git is not installed")
@pytest.mark.skipif(
    kit.pg_url_from_env() is not None, reason="the patch run uses SQLite files"
)
def test_patches_apply_and_remove_the_n_plus_one(tmp_path: Path) -> None:
    scenarios = [
        s for s in runner.load() if s.verdict == kit.BUG and kit.REL in s.kinds
    ]
    names = [s.name for s in scenarios]
    directory = tmp_path / "session"
    _copy_corpus(directory)
    # the copy, not the checkout, is what the session imports and patches
    env = {"PYTHONPATH": f"{directory}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"}
    before = runner.run_subprocess(
        directory,
        names,
        args=("--queryguard-write-patch=fix.patch",),
        extra_env=env,
    )
    patch = (directory / "fix.patch").read_text(encoding="utf-8")
    patched = set()
    for file in _FILES.findall(patch):
        match = _SCENARIO.fullmatch(file)
        # a shared module (a domain, kit.py) would change other scenarios too
        assert match is not None, f"the patch changes {file}"
        patched.add(match.group(1))
    expected = {s.name for s in scenarios if getattr(s.module, "PATCH", False)}
    assert patched == expected, (
        f"patched but not expected: {sorted(patched - expected)}; "
        f"expected but not patched: {sorted(expected - patched)}"
    )
    assert all(kit.REL in before[name].kinds for name in patched)
    if not patched:
        return
    _git(directory, "apply", "--check", "fix.patch")
    _git(directory, "apply", "fix.patch")
    after = runner.run_subprocess(directory / "again", names, extra_env=env)
    for name in names:
        result = after[name]
        assert result.failed == bool(result.findings), name
        if name in patched:
            assert kit.REL not in result.kinds, (name, result.findings)
            assert result.kinds <= before[name].kinds, (name, result.findings)
        else:
            assert result.kinds == before[name].kinds, (name, result.findings)
