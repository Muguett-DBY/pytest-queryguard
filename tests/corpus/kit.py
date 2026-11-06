"""What every scenario module uses: the context, the verdict constants, helpers.

A scenario module defines:

* ``VERDICT``: ``BUG`` or ``CLEAN``, the ground truth, from reading the code;
* ``KINDS``: the finding kinds a perfect tool would report (empty when CLEAN);
* ``KNOWN``: optional ``{config: reason}``. In those configurations the plugin
  is known to disagree with the verdict, a documented limitation or an open
  bug; the corpus test is then an ``xfail(strict=True)``;
* ``PATCH``: optional, ``True`` when ``--queryguard-write-patch`` is expected
  to write a patch for its relationship-load that removes it
  (``test_fix_patches.py``); without it, no patch is expected;
* ``setup(ctx)``: create the schema and seed rows; not measured;
* ``run(ctx)``: the code under test; measured. It may be ``async``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

BUG = "BUG"
CLEAN = "CLEAN"

REL = "relationship-load"
REPEAT = "repeated-statement"
COLUMN = "column-load"
WRITE = "write-loop"
UNUSED = "unused-eager-load"
CARTESIAN = "cartesian-load"
COLUMNS = "unused-columns"

# Set to a SQLAlchemy URL of a PostgreSQL server (pg8000 driver) to run the
# corpus there instead of on SQLite files.
PG_URL_ENV = "QUERYGUARD_CORPUS_PG_URL"


_SYNC_SEQUENCES = """
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT d.objid::regclass AS seq, d.refobjid::regclass AS tab, a.attname
        FROM pg_depend d
        JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
        JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
        JOIN pg_namespace n ON n.oid = s.relnamespace AND n.nspname = 'public'
        WHERE d.classid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
    LOOP
        EXECUTE format(
            'SELECT setval(%L, coalesce((SELECT max(%I) FROM %s), 0) + 1, false)',
            r.seq, r.attname, r.tab);
    END LOOP;
END $$
"""


def pg_url_from_env() -> str | None:
    return os.environ.get(PG_URL_ENV)


def _fast_sqlite(engine: Engine) -> None:
    """No fsync: the corpus databases are throw-away. Not seen by the plugin,
    which listens to statements SQLAlchemy executes, not to the DBAPI cursor here."""

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.execute("PRAGMA journal_mode=MEMORY")
        cursor.close()


class Ctx:
    """One scenario's database: a SQLite file, or a clean schema on PostgreSQL."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.pg_url = pg_url_from_env()
        self._engine: Engine | None = None
        self._async_engine: AsyncEngine | None = None

    @property
    def url(self) -> str:
        return self.pg_url or f"sqlite:///{self.directory / 'corpus.db'}"

    @property
    def async_url(self) -> str:
        if self.pg_url:
            return self.pg_url.replace("postgresql+pg8000", "postgresql+asyncpg")
        return f"sqlite+aiosqlite:///{self.directory / 'corpus.db'}"

    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(self.url)
            if not self.pg_url:
                _fast_sqlite(self._engine)
        return self._engine

    def async_engine(self) -> AsyncEngine:
        if self._async_engine is None:
            self._async_engine = create_async_engine(self.async_url)
            if not self.pg_url:
                _fast_sqlite(self._async_engine.sync_engine)
        return self._async_engine

    def sessions(self) -> sessionmaker[Session]:
        return sessionmaker(self.engine())

    def reset(self) -> None:
        """An empty database: PostgreSQL keeps tables between scenarios."""
        if self.pg_url:
            with self.engine().begin() as conn:
                conn.execute(text("DROP SCHEMA public CASCADE"))
                conn.execute(text("CREATE SCHEMA public"))

    def sync_sequences(self) -> None:
        """Move every serial and identity sequence past the seeded rows.

        The seeds insert explicit primary keys. SQLite then numbers new rows
        after the largest one, but a PostgreSQL sequence still starts at 1, so
        the first INSERT of a scenario would fail with a duplicate key.
        """
        if self.pg_url:
            with self.engine().begin() as conn:
                conn.execute(text(_SYNC_SEQUENCES))

    def warm_up(self) -> None:
        """Connect once, so the first connection is not part of the measurement."""
        with self.engine().connect() as conn:
            conn.execute(text("SELECT 1"))

    async def dispose_async(self) -> None:
        if self._async_engine is not None:
            await self._async_engine.dispose()
            self._async_engine = None

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None


def insert_for(engine: Engine) -> Any:
    """The dialect's ``insert`` with ``on_conflict_*``: SQLite, or PostgreSQL."""
    return postgresql.insert if engine.dialect.name == "postgresql" else sqlite.insert
