"""Column loads in the in-app mode follow the pytest plugin's rules.

A refresh after the flush that wrote the object is not counted (see
tests/test_refresh_after_write.py), ``session.refresh(obj, names)`` is a
refresh and not an expired load, and the column load of a joined-inheritance
subclass is a column load, also on SQLAlchemy before 2.0.30.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from sqlalchemy import ForeignKey, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.pool import StaticPool

from pytest_queryguard import app as queryguard
from pytest_queryguard import record_queries
from pytest_queryguard.app import RequestReport, _core, _listeners

ROWS = 8


class Base(DeclarativeBase):
    pass


class Team(Base):
    __tablename__ = "team"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    version: Mapped[int] = mapped_column(server_default="1")
    members: Mapped[list[Member]] = relationship(lazy="selectin")


class Member(Base):
    __tablename__ = "member"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"))


class Employee(Base):
    __tablename__ = "employee"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]
    name: Mapped[str]
    __mapper_args__: ClassVar[dict[str, Any]] = {
        "polymorphic_on": "kind",
        "polymorphic_identity": "employee",
    }


class Manager(Employee):
    __tablename__ = "manager"

    id: Mapped[int] = mapped_column(ForeignKey("employee.id"), primary_key=True)
    budget: Mapped[int]
    __mapper_args__: ClassVar[dict[str, Any]] = {"polymorphic_identity": "manager"}


@pytest.fixture
def db() -> Iterator[Engine]:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    queryguard.watch(engine)
    yield engine
    engine.dispose()


def _create(session: Session, name: str, *, commit: bool = False) -> Team:
    team = Team(name=name)
    session.add(team)
    if commit:
        session.commit()
    else:
        session.flush()
    session.refresh(team)
    return team


def _seed(db: Engine) -> None:
    with Session(db) as session:
        session.add_all(Team(name=f"t{i}") for i in range(ROWS))
        session.commit()


def _column_loads(report: RequestReport) -> list[Any]:
    return [f for f in report.findings if f.kind == "column-load"]


# --------------------------------------------------------- refresh after write


@pytest.mark.parametrize("commit", [False, True], ids=["flush", "commit"])
def test_creates_each_refreshed_are_clean(db: Engine, commit: bool) -> None:
    with (
        Session(db) as session,
        queryguard.track(column_load_threshold=2, report=[]) as report,
    ):
        for i in range(ROWS):
            _create(session, f"t{i}", commit=commit)
    assert report.findings == ()


def test_the_refreshes_are_not_a_repeated_statement_either(db: Engine) -> None:
    with (
        Session(db) as session,
        queryguard.track(repeat_threshold=2, report=[]) as report,
    ):
        for i in range(ROWS):
            _create(session, f"t{i}")
    assert report.findings == ()


def test_creates_refreshed_by_attribute_name_are_clean(db: Engine) -> None:
    with (
        Session(db) as session,
        queryguard.track(column_load_threshold=2, report=[]) as report,
    ):
        for i in range(ROWS):
            team = Team(name=f"t{i}")
            session.add(team)
            session.commit()
            session.refresh(team, ["members"])
    assert report.findings == ()


def test_updates_each_refreshed_are_clean(db: Engine) -> None:
    with Session(db) as session:
        teams = [_create(session, f"t{i}") for i in range(ROWS)]
        with queryguard.track(column_load_threshold=2, report=[]) as report:
            for team in teams:
                team.name += " renamed"
                session.flush()
                session.refresh(team)
    assert report.findings == ()


def test_async_creates_each_refreshed_are_clean() -> None:
    async def main() -> RequestReport:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            queryguard.watch(engine)
            with queryguard.track(column_load_threshold=2, report=[]) as report:
                async with AsyncSession(engine) as session:
                    for i in range(ROWS):
                        team = Team(name=f"t{i}")
                        session.add(team)
                        await session.commit()
                        await session.refresh(team)
            return report
        finally:
            await engine.dispose()

    assert asyncio.run(main()).findings == ()


def test_a_refresh_loop_of_objects_that_were_only_read_is_reported(
    db: Engine,
) -> None:
    _seed(db)
    with (
        Session(db) as session,
        queryguard.track(report=[]) as report,
    ):
        for team in session.scalars(select(Team)).all():
            session.refresh(team)
    (finding,) = report.findings
    assert finding.label == "Team (refresh)"
    assert finding.count == ROWS


def test_refreshing_each_row_of_a_bulk_insert_is_reported(db: Engine) -> None:
    with Session(db) as session, queryguard.track(report=[]) as report:
        teams = [Team(name=f"t{i}") for i in range(ROWS)]
        session.add_all(teams)
        session.flush()
        for team in teams:
            session.refresh(team)
    # one refresh per class and flush is let through, the others count
    (finding,) = report.findings
    assert finding.label == "Team (refresh)"
    assert finding.count == ROWS - 1


def test_a_second_refresh_of_the_same_write_counts(db: Engine) -> None:
    with (
        Session(db) as session,
        queryguard.track(column_load_threshold=2, report=[]) as report,
    ):
        for i in range(2):
            team = _create(session, f"t{i}")
            session.refresh(team)
    (finding,) = report.findings
    assert finding.label == "Team (refresh)"
    assert finding.count == 2


def test_a_refresh_after_a_later_flush_counts(db: Engine) -> None:
    # only the latest flush of the Session is a write the refresh reads back
    with (
        Session(db) as session,
        queryguard.track(column_load_threshold=2, report=[]) as report,
    ):
        a, b = Team(name="a"), Team(name="b")
        session.add(a)
        session.flush()
        session.add(b)
        session.flush()
        session.refresh(b)  # let through: the latest flush wrote it
        session.refresh(a)
        c = Team(name="c")
        session.add(c)
        session.flush()
        session.refresh(b)
    (finding,) = report.findings
    assert finding.count == 2


def test_a_refresh_loop_after_flushes_that_wrote_nothing_is_reported(
    db: Engine,
) -> None:
    # An attribute set to its own value makes the object dirty, but the flush
    # emits no UPDATE: the refresh reads back no write.
    _seed(db)
    with Session(db) as session, queryguard.track(report=[]) as report:
        for team in session.scalars(select(Team)).all():
            team.name = team.name
            session.flush()
            session.refresh(team)
    assert not any(q.sql.startswith("UPDATE") for q in report.statements)
    (finding,) = report.findings
    assert finding.label == "Team (refresh)"
    assert finding.count == ROWS


def test_a_flush_of_another_unit_is_not_read_back_in_this_one(
    db: Engine,
) -> None:
    # The record of a flush belongs to the request that flushed: a Session
    # that outlives it does not carry the exemption into the next request.
    with Session(db) as session:
        with queryguard.track(report=[]):
            teams = [Team(name=f"t{i}") for i in range(ROWS)]
            session.add_all(teams)
            session.flush()
        with queryguard.track(report=[]) as report:
            for team in teams:
                session.refresh(team)
    (finding,) = report.findings
    assert finding.count == ROWS


def test_the_pytest_plugin_and_the_in_app_mode_each_let_the_refresh_through(
    db: Engine,
) -> None:
    # Both measure at once; neither takes the other's record of the flush.
    with (
        Session(db) as session,
        record_queries() as log,
        queryguard.track(column_load_threshold=2, report=[]) as report,
    ):
        for i in range(ROWS):
            _create(session, f"t{i}")
    assert report.findings == ()
    log.assert_no_nplusone(column_load_threshold=2)


# ----------------------------------------------------------- refresh by name


def test_a_named_session_refresh_in_a_loop_is_a_refresh(db: Engine) -> None:
    # refresh(obj, names) expires the names first; it is still a refresh
    _seed(db)
    with Session(db) as session, queryguard.track(report=[]) as report:
        for team in session.scalars(select(Team)).all():
            session.refresh(team, ["name"])
    (finding,) = report.findings
    assert finding.label == "Team (refresh)"
    assert finding.count == ROWS
    assert "reloaded with session.refresh()" in finding.message
    assert "expire_on_commit" not in finding.message


def test_an_expired_loop_is_still_an_expired_load(db: Engine) -> None:
    _seed(db)
    with Session(db) as session, queryguard.track(report=[]) as report:
        teams = session.scalars(select(Team)).all()
        session.commit()
        for team in teams:
            _ = team.name
    (finding,) = report.findings
    assert finding.label == "Team (expired)"
    assert "expire_on_commit=False" in finding.message


# -------------------------------------------------------- joined inheritance


def test_a_joined_inheritance_subclass_column_is_a_column_load(
    db: Engine,
) -> None:
    with Session(db) as session:
        session.add_all(Manager(name=f"m{i}", budget=i) for i in range(ROWS))
        session.commit()
    with Session(db) as session, queryguard.track(report=[]) as report:
        for employee in session.scalars(select(Employee)).all():
            assert isinstance(employee, Manager)
            _ = employee.budget
    assert [f.kind for f in report.findings] == ["column-load"]
    assert _column_loads(report)[0].count == ROWS


def test_the_eager_loader_a_refresh_runs_is_not_a_lazy_load(db: Engine) -> None:
    # Refreshing a Team runs its lazy="selectin" members loader for that one
    # object: the mapped eager strategy, so no relationship-load finding.
    _seed(db)
    with (
        Session(db) as session,
        queryguard.track(column_load_threshold=2, report=[]) as report,
    ):
        for team in session.scalars(select(Team)).all():
            session.refresh(team)
    assert [f.kind for f in report.findings] == ["column-load"]
    members = [q for q in report.statements if "FROM member" in q.sql]
    assert len(members) == 1 + ROWS  # the query's selectin load, then one each
    assert {q.relationship for q in members} == {"Team.members"}


def test_a_bug_in_the_after_flush_listener_switches_the_mode_off(
    db: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken:
        @property
        def new(self) -> Any:
            raise RuntimeError("boom")

    with queryguard.track(report=[]):
        _listeners._on_after_flush(Broken(), None)  # type: ignore[arg-type]
    assert isinstance(_core.internal_error(), RuntimeError)
    assert "the after_flush listener" in caplog.text
