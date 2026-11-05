"""The session-wide cache of what the index check reads from the catalogs.

The first key column of each btree index and the varchar columns of each
table are read once per engine for the whole session (see indexes.py). DDL
can change both, so the whole cache is dropped:

- when a statement that may change the catalog runs, on any engine and in
  any test or fixture (anything not known to be safe counts);
- when a transaction that ran such a statement ends or rolls back to a
  savepoint: its commit changes what other connections see, its rollback
  what its own connection sees;
- when a connection is invalidated, or lost, while it has such a
  transaction open.

See docs/design-0.2.md, section 4.6.
"""

from __future__ import annotations

import re
import weakref
from typing import Any

from . import collector
from .events import Catalog
from .sqltext import QUOTED, skip_prefix

# Statements that cannot drop, rename or alter an index or a column. CREATE
# is not among them: like any statement not known to be safe, it clears the
# cache.
_SAFE_WORDS = """
    SELECT WITH VALUES TABLE INSERT UPDATE DELETE MERGE SHOW SET RESET
    BEGIN START SAVEPOINT RELEASE COMMIT END ROLLBACK ABORT ANALYZE ANALYSE
    VACUUM LOCK COPY DECLARE FETCH MOVE CLOSE EXECUTE DEALLOCATE LISTEN
    UNLISTEN NOTIFY CHECKPOINT
"""
_SAFE = frozenset(_SAFE_WORDS.split())
_TRANSACTION_END = frozenset({"COMMIT", "END", "ROLLBACK", "ABORT"})
_WORD = re.compile(r"[A-Za-z]+")
_SEMICOLON = re.compile(rf"{QUOTED}|(?P<semi>;)", re.VERBOSE | re.DOTALL)

_cache: weakref.WeakKeyDictionary[Any, Catalog] = weakref.WeakKeyDictionary()
# Connections with DDL in a transaction that has not ended yet, by id; the
# weak reference clears the cache when such a connection is collected.
_pending: dict[int, weakref.ref[Any]] = {}


def _words(sql: str) -> tuple[str, str] | None:
    """The first two keywords, upper case, or None when there is no first one."""
    start = skip_prefix(sql)
    if start is None:
        return None
    first = _WORD.match(sql, start)
    if first is None:
        return None
    second = _WORD.match(sql, skip_prefix(sql, first.end()) or first.end())
    return first[0].upper(), second[0].upper() if second else ""


def _more_statements(sql: str) -> bool:
    """True when a statement follows a semicolon outside quotes and comments."""
    if ";" not in sql:
        return False
    for match in _SEMICOLON.finditer(sql):
        if match["semi"] is not None:
            i = skip_prefix(sql, match.end())
            while i is not None and i < len(sql) and sql[i] == ";":
                i = skip_prefix(sql, i + 1)
            return i is not None and i < len(sql)
    return False


def _changes_catalog(sql: str, words: tuple[str, str] | None) -> bool:
    return words is None or words[0] not in _SAFE or _more_statements(sql)


def changes_catalog(sql: str) -> bool:
    """False only for statements known to leave indexes and columns alone."""
    return _changes_catalog(sql, _words(sql))


def for_engine(engine: Any) -> Catalog:
    """The catalog entries cached for this engine in this session."""
    try:
        found = _cache.get(engine)
        if found is None:
            found = _cache[engine] = Catalog()
    except TypeError:  # not weakly referenceable: nothing is kept
        return Catalog()
    return found


def cached_engines() -> int:
    return len(_cache)


def clear() -> None:
    """Drop every cached entry, for every engine.

    The statements already explained in a test are forgotten too: the same
    statement is explained again against the new state of the catalog.
    """
    for entry in list(_cache.values()):
        # Rebound, not cleared: a lookup that is reading the old dicts, in
        # this thread or another, must not see them change size.
        entry.leading = {}
        entry.varchar = {}
        entry.partial = {}
        entry.roots = {}
    for target in collector.active():
        target.explained.clear()
        target.index_runs.clear()


def reset() -> None:
    """Start a session with an empty cache.

    Connections with DDL pending stay tracked: their transactions may
    outlive a nested session.
    """
    _cache.clear()


def _lost(ref: weakref.ref[Any]) -> None:
    for key, value in list(_pending.items()):
        if value is ref:
            del _pending[key]
            clear()


def _is_pending(conn: Any) -> bool:
    ref = _pending.get(id(conn))
    return ref is not None and ref() is conn


def _resolve(conn: Any) -> None:
    """The transaction on ``conn`` ended: drop entries that relied on it."""
    if _is_pending(conn):
        del _pending[id(conn)]
        clear()


def observe(conn: Any, statement: str) -> None:
    """Look at a statement that ran on ``conn`` (an SQLAlchemy Connection)."""
    words = _words(statement)
    if _changes_catalog(statement, words):
        clear()
        if not _is_pending(conn):
            _pending[id(conn)] = weakref.ref(conn, _lost)
        return
    assert words is not None  # _changes_catalog() is True without words
    if words[0] not in _TRANSACTION_END:
        return
    if words[1] == "PREPARED" or _is_pending(conn):
        # A prepared transaction may have run DDL anywhere. The connection
        # stays pending until SQLAlchemy ends its own transaction.
        clear()


def on_commit(conn: Any) -> None:
    _resolve(conn)


def on_rollback(conn: Any) -> None:
    _resolve(conn)


def on_commit_twophase(conn: Any, xid: object, is_prepared: bool) -> None:
    # A prepared transaction may have run DDL on any connection.
    if is_prepared:
        clear()
    _resolve(conn)


def on_rollback_twophase(conn: Any, xid: object, is_prepared: bool) -> None:
    if is_prepared:
        clear()
    _resolve(conn)


def on_rollback_savepoint(conn: Any, name: object, context: object) -> None:
    # The DDL may have run before the savepoint, so the connection stays pending.
    if _is_pending(conn):
        clear()


def on_invalidate(dbapi_connection: object, record: object, exception: object) -> None:
    # The pool does not say which Connection this was; any DDL may be gone.
    if _pending:
        clear()
