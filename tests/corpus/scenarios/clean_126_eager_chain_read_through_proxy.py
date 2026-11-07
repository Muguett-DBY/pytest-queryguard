"""CLEAN: an eager chain that only an ``association_proxy`` and a property read.

Shape from Bullet #323 (has_one :through, a false positive there). A city's
continent is reached through its country. The page loads
``selectinload(City.country).selectinload(Country.continent)`` and reads
``city.continent_name`` (an ``association_proxy`` through ``country``) and
``city.continent`` (a property). User code never names ``City.country``, but
the proxy and the property read it: both links of the chain are used.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.ext.associationproxy import AssociationProxy, association_proxy
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    selectinload,
)

from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()


class Base(DeclarativeBase):
    pass


class Continent(Base):
    __tablename__ = "continent"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(20))


class Country(Base):
    __tablename__ = "country"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    continent_id: Mapped[int] = mapped_column(ForeignKey("continent.id"))
    continent: Mapped[Continent] = relationship()
    continent_name: AssociationProxy[str] = association_proxy("continent", "name")


class City(Base):
    __tablename__ = "city"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    country_id: Mapped[int] = mapped_column(ForeignKey("country.id"))
    country: Mapped[Country] = relationship()
    continent_name: AssociationProxy[str] = association_proxy(
        "country", "continent_name"
    )

    @property
    def continent(self) -> Continent:
        return self.country.continent


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Continent(id=i, name=f"continent{i}") for i in range(1, 4))
        session.flush()
        session.add_all(
            Country(id=i, name=f"country{i}", continent_id=1 + i % 3)
            for i in range(1, 7)
        )
        session.flush()
        session.add_all(
            City(id=i, name=f"city{i}", country_id=1 + i % 6) for i in range(1, 16)
        )
        session.commit()


def run(ctx: Ctx) -> None:
    stmt = (
        select(City)
        .order_by(City.id)
        .options(selectinload(City.country).selectinload(Country.continent))
    )
    with Session(ctx.engine()) as session:
        rows = [
            (c.name, c.continent_name, c.continent.id) for c in session.scalars(stmt)
        ]
    assert len(rows) == 15
