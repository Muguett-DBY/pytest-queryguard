from __future__ import annotations

import itertools
import os
import sys
import sysconfig
from collections.abc import Iterator
from pathlib import Path
from types import CodeType, FrameType

from ._compat import FLUSH_CONTEXT, FLUSH_FUNCTION, SESSION_FILE
from .events import CallSite


def _package_dir(name: str) -> str | None:
    module = sys.modules.get(name)
    file = getattr(module, "__file__", None)
    return str(Path(file).resolve().parent) + os.sep if file else None


# Packages whose frames are never the application's. pluggy is also where the
# walk for the callers stops (see _callers).
_HOOK_PACKAGE = "pluggy"
_INTERNAL_PACKAGES = ("sqlalchemy", "pytest", "_pytest", _HOOK_PACKAGE)


def _compute_prefixes() -> tuple[str, ...]:
    paths: list[str | None] = [_package_dir(name) for name in _INTERNAL_PACKAGES]
    paths.append(str(Path(__file__).resolve().parent) + os.sep)
    for key in ("stdlib", "platstdlib"):
        path = sysconfig.get_paths().get(key)
        if path:
            paths.append(path.rstrip(os.sep) + os.sep)
            paths.append(str(Path(path).resolve()) + os.sep)
    return tuple(dict.fromkeys(os.path.normcase(p) for p in paths if p))


_prefixes: tuple[str, ...] | None = None
# filename -> internal?  A suite touches a few hundred files at most, and the
# answer never changes while the process runs, so each is classified once.
_internal: dict[str, bool] = {}


def _classify(filename: str) -> bool:
    global _prefixes
    if filename.startswith("<"):
        return True
    if _prefixes is None:
        _prefixes = _compute_prefixes()
    if os.path.normcase(filename).startswith(_prefixes):
        return True
    parts = Path(filename).parts
    return "site-packages" in parts or "dist-packages" in parts


def _is_internal(filename: str) -> bool:
    try:
        return _internal[filename]
    except KeyError:
        result = _internal[filename] = _classify(filename)
        return result


# code object -> is it a method of a Session subclass? A Session subclass in the
# application's code (SQLModel's, installed in editable mode, or an app's own
# override of execute()) is part of the session: every query of the test would
# otherwise share its `super().execute()` line as call site, and lookups made
# at different lines would look like one query line returning many rows.
# Decided once per code object, from the `self` of its first run: a mixin
# method shared by a Session subclass and another class keeps that answer.
_session_methods: dict[CodeType, bool] = {}
_MAX_SESSION_METHODS = 4096


def _session_classes() -> tuple[type, ...]:
    classes: list[type] = []
    # only modules already loaded: an instance of one has imported it
    for module_name, name in (
        ("sqlalchemy.orm.session", "Session"),
        ("sqlalchemy.ext.asyncio.session", "AsyncSession"),
    ):
        cls = getattr(sys.modules.get(module_name), name, None)
        if isinstance(cls, type):
            classes.append(cls)
    return tuple(classes)


def _is_session_method(frame: FrameType) -> bool:
    """Whether ``frame`` runs a method of a ``Session`` or ``AsyncSession`` subclass."""
    code = frame.f_code
    try:
        return _session_methods[code]
    except KeyError:
        pass
    result = False
    if code.co_argcount and code.co_varnames[0] == "self":
        try:
            this = frame.f_locals.get("self")
            result = isinstance(this, _session_classes())
        except Exception:  # noqa: BLE001  # a plugin bug must never break the user suite
            result = False
    if len(_session_methods) >= _MAX_SESSION_METHODS:
        _session_methods.clear()
    _session_methods[code] = result
    return result


def _parent_greenlet_frames() -> Iterator[FrameType]:
    """Frames of the greenlets above the current one, innermost first.

    SQLAlchemy runs the sync code behind an async session in a child greenlet,
    so the awaiting user code is only reachable through the parent greenlet.
    """
    # greenlet comes with SQLAlchemy's async support; only used if already loaded.
    # Code running inside a SQLAlchemy greenlet has imported it already, while
    # an import here, after pytester purges sys.modules between runs, could
    # create a second greenlet module object next to the one SQLAlchemy uses.
    module = sys.modules.get("greenlet")
    if module is None:
        return
    try:
        parent = module.getcurrent().parent
        while parent is not None:
            frame = parent.gr_frame
            if frame is not None:
                yield frame
            parent = parent.parent
    except Exception:  # noqa: BLE001  # a plugin bug must never break the user suite
        return


# (filename, line, function) -> CallSite.  A suite runs the same few hundred
# lines over and over, and CallSite is immutable, so one object per location
# is shared instead of building a new one for every statement. Bounded, so
# generated code cannot grow it without limit. Emptied when a measurement
# starts (new_measurement()), so that the first statement of each line in it
# builds a new CallSite with that run's callers. The in-app mode passes a cache
# of each unit's own instead (find_callsite(sites)): its units overlap.
Sites = dict[tuple[str, int, str], CallSite]
_sites: dict[tuple[str, int, str], CallSite] = {}
_MAX_SITES = 4096
DEFAULT_STACK_DEPTH = 3
_stack_depth = DEFAULT_STACK_DEPTH


def new_measurement() -> None:
    """Build each call site again, with its callers, the next time its line runs.

    The CallSite objects already handed out keep their callers: an outer
    measurement's events are not changed by an inner one starting.
    """
    _sites.clear()


def swap_stack_depth(depth: int) -> int:
    """Set how many callers are recorded per call site; return the previous value."""
    global _stack_depth
    old, _stack_depth = _stack_depth, depth
    return old


_hook_prefix: str | None = None


