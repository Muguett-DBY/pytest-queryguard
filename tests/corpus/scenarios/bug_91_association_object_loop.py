"""BUG relationship-load: a playlist page walks an association object lazily.

``Playlist.entries`` is an association object (``PlaylistEntry`` with a
``position``), and each entry points to its ``Track``. The page loads the
playlists, then for each one the entries, then for each entry the track:
two N+1s, ``Playlist.entries`` and ``PlaylistEntry.track``.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True


class Base(DeclarativeBase):
    pass


class Track(Base):
    __tablename__ = "track"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(80))


class PlaylistEntry(Base):
    __tablename__ = "playlist_entry"
    playlist_id: Mapped[int] = mapped_column(
        ForeignKey("playlist.id"), primary_key=True
    )
    track_id: Mapped[int] = mapped_column(ForeignKey("track.id"), primary_key=True)
    position: Mapped[int]
    track: Mapped[Track] = relationship()


class Playlist(Base):
    __tablename__ = "playlist"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    entries: Mapped[list[PlaylistEntry]] = relationship(order_by=PlaylistEntry.position)


def setup(ctx: Ctx) -> None:
    ctx.reset()
    Base.metadata.create_all(ctx.engine())
    with Session(ctx.engine()) as session:
        session.add_all(Track(id=i, title=f"track{i}") for i in range(1, 31))
        for p in range(1, 7):
            session.add(Playlist(id=p, name=f"list{p}"))
            session.add_all(
                PlaylistEntry(playlist_id=p, track_id=(p * 5 + k) % 30 + 1, position=k)
                for k in range(5)
            )
        session.commit()


def playlists_page(session: Session) -> list[dict[str, object]]:
    playlists = session.scalars(select(Playlist).order_by(Playlist.id)).all()
    return [
        {"name": p.name, "tracks": [e.track.title for e in p.entries]}
        for p in playlists
    ]


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        page = playlists_page(session)
    assert len(page) == 6
