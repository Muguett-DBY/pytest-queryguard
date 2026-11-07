"""CLEAN: three new objects, each refreshed and its collection read on a line of its own.

From SQLModel 0.0.24, ``docs_src/tutorial/many_to_many/tutorial001.py``
(``create_heroes``), seen by ``.github/workflows/realworld.yml``: the tutorial
creates three heroes with their teams, commits, refreshes each hero and prints
each hero's teams, one line per hero. Three lazy loads of ``Hero.teams``, but
no loop: each is read on a line of its own, as two single-object lookups at
two lines are (``clean_93_two_single_object_lookups``).

The objects were made by the code, so no query line loaded them (a refresh
does not say where an object came from). The plugin counted such parents as
one group whatever line read them, and reported the three reads as an N+1.
"""

from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
_REFRESH = (
    "the three refreshes after one commit are the opt-in column-load threshold of "
    "2's pattern: of the rows of one flush only the first refresh is let through"
)
KNOWN = {"column-load-2": _REFRESH, "all-opt-in": _REFRESH}


class Base(DeclarativeBase):
    pass


hero_team = Table(
    "hero_team",
    Base.metadata,
    Column("hero_id", ForeignKey("hero.id"), primary_key=True),
    Column("team_id", ForeignKey("team.id"), primary_key=True),
)


class Team(Base):
    __tablename__ = "team"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class Hero(Base):
    __tablename__ = "hero"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    teams: Mapped[list[Team]] = relationship(secondary=hero_team)


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        preventers = Team(name="Preventers")
        z_force = Team(name="Z-Force")
        deadpond = Hero(name="Deadpond", teams=[z_force, preventers])
        rusty_man = Hero(name="Rusty-Man", teams=[preventers])
        spider_boy = Hero(name="Spider-Boy", teams=[preventers])
        session.add_all([deadpond, rusty_man, spider_boy])
        session.commit()

        session.refresh(deadpond)
        session.refresh(rusty_man)
        session.refresh(spider_boy)

        shown = [
            ("Deadpond teams:", [team.name for team in deadpond.teams]),
        ]
        shown.append(("Rusty-Man teams:", [team.name for team in rusty_man.teams]))
        shown.append(("Spider-Boy teams:", [team.name for team in spider_boy.teams]))
    assert [len(teams) for _, teams in shown] == [2, 1, 1]