def _hook_caller() -> str:
    """The directory of pluggy, which calls the test function and the fixtures."""
    global _hook_prefix
    if _hook_prefix is None:
        _hook_prefix = os.path.normcase(_package_dir(_HOOK_PACKAGE) or "")
    return _hook_prefix


# Before 3.12 a list, dict or set comprehension runs in a frame of its own,
# called by the function it is written in, at the same place (PEP 709 inlined
# them). Reports name that function, as they do on 3.12 and later.
_INLINED = (
    frozenset({"<listcomp>", "<dictcomp>", "<setcomp>"})
    if sys.version_info < (3, 12)
    else frozenset()
)


def _outer(frame: FrameType) -> FrameType:
    """The frame of the function a comprehension is written in, or ``frame``."""
    while frame.f_code.co_name in _INLINED:
        back = frame.f_back
        if back is None or back.f_code.co_filename != frame.f_code.co_filename:
            break
        frame = back
    return frame


def _callers(frame: FrameType | None, depth: int) -> tuple[CallSite, ...]:
    """Up to ``depth`` application frames from ``frame`` outwards.

    The walk ends at the first pluggy frame: the hook call that ran the test
    or the fixture. Above it is pytest itself and its console script.
    """
    found: list[CallSite] = []
    stop = _hook_caller()
    while frame is not None and len(found) < depth:
        code = frame.f_code
        if stop and os.path.normcase(code.co_filename).startswith(stop):
            break
        if not _is_internal(code.co_filename) and not _is_session_method(frame):
            if code.co_name in _INLINED:
                frame = _outer(frame)
                code = frame.f_code
            found.append(
                CallSite(
                    filename=code.co_filename,
                    lineno=frame.f_lineno,
                    function=code.co_name,
                )
            )
        frame = frame.f_back
    return tuple(found)


def _site(frame: FrameType, sites: Sites) -> CallSite:
    code = frame.f_code
    key = (code.co_filename, frame.f_lineno, code.co_name)
    site = sites.get(key)
    if site is None:
        if len(sites) >= _MAX_SITES:
            sites.clear()
        outer = _outer(frame) if key[2] in _INLINED else frame
        site = sites[key] = CallSite(
            filename=key[0],
            lineno=key[1],
            function=outer.f_code.co_name,
            callers=_callers(outer.f_back, _stack_depth),
        )
    return site


def _first_external(
    frame: FrameType | None, sites: Sites, skipped: list[FrameType] | None = None
) -> CallSite | None:
    """The first application frame from ``frame`` outwards, as a call site.

    A method of a Session subclass is passed over (see _is_session_method);
    the first one is appended to ``skipped``, the call site to use when no
    other application frame is found.
    """
    internal = _internal
    while frame is not None:
        filename = frame.f_code.co_filename
        try:
            skip = internal[filename]
        except KeyError:
            skip = internal[filename] = _classify(filename)
        if not skip:
            if not _is_session_method(frame):
                return _site(frame, sites)
            if skipped is not None and not skipped:
                skipped.append(frame)
        frame = frame.f_back
    return None


# Session._flush holds the UOWTransaction of one flush in its local
# `flush_context`. It sits behind a generated "warned" wrapper, so the frame is
# found by function name and file. If a SQLAlchemy release renames either, no
# statement is tagged and the write-loop rule counts every statement, as before.
_SESSION_FILE = SESSION_FILE
_flush_numbers = itertools.count(1)
_MAX_FLUSH_DEPTH = 60


def find_flush() -> int | None:
    """The number of the ORM flush this statement runs in, or None.

    The statements of one flush share a number: a unit of work cannot make them
    fewer, so they are not a loop of the application's own writes.
    """
    if _SESSION_FILE is None:
        return None
    frame: FrameType | None = sys._getframe(1)
    for _ in range(_MAX_FLUSH_DEPTH):
        if frame is None:
            return None
        code = frame.f_code
        if code.co_name == FLUSH_FUNCTION and code.co_filename == _SESSION_FILE:
            context = frame.f_locals.get(FLUSH_CONTEXT)
            if context is None:
                return None
            number = getattr(context, "_queryguard_flush", None)
            if number is None:
                number = next(_flush_numbers)
                try:
                    context._queryguard_flush = number
                except AttributeError:
                    return None
            return int(number)
        frame = frame.f_back
    return None


def find_callsite(sites: Sites | None = None) -> CallSite | None:
    """First stack frame outside SQLAlchemy, pytest, stdlib, site-packages and us.

    Methods of a Session subclass are passed over too, unless no other frame
    is found: they are part of the session, not the line that ran the query.

    When the stack ends inside a greenlet (async sessions), the search goes on
    in the parent greenlets, where the awaiting user code lives. ``sites`` is
    the cache of call sites to use instead of the process-wide one.
    """
    if sites is None:
        sites = _sites
    skipped: list[FrameType] = []
    site = _first_external(sys._getframe(1), sites, skipped)
    if site is not None:
        return site
    for frame in _parent_greenlet_frames():
        site = _first_external(frame, sites, skipped)
        if site is not None:
            return site
    return _site(skipped[0], sites) if skipped else None


def trim_to_user_code(exc: BaseException) -> None:
    """End the traceback of ``exc`` at the last frame of application code.

    For the exception that queryguard_strict raises inside SQLAlchemy's lazy
    loader: the frames below the attribute access (SQLAlchemy, the plugin)
    say nothing about the N+1, so the report ends at the line that read the
    relationship. Nothing changes when no such frame is found.
    """
    last_user = None
    tb = exc.__traceback__
    while tb is not None:
        if not _is_internal(tb.tb_frame.f_code.co_filename):
            last_user = tb
        tb = tb.tb_next
    if last_user is not None and last_user.tb_next is not None:
        last_user.tb_next = None
