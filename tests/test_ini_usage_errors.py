"""A bad ini value is reported with the option's name on every pytest.

pytest 9.2 raises ``UsageError`` from ``Config.getini()`` for a value that
does not fit the option's type, where older releases raised ``TypeError`` or
``ValueError``. Each reader in plugin.py turns all three into its own
message; this runs the ``UsageError`` path on whatever pytest is installed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "src" / "pytest_queryguard" / "plugin.py"
OPTIONS = sorted(
    set(re.findall(r'parser\.addini\(\s*"(queryguard_\w+)"', PLUGIN.read_text()))
)

CONFTEST = """
import pytest

@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    real = config.getini

    def getini(name):
        if name == {name!r}:
            raise pytest.UsageError("rejected by pytest")
        return real(name)

    config.getini = getini
"""


@pytest.mark.parametrize("name", OPTIONS)
def test_a_usage_error_from_getini_names_the_option(
    pytester: pytest.Pytester, name: str
) -> None:
    pytester.makeconftest(CONFTEST.format(name=name))
    pytester.makepyfile("def test_nothing(): pass")
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([f"*{name}*rejected by pytest*"])
