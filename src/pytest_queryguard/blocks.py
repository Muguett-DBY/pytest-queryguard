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
import threading
from collections.abc import Callable, Iterable
from contextvars import ContextVar, Token
from fnmatch import fnmatchcase
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from .events import CallSite
from .ignores import check_kind

if TYPE_CHECKING:
    from typing_extensions import Self

_F = TypeVar("_F", bound=Callable[..., Any])


class IgnoreBlock:
    """The context manager ``ignore()`` returns; also a decorator.

    Not constructible by users: call ``ignore()``.
    """

    __slots__ = ("_matched", "kinds", "reason", "site")

    def __init__(
        self, kinds: tuple[str, ...], reason: str | None, site: CallSite | None
    ) -> None:
        self.kinds = kinds
        self.reason = reason
        self.site = site  # where ignore() was called: the with or the decorator
        self._matched: dict[str, bool] = {}  # kind -> matches, filled on use

    def matches(self, kind: str) -> bool:
        matched = self._matched.get(kind)
        if matched is None:
            matched = any(fnmatchcase(kind, pattern) for pattern in self.kinds)
            self._matched[kind] = matched
        return matched

    def __enter__(self) -> Self:
        _blocks.set((*_blocks.get(), self))
        recording = _recording.get()
        if recording is not None and self.site is not None:
            # for the stale-block report: only in the context of a test the
            # plugin measures (see record_entered)
            entered = recording.blocks_entered
            key = (self.site.filename, self.site.lineno)
            if key not in entered:
                with lock:
                    entered.setdefault(key, self)
        return self

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

# Set by the plugin around a measured test: its collector. Its
# ``blocks_entered`` maps (filename, line) to a block entered there, in the
# test's context (its thread, its tasks, and threads that run in a copy of
# it); its ``blocks_used`` holds the blocks that left out a finding in a check
# run in that context (``QueryLog.assert_no_nplusone``, an in-app report),
# whose own Sieve the plugin never sees. Threads left over from other tests
# and code outside a measured test do not see it.
Entered = dict[tuple[str, int], IgnoreBlock]
Used = dict[IgnoreBlock, None]  # ordered set


class Recording(Protocol):
    blocks_entered: Entered
    blocks_used: Used | None  # None until a check uses a block: nothing per test


_recording: ContextVar[Recording | None] = ContextVar(
    "pytest_queryguard_ignore_recording", default=None
)
# threads given the context may enter or use blocks at once
lock = threading.Lock()


def record_entered(target: Recording) -> Token[Recording | None]:
    """Note the blocks entered, and those used, from now on in this context."""
    return _recording.set(target)


def stop_recording(token: Token[Recording | None]) -> None:
    try:
        _recording.reset(token)
    except ValueError:  # reset in another context than the one that set it
        _recording.set(None)


class _Tagged(Protocol):
    @property
    def ignores(self) -> tuple[IgnoreBlock, ...]: ...


def covering(events: Iterable[_Tagged], kind: str) -> tuple[IgnoreBlock, ...]:
    """The blocks that leave out a finding of ``kind`` made of ``events``.

    Empty unless every event was recorded inside a block for ``kind``; then
    the innermost such block of each event, in order of first use.
    """
    return covering_tags((event.ignores for event in events), kind)


def covering_tags(
    runs: Iterable[tuple[IgnoreBlock, ...]], kind: str
) -> tuple[IgnoreBlock, ...]:
    """``covering`` for the tags of each event (or run) instead of the events."""
    found: dict[IgnoreBlock, None] = {}
    for tags in runs:
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


class Sieve:
    """Leaves out the findings made inside ignore() blocks; notes the blocks."""

    __slots__ = ("used",)

    def __init__(self) -> None:
        self.used: dict[IgnoreBlock, None] = {}  # ordered set

    def note(self, found: Iterable[IgnoreBlock]) -> None:
        new = dict.fromkeys(found)
        if not new:
            return
        self.used.update(new)
        recording = _recording.get()
        if recording is not None:
            # a check run inside a measured test: the test's stale-block
            # report counts the blocks too (see record_entered)
            with lock:
                if recording.blocks_used is None:
                    recording.blocks_used = new
                else:
                    recording.blocks_used.update(new)

    def drops(self, events: Iterable[_Tagged], kind: str) -> bool:
        """True when the finding made of ``events`` is left out (see ``covering``)."""
        return self.drops_tags((event.ignores for event in events), kind)

    def drops_tags(self, runs: Iterable[tuple[IgnoreBlock, ...]], kind: str) -> bool:
        """``drops`` for the tags of each event (or run) instead of the events."""
        found = covering_tags(runs, kind)
        self.note(found)
        return bool(found)
