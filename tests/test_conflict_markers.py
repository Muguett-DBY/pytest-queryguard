"""No merge conflict markers in the files the repository tracks.

Git commits them without complaint; a resolved-looking CHANGELOG with
``<<<<<<<`` in it once reached a pull request.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MARKER = re.compile(r"^(<{7}|>{7})( |$)|^={7}$", re.MULTILINE)
SUFFIXES = {".py", ".md", ".toml", ".txt", ".yml", ".yaml", ".json", ".cfg", ".ini"}


def _tracked() -> list[Path]:
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(
        [git, "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode()
    return [ROOT / name for name in out.split("\0") if name]


def test_no_conflict_markers() -> None:
    found = []
    for path in _tracked():
        if path.suffix not in SUFFIXES or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in MARKER.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{path.relative_to(ROOT)}:{line}")
    assert not found, "merge conflict markers: " + ", ".join(found)
