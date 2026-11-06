from __future__ import annotations

import inspect

import pytest

from pytest_queryguard.callsite import find_callsite


def test_callsite_points_to_test_file() -> None:
    site = find_callsite()
    expected = inspect.currentframe().f_lineno - 1  # type: ignore[union-attr]
    assert site is not None
    assert site.filename.endswith("test_callsite.py")
    assert site.lineno == expected
    assert site.function == "test_callsite_points_to_test_file"


def test_callsite_skips_stdlib_frames() -> None:
    import functools

    holder: list[object] = []
    functools.partial(lambda: holder.append(find_callsite()))()
    site = holder[0]
    assert site is not None
    assert site.filename.endswith("test_callsite.py")  # type: ignore[attr-defined]


def test_stdlib_files_are_internal() -> None:
    import json

    from pytest_queryguard.callsite import _is_internal

    assert _is_internal(json.__file__)


def test_callsite_found_through_parent_greenlet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import contextlib

    from pytest_queryguard import callsite

    greenlet = pytest.importorskip("greenlet")
    found: list[object] = []
    original = callsite._first_external

    def spy(frame: object, sites: object) -> object:
        site = original(frame, sites)  # type: ignore[arg-type]
        found.append(site)
        return site

    monkeypatch.setattr(callsite, "_first_external", spy)
    # ExitStack calls find_callsite from a stdlib frame, so the child
    # greenlet's own stack has no user frame and the parent must be searched.
    stack = contextlib.ExitStack()
    stack.callback(find_callsite)
    greenlet.greenlet(stack.close).switch()
    assert found[0] is None
    site = found[-1]
    assert site is not None
    assert site.function == "test_callsite_found_through_parent_greenlet"  # type: ignore[attr-defined]


def test_callsite_greenlet_lookup_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    broken = types.ModuleType("greenlet")  # no getcurrent(): AttributeError
    monkeypatch.setitem(sys.modules, "greenlet", broken)
    from pytest_queryguard import callsite

    assert list(callsite._parent_greenlet_frames()) == []


def test_internal_answer_is_cached_per_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pytest_queryguard import callsite

    calls: list[str] = []
    real = callsite._classify

    def spy(filename: str) -> bool:
        calls.append(filename)
        return real(filename)

    monkeypatch.setattr(callsite, "_classify", spy)
    monkeypatch.setattr(callsite, "_internal", {})
    name = "/some/project/app/models.py"
    assert callsite._is_internal(name) is False
    assert callsite._is_internal(name) is False
    assert callsite._is_internal("<string>") is True
    assert callsite._is_internal("<string>") is True
    assert calls == [name, "<string>"]


def test_same_location_shares_one_callsite() -> None:
    sites = [find_callsite() for _ in range(3)]  # one line, three calls
    assert sites[0] is not None
    assert sites[0] == sites[1] == sites[2]
    assert sites[0] is sites[1] is sites[2]


def test_different_lines_give_different_callsites() -> None:
    first = find_callsite()
    second = find_callsite()
    assert first is not None and second is not None
    assert first != second
    assert second.lineno == first.lineno + 1


def test_callsite_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from pytest_queryguard import callsite

    monkeypatch.setattr(callsite, "_sites", {})
    monkeypatch.setattr(callsite, "_MAX_SITES", 2)
    for _ in range(5):
        find_callsite()
        find_callsite()
        find_callsite()
    assert len(callsite._sites) <= 2
    site = find_callsite()
    assert site is not None and site.function == "test_callsite_cache_is_bounded"
