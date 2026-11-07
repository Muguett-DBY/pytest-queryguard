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

    def spy(frame: object, sites: object, *args: object) -> object:
        site = original(frame, sites, *args)  # type: ignore[arg-type]
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


def test_a_comprehension_is_named_after_the_function_it_is_in(
    pytester: pytest.Pytester,
) -> None:
    # Before 3.12 a list comprehension runs in a frame of its own; the report
    # names the enclosing function, with no extra "called from" line, as on
    # 3.12 and later.
    pytester.makepyfile(
        test_comp="""
        from sqlalchemy import ForeignKey, create_engine, select
        from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship


        class Base(DeclarativeBase):
            pass


        class Customer(Base):
            __tablename__ = "customer"
            id: Mapped[int] = mapped_column(primary_key=True)
            name: Mapped[str]


        class Order(Base):
            __tablename__ = "orders"
            id: Mapped[int] = mapped_column(primary_key=True)
            customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
            customer: Mapped[Customer] = relationship()


        def names(session):
            return [o.customer.name for o in session.scalars(select(Order))]


        def test_names():
            engine = create_engine("sqlite://")
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                session.add_all(
                    [Order(id=n, customer=Customer(id=n, name="c")) for n in range(3)]
                )
                session.commit()
            with Session(engine) as session:
                names(session)
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    out = result.stdout.str()
    assert "<listcomp>" not in out
    result.stdout.fnmatch_lines(
        [
            "*first at test_comp.py:* in names",
            "*called from test_comp.py:* in test_names",
        ]
    )


class _AppSession:
    """Stands in for a Session subclass in the application's own code."""

    site: object = None

    def execute(self) -> object:
        self.site = find_callsite()
        return self.site

    def exec(self) -> object:
        return self.execute()


def _session_class() -> type:
    from sqlalchemy.orm import Session

    return type("AppSession", (_AppSession, Session), {})


def test_a_session_subclass_method_is_not_the_call_site() -> None:
    # SQLModel's Session (and many applications') overrides execute() in code
    # outside site-packages; the call site is the code that called the session,
    # so two queries at two lines are not taken for one query line.
    session = _session_class()()
    site = session.exec()
    expected = inspect.currentframe().f_lineno - 1  # type: ignore[union-attr]
    assert site is not None
    assert site.function == "test_a_session_subclass_method_is_not_the_call_site"
    assert site.lineno == expected
    # nor one of its callers
    assert all(caller.function not in ("exec", "execute") for caller in site.callers)


class _Repository:
    def execute(self) -> object:
        return find_callsite()


def test_a_method_of_another_class_is_still_the_call_site() -> None:
    site = _Repository().execute()
    assert site is not None
    assert site.function == "execute"  # type: ignore[attr-defined]


def test_a_session_method_is_the_call_site_when_nothing_else_is() -> None:
    # In a thread whose target is the session method, every frame above it is
    # the standard library's: the method is kept rather than no call site.
    import threading

    session = _session_class()()
    thread = threading.Thread(target=session.execute)
    thread.start()
    thread.join()
    assert session.site is not None
    assert session.site.function == "execute"  # type: ignore[attr-defined]


def test_a_comprehension_in_a_session_method_is_passed_over() -> None:
    class Many(_AppSession):
        def many(self) -> list[object]:
            return [find_callsite() for _ in range(2)]

    from sqlalchemy.orm import Session

    session = type("ManySession", (Many, Session), {})()
    sites = session.many()
    expected = inspect.currentframe().f_lineno - 1  # type: ignore[union-attr]
    assert sites[0] is not None
    assert sites[0].lineno == expected  # type: ignore[attr-defined]
