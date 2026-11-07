"""One wrapper of ``InstrumentedAttribute.__get__`` for the checks that need reads.

A plain read of a loaded attribute is a dictionary lookup that fires no
SQLAlchemy event, so the experimental checks that need to know what a test
read (unused eager loads, unused columns) wrap the descriptor while they are
on. They share this one wrapper, so turning them on and off in any order
always gives SQLAlchemy its own ``__get__`` back once the last one is off.

The wrapper runs on every attribute read in the process, so it does as little
as it can, with no function call: it looks at two module globals, which the
checks set only while a test body is measured.

* ``reads``: a set that gets ``(class, attribute name)`` of each read
  (the unused eager-load check), or None;
* ``instances``: ``id(instance)`` -> ``(InstanceState, set)``; the set gets
  the attribute name of each read of that instance (the unused-columns
  check), or None. The state is compared with the instance's own, so an
  ``id()`` that a new object reuses after the instance is gone never matches.

An error in either step calls ``reads_failed`` or ``instances_failed`` (set by
the check that owns it, to turn itself off) and the read goes on.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ._compat import ATTRIBUTE_DESCRIPTOR, attribute_get

reads: set[tuple[type, str]] | None = None
instances: dict[int, tuple[Any, set[str]]] | None = None
reads_failed: Callable[[Exception], None] | None = None
instances_failed: Callable[[Exception], None] | None = None
_users = 0
_original: Any = None


def _tracked_get(self: Any, instance: Any, owner: Any) -> Any:
    if instance is not None:
        if reads is not None:
            try:
                reads.add((type(instance), self.key))
            except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
                _failed(reads_failed, exc)
        if instances is not None:
            try:
                entry = instances.get(id(instance))
                # _compat.INSTANCE_STATE, inline: this runs on every read
                if entry is not None and entry[0] is instance._sa_instance_state:
                    entry[1].add(self.key)
            except Exception as exc:  # noqa: BLE001  # a plugin bug must never break the user suite
                _failed(instances_failed, exc)
    return _original(self, instance, owner)


def _failed(callback: Callable[[Exception], None] | None, exc: Exception) -> None:
    try:
        if callback is not None:
            callback(exc)
    except Exception:  # noqa: BLE001, S110  # never let the report of an error raise
        pass


def install() -> None:
    """Wrap the descriptor; calls nest, see ``uninstall()``."""
    global _users, _original
    _users += 1
    if _users == 1:
        _original = attribute_get()
        ATTRIBUTE_DESCRIPTOR.__get__ = _tracked_get  # type: ignore[method-assign]


def uninstall() -> None:
    """Undo one ``install()``; the last one restores SQLAlchemy's descriptor."""
    global _users
    if _users == 0:
        return
    _users -= 1
    if _users == 0:
        # _original is kept: another thread may have entered _tracked_get
        # just before this line and still has to call it.
        ATTRIBUTE_DESCRIPTOR.__get__ = _original  # type: ignore[method-assign]
