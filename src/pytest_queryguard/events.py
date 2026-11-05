from __future__ import annotations

from dataclasses import dataclass, field

from .partial import PartialIndex


@dataclass(frozen=True, slots=True, kw_only=True)
class CallSite:
    """A source location. Its fields are public API; its constructor is not."""

    filename: str
    lineno: int
    function: str


@dataclass(slots=True, frozen=True)
class CartesianLoadEvent:
    """One SELECT that joins two or more collections side by side.

    ``collections`` are the joined collection paths, each a tuple of
    ``(Class.relationship, loader)`` steps from the query's entity; ``root``
    is the entity's class name.
    """

    collections: tuple[tuple[tuple[str, str], ...], ...]
    root: str
    callsite: CallSite | None


@dataclass(slots=True)
class StatementEvent:
    sql: str
    callsite: CallSite | None
    relationship: str | None
    # Seconds between the before and after cursor-execute events; None until
    # the statement has finished (or when it never did, because it raised).
    duration: float | None = None
    # True when the statement loaded columns of an instance that already
    # existed (a deferred column, or attributes expired by commit or expire()).
    column_load: bool = False
    # True when the statement is the lazy load of `relationship`; False for the
    # query of an eager loader (selectinload, subqueryload) that carries the
    # same relationship label.
    lazy_load: bool = False
    # The bound parameters as the driver got them (not copied); None for
    # executemany, which can be large. Compared by the small-repeat rule.
    parameters: object = None
    # True when the driver ran the statement for a list of parameter sets.
    executemany: bool = False
    # Which ORM flush issued the statement (a number, the same for every
    # statement of one flush); None for a statement outside a flush.
    flush: int | None = None
    # The Session (or, without one, the connection checkout) the statement ran
    # in: a number, the same for every statement of that Session. None when
    # unknown. See listeners.scope_of_connection().
    scope: int | None = None


@dataclass(frozen=True, slots=True)
class ParentLoad:
    """Where the parent of a lazy load came from, for the fix hint."""

    # The query that loaded the parent; for a parent reached through other
    # relationships, the query at the start of that chain. None when unknown.
    callsite: CallSite | None
    # The class that query selected ("Order"), and the relationships from it to
    # the parent as (label, scalar): (("Order.customer", True),). root is None
    # when the way is unknown or cannot be written as a loader option chain.
    root: str | None = None
    path: tuple[tuple[str, bool], ...] = ()


@dataclass(frozen=True, slots=True)
class RelationshipLoadEvent:
    relationship: str
    parent_key: object
    callsite: CallSite | None
    scalar: bool = False  # many-to-one or one-to-one, not a collection
    scope: int | None = None  # the Session that lazy-loaded it
    parent: ParentLoad | None = None  # None when nothing is known about it


# What made the instance load columns again.
CAUSE_DEFERRED = "deferred"  # a deferred column, or one a query option left out
CAUSE_EXPIRED = "expired"  # expired by commit(), expire() or expire_all()
CAUSE_REFRESH = "refresh"  # session.refresh() without attribute names


@dataclass(slots=True)
class ColumnLoadEvent:
    """An instance that went back to the database for some of its columns."""

    owner: str  # the mapped class, "Child"
    attributes: tuple[str, ...]  # the attributes loaded, sorted
    cause: str  # one of the CAUSE_* values
    instance_key: object
    callsite: CallSite | None
    sql: str | None = None  # set when the statement runs
    scope: int | None = None  # the Session that loaded the columns


@dataclass(frozen=True, slots=True)
class IndexKey:
    """One key column of a suggested index."""

    column: str  # as the plan prints it: quoted when it needs quotes
    suffix: str = ""  # " DESC", " DESC NULLS LAST", ... where the order matters


@dataclass(frozen=True, slots=True)
class Verification:
    """What hypopg answered for a suggested index, estimated on the plan's statement.

    ``used`` is None when nothing was verified (``error`` says why, if it
    failed). The costs are the planner's estimates for the whole statement
    with sequential scans allowed, without and with the hypothetical index;
    ``used`` is asked with them off, as the check does.
    """

    used: bool | None = None
    cost_before: float | None = None
    cost_after: float | None = None
    scan: str | None = None  # the plan node type that reads the hypothetical index
    size: int | None = None  # estimated size of the index in bytes
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Advice:
    """A suggested index for one finding, or why none is suggested."""

    keys: tuple[IndexKey, ...] = ()
    where: str | None = None  # the predicate of a partial index
    create_sql: str | None = None
    alembic: str | None = None
    skipped: str | None = None  # why no index is suggested
    verification: Verification | None = None


@dataclass(frozen=True, slots=True)
class ScanIssue:
    """A plan node that reads a whole table although the statement narrows it."""

    schema: str | None
    table: str
    node: str  # "Seq Scan" or "Index Scan using orders_pkey"
    kind: str  # "filter" or "index condition"
    condition: str
    column: str | None  # the column for a CREATE INDEX hint, when obvious
    show_schema: bool = True  # False when the table is in the current schema
    expression: bool = False  # the column sits in a function or a non-text cast
    # Set when the plan walks an existing index or primary key that the filter
    # cannot use ("index ix_orders_created_at", "primary key"); the table is
    # then not read in full, the index is. SQLite and PostgreSQL.
    index: str | None = field(default=None, compare=False)
    # What the index advisor works from; not part of the issue's identity.
    alias: str = field(default="", compare=False)
    conditions: tuple[str, ...] = field(default=(), compare=False)
    sort_keys: tuple[str, ...] = field(default=(), compare=False)
    limit: bool = field(default=False, compare=False)
    advice: Advice | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class ScanEvent:
    issue: ScanIssue
    sql: str
    callsite: CallSite | None


@dataclass(slots=True)
class Catalog:
    """What the index check read from the system catalogs, for one engine."""

    # (schema, index) -> first key column of a btree index, or None
    leading: dict[tuple[str, str], str | None] = field(default_factory=dict)
    # (schema, table) -> names of its varchar columns
    varchar: dict[tuple[str, str], frozenset[str]] = field(default_factory=dict)
    # (schema, table) -> its valid partial indexes (see partial.py)
    partial: dict[tuple[str, str], tuple[PartialIndex, ...]] = field(
        default_factory=dict
    )
    # (schema, table) of a partition -> (schema, table) of its partitioned
    # root; None for a table that is not a partition
    roots: dict[tuple[str, str], tuple[str, str] | None] = field(default_factory=dict)
    # (installed, available) versions of the hypopg extension; None until read
    hypopg: tuple[str | None, str | None] | None = None
