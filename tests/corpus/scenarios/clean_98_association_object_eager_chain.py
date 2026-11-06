"""CLEAN: the playlist page with ``selectinload(...).joinedload(...)``.

``selectinload(Playlist.entries).joinedload(PlaylistEntry.track)``: the
entries in one SELECT with their tracks joined, a many-to-one, so two
queries in all.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..kit import CLEAN, Ctx
from .bug_91_association_object_loop import Playlist, PlaylistEntry
from .bug_91_association_object_loop import setup as playlist_setup

VERDICT = CLEAN
KINDS = frozenset()
setup = playlist_setup


def run(ctx: Ctx) -> None:
    stmt = (
        select(Playlist)
        .order_by(Playlist.id)
        .options(selectinload(Playlist.entries).joinedload(PlaylistEntry.track))
    )
    with Session(ctx.engine()) as session:
        page = [
            {"name": p.name, "tracks": [e.track.title for e in p.entries]}
            for p in session.scalars(stmt)
        ]
    assert len(page) == 6
