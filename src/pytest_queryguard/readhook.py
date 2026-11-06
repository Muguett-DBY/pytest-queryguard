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
* ``instances``: ``id(instance)`` -> a set that gets the attribute name of
  each read of that instance (the unused-columns check), or None.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import InstrumentedAttribute

reads: set[tuple[type, str]] | None = None
instances: dict[int, set[str]] | None = None
_users = 0
_original: Any = None


def _tracked_get(self: Any, instance: Any, owner: Any) -> Any:
    if instance is not None:
        if reads is not None:
            reads.add((type(instance), self.key))
        if instances is not None:
            names = instances.get(id(instance))
            if names is not None:
                names.add(self.key)
    return _original(self, instance, owner)


def install() -> None:
    """Wrap the descriptor; calls nest, see ``uninstall()``."""
    global _users, _original
    _users += 1
    if _users == 1:
        _original = InstrumentedAttribute.__get__
        InstrumentedAttribute.__get__ = _tracked_get  # type: ignore[method-assign]


def uninstall() -> None:
    """Undo one ``install()``; the last one restores SQLAlchemy's descriptor."""
    global _users, _original
    if _users == 0:
        return
    _users -= 1
    if _users == 0:
        InstrumentedAttribute.__get__ = _original  # type: ignore[method-assign]
        _original = None


def is_installed() -> bool:
    return _users > 0
