from __future__ import annotations

import sys
import warnings
from types import FrameType
from typing import Any

# The release that removes the names deprecated before 1.0 (docs/stability.md).
REMOVED_IN = "2.0"


class QueryGuardWarning(UserWarning):
    """An N+1 query pattern or a missing index found in ``warn`` mode."""


class QueryGuardInternalWarning(UserWarning):
    """A bug inside pytest-queryguard; the plugin stops measuring.

    Also issued when a ``record_queries()`` block ends after such an error:
    its ``QueryLog`` may then be incomplete, and ``assert_no_nplusone()``
    raises ``RuntimeError`` instead of checking it.
    """


class QueryGuardDeprecationWarning(DeprecationWarning):
    """A pytest-queryguard option, name or behaviour that a later release removes.

    Not a ``QueryGuardWarning``: a ``filterwarnings`` entry that turns findings
    into errors (or ignores them) does not catch a deprecation, and the other
    way round.
    """


def warn_internal(warning: QueryGuardInternalWarning, stacklevel: int = 1) -> None:
    """Issue ``warning``; a warnings "error" filter never raises it.

    A bug in the plugin must not fail the user's test. With ``-W error`` or
    ``filterwarnings = error`` the warning would be raised as an exception, so
    it is issued once more with an "always" filter for its class: it is still
    shown and recorded, never raised. An "ignore" filter is respected.
    """
    try:
        warnings.warn(warning, stacklevel=stacklevel + 1)
    except QueryGuardInternalWarning:
        with warnings.catch_warnings():
            warnings.simplefilter("always", QueryGuardInternalWarning)
            warnings.warn(warning, stacklevel=stacklevel + 1)


class _Default(str):
    """A default value, told apart from the same value passed explicitly."""


def default(value: str) -> str:
    """``value`` as a default that ``renamed()`` can tell from an explicit one."""
    return _Default(value)


def warn_deprecated(message: str) -> None:
    """Issue ``QueryGuardDeprecationWarning`` at the first caller outside the package.

    The warning then names the user's line (once per call site under the
    default filters), however many frames of the package lie in between.
    """
    level = 1
    frame: FrameType | None = sys._getframe(0)
    while frame is not None and str(frame.f_globals.get("__name__", "")).startswith(
        "pytest_queryguard"
    ):
        frame = frame.f_back
        level += 1
    warnings.warn(message, QueryGuardDeprecationWarning, stacklevel=level)


def renamed(new_name: str, new: Any, old_name: str, old: Any) -> Any:
    """The value of an argument renamed from ``old_name`` to ``new_name``.

    ``new`` must have a ``default()`` as its default and ``old`` None. Passing
    ``old_name`` warns; passing both with different values is a ``ValueError``.
    """
    if old is None:
        return str(new) if isinstance(new, _Default) else new
    warn_deprecated(
        f"{old_name}= is deprecated and will be removed in pytest-queryguard "
        f"{REMOVED_IN}; use {new_name}= instead"
    )
    if not isinstance(new, _Default) and new != old:
        raise ValueError(
            f"{new_name}={new!r} and its deprecated name {old_name}={old!r} "
            f"disagree; pass only {new_name}="
        )
    return old
