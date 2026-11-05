"""``ignore()``: leave out the findings of a block of code (#234).

The collector is process-wide (the code under test may run in another
thread), so it cannot tell which block a statement belongs to. The block is
told apart by a context variable instead: entering it adds it to the blocks of
the current thread or asyncio task, and every event the listeners record
carries the blocks active where it was recorded (one ``ContextVar.get`` per
event). A finding is left out when every event it counts was recorded inside a
block for its kind. A thread started inside the block does not see it unless
it runs in a copy of the context (``asyncio.to_thread``,
``contextvars.copy_context().run``); a task created inside it does.
"""

from __future__ import annotations

import functools
import inspect
import sys
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from fnmatch import fnmatchcase
from types import TracebackType
from typing import Any, Protocol, TypeVar

from . import collector
from .events import CallSite
from .ignores import check_kind

_F = TypeVar("_F", bound=Callable[..., Any])


class IgnoreBlock:
    """The context manager ``ignore()`` returns; also a decorator.

    Not constructible by users: call ``ignore()``.
    """

    __slots__ = ("kinds", "reason", "site")

    def __init__(
        self, kinds: tuple[str, ...], reason: str | None, site: CallSite | None
    ) -> None:
        self.kinds = kinds
        self.reason = reason
        self.site = site  # where ignore() was called: the with or the decorator

    def matches(self, kind: str) -> bool:
        return any(fnmatchcase(kind, pattern) for pattern in self.kinds)

    def __enter__(self) -> None:
        _blocks.set((*_blocks.get(), self))
        if collector.active() and self.site is not None:
            # for the stale-block report; a block entered while nothing is
            # measured could not have ignored anything
            _entered.setdefault((self.site.filename, self.site.lineno), self)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Take out this block's last entry in the current context, without a
        # token: one IgnoreBlock may be entered in several tasks at once (a
        # decorated coroutine function), each with its own context.
        stack = _blocks.get()
        for i in range(len(stack) - 1, -1, -1):
            if stack[i] is self:
                _blocks.set(stack[:i] + stack[i + 1 :])
                break

    def __call__(self, func: _F) -> _F:
        """Run every call of ``func`` (a function or a coroutine function) in the block."""
        if inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func):
            raise TypeError(
                "ignore() cannot decorate a generator function: the block would "
                "end before the generator runs; use a with block inside it"
            )
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def run_async(*args: Any, **kwargs: Any) -> Any:
                with self:
                    return await func(*args, **kwargs)

            return run_async  # type: ignore[return-value]

        @functools.wraps(func)
        def run(*args: Any, **kwargs: Any) -> Any:
            with self:
                return func(*args, **kwargs)

        return run  # type: ignore[return-value]

    def describe(self) -> str:
        """``ignore('relationship-load')`` as written, for the stale-block report."""
        return f"ignore({', '.join(repr(kind) for kind in self.kinds)})"

    def __repr__(self) -> str:
        where = (
            "" if self.site is None else f" at {self.site.filename}:{self.site.lineno}"
        )
        return f"<{self.describe()}{where}>"


def ignore(*kinds: str, reason: str | None = None) -> IgnoreBlock:
    """Leave out the findings of these kinds that happen inside the block.

    ``kinds`` are finding kinds (``"relationship-load"``, ...) or ``fnmatch``
    patterns for them (``"*"`` for all). A finding is left out when every
    statement or load it counts ran inside the block, in the thread or asyncio
    task that entered it (or in a task created inside it). Also a decorator.
    ``reason`` is for the reader. Budgets are not affected.
    """
    if not kinds:
        raise TypeError(
            "ignore() needs at least one finding kind, such as "
            "'relationship-load', or '*' for every kind"
        )
    for kind in kinds:
        if not isinstance(kind, str) or not kind:
            raise TypeError(f"a finding kind must be a non-empty string, got {kind!r}")
        check_kind(kind)
    if reason is not None and not isinstance(reason, str):
        raise TypeError(f"reason must be a string, got {reason!r}")
    frame = sys._getframe(1)
    site = CallSite(
        filename=frame.f_code.co_filename,
        lineno=frame.f_lineno,
        function=frame.f_code.co_name,
    )
    return IgnoreBlock(kinds, reason, site)


# The blocks the current thread or task is in, innermost last.
_blocks: ContextVar[tuple[IgnoreBlock, ...]] = ContextVar(
    "pytest_queryguard_ignore", default=()
)
# What the listeners store on each event; bound once, called per event.
current = _blocks.get

# (filename, line) -> a block entered there while a collector was active,
# since the plugin last took them (once per test).
_entered: dict[tuple[str, int], IgnoreBlock] = {}


def take_entered() -> list[IgnoreBlock]:
    """The blocks entered since the last call, one per call site; clears them."""
    found = list(_entered.values())
    _entered.clear()
    return found


class _Tagged(Protocol):
    @property
    def ignores(self) -> tuple[IgnoreBlock, ...]: ...


def covering(events: Iterable[_Tagged], kind: str) -> tuple[IgnoreBlock, ...]:
    """The blocks that leave out a finding of ``kind`` made of ``events``.

    Empty unless every event was recorded inside a block for ``kind``; then
    the innermost such block of each event, in order of first use.
    """
    found: dict[IgnoreBlock, None] = {}
    for event in events:
        tags = event.ignores
        if not tags:
            return ()
        for block in reversed(tags):
            if block.matches(kind):
                found[block] = None
                break
        else:
            return ()
    return tuple(found)


def covers(tags: tuple[IgnoreBlock, ...], kind: str) -> bool:
    """True when one of ``tags`` (an event's blocks) is a block for ``kind``."""
    return any(block.matches(kind) for block in tags)


def site_key(block: IgnoreBlock) -> tuple[str, int] | None:
    site = block.site
    return None if site is None else (site.filename, site.lineno)


class Sieve:
    """Leaves out the findings made inside ignore() blocks; notes the blocks."""

    __slots__ = ("used",)

    def __init__(self) -> None:
        self.used: dict[IgnoreBlock, None] = {}  # ordered set

    def note(self, found: Iterable[IgnoreBlock]) -> None:
        self.used.update(dict.fromkeys(found))

    def drops(self, events: Iterable[_Tagged], kind: str) -> bool:
        """True when the finding made of ``events`` is left out (see ``covering``)."""
        found = covering(events, kind)
        self.note(found)
        return bool(found)
