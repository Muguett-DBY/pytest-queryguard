"""Every private SQLAlchemy name pytest-queryguard reads, in one place.

SQLAlchemy's public events say that a statement runs, not what it is for: a
lazy load, a refresh, the loader of a ``selectinload()``. The listeners read
that from attributes and execution options SQLAlchemy keeps for itself, and
a few checks look into its frames, code objects and descriptors. They are
named here, with the versions they are known for, and tests/test_compat.py
exercises each against the installed SQLAlchemy: a release that renames or
drops one fails that test first, with the name in the message.

Supported: SQLAlchemy 2.0.13 to 2.1 (``pyproject.toml``); "2.0.13+" below
means unchanged over that whole range.

The per-statement code (the cursor listeners, the attribute read hook) reads
these names inline, as ``getattr(obj, NAME)`` with the constants below, and
not through an accessor function: a call per statement is measurable in
``scripts/bench_gate.py``. The accessors are for the code that runs rarely
(once per query, per flush or at import) and for the tests.

When a name is gone the readers never raise into the user's code (the
docstrings of the readers say what they do): most miss a label or a fix
hint's parent site; a few (``_only_load_props``) stop the measurement through
the internal-error path rather than mislabel. ``QueryContext`` is imported,
so a release that moves it fails at import, as SQLAlchemy's own imports do.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy.orm.session as _orm_session
from sqlalchemy.orm import InstrumentedAttribute as _InstrumentedAttribute
from sqlalchemy.orm import loading as _loading

# QueryContext is not exported by sqlalchemy.orm (2.0.13+): the context the
# ORM turns rows into instances with, which the Mapper ``load`` and
# ``refresh`` events receive. listeners.py and unused_columns.py check
# ``isinstance(context, QueryContext)``.
from sqlalchemy.orm.context import QueryContext

__all__ = ["QueryContext"]

# --- the ORM SELECT of a cursor execution (listeners.orm_select) ----------
# Per statement: read inline.

# DefaultExecutionContext.invoked_statement: the statement object the
# execution runs (not compiled). Undocumented attribute, 2.0.13+.
INVOKED_STATEMENT = "invoked_statement"
# Executable._propagate_attrs: {"compile_state_plugin": "orm", ...} for a
# statement that names ORM entities. 2.0.13+.
PROPAGATE_ATTRS = "_propagate_attrs"
COMPILE_STATE_PLUGIN = "compile_state_plugin"
ORM_PLUGIN = "orm"
# The ORM statement's compile options (ORMCompileState.default_compile_options
# subclass). 2.0.13+.
COMPILE_OPTIONS = "_compile_options"
# ... their flag that Query.enable_eagerloads(False) clears; absent from the
# compile options of a plain select(). 2.0.13+.
ENABLE_EAGERLOADS = "_enable_eagerloads"
# Select._raw_columns: what the select() names, an entity as its annotated
# table or alias, whose _annotations hold the "parententity" (the Mapper or
# AliasedInsp). 2.0.13+.
RAW_COLUMNS = "_raw_columns"
ANNOTATIONS = "_annotations"
PARENT_ENTITY = "parententity"
# ... their fields: the path of a relationship loader's statement (a
# PathRegistry; ``is_root`` for a top-level query, ``[-1]`` the
# relationship), whether the statement reloads one instance (a refresh, an
# expired or deferred column load), and which attributes it loads (None:
# all). 2.0.13+.
CURRENT_PATH = "_current_path"
FOR_REFRESH_STATE = "_for_refresh_state"
ONLY_LOAD_PROPS = "_only_load_props"
# Execution option with the ORM's load options of the statement
# (QueryContext.default_load_options subclass). 2.0.13+.
LOAD_OPTIONS = "_sa_orm_load_options"
# ... their fields: the InstanceState a column load reloads, whether it is a
# ``session.refresh()``, the InstanceState whose lazy load this is. 2.0.13+.
REFRESH_STATE = "_refresh_state"
IS_USER_REFRESH = "_is_user_refresh"
LAZY_LOADED_FROM = "_lazy_loaded_from"
# Execution option the selectin and immediate loaders pass their statement:
# the outer query's QueryContext (``.refresh_state``, ``.session``). Not set
# by a subquery loader. 2.0.13+.
TOP_LEVEL_ORM_CONTEXT = "sa_top_level_orm_context"

# --- ORMExecuteState (do_orm_execute: strict mode, eager check, heal) ------

# The do_orm_execute listeners still to run for this execution; the heal
# (listeners._on_orm_execute_heal) acts only when all of them are the
# plugin's. 2.0.13+.
EVENTS_TODO = "_events_todo"
# The statement's loader options (``.options(...)``); per query, read by the
# unused eager-load and the cartesian-load checks. 2.0.13+.
WITH_OPTIONS = "_with_options"


def load_option_attribute(option: str) -> str:
    """The load-options field that holds execution option ``option``.

    ``yield_per`` -> ``_yield_per``: the load options keep each execution
    option they read under its name with a leading underscore (2.0.13+). The
    heal compares them to find the options a loader statement inherited.
    """
    return "_" + option


# --- loader options (eager.loader_paths) -----------------------------------
# A ``Load`` option's ``context`` is a tuple of _LoadElement; each has a
# ``path`` (a PathRegistry whose ``path`` alternates mappers and
# relationships), ``local_opts`` and ``strategy`` (``(("lazy", "joined"),)``,
# None for ``defaultload()``). Undocumented, 2.0.13+.
LOAD_CONTEXT = "context"
ELEMENT_PATH = "path"
ELEMENT_LOCAL_OPTS = "local_opts"
ELEMENT_STRATEGY = "strategy"
# A wildcard option's path ends in a token, a string: "relationship:*" for
# ``Load(A).lazyload("*")``, "relationship:_sa_default" for an unbound
# ``lazyload("*")`` (whose option has a ``path`` and no ``context``). 2.0.13+.
RELATIONSHIP_TOKEN = "relationship:"
# ``contains_eager()``'s marker in ``local_opts``. 2.0.13+.
EAGER_FROM_ALIAS = "eager_from_alias"

# --- QueryContext and InstanceState (parent sites, unused columns) ---------
# QueryContext.top_level_context (the outer query's context for a loader's
# statement), .load_options, .query (the statement), .runid (one per
# result), .refresh_state; InstanceState.load_path (the path that loaded
# it). Undocumented attributes, 2.0.13+.
TOP_LEVEL_CONTEXT = "top_level_context"
QUERY_LOAD_OPTIONS = "load_options"
QUERY_STATEMENT = "query"
# Select._where_criteria: the WHERE clauses of a select(), ANDed. 2.0.13+.
WHERE_CRITERIA = "_where_criteria"
RUNID = "runid"
QUERY_REFRESH_STATE = "refresh_state"
STATE_LOAD_PATH = "load_path"
# The attribute a mapped instance keeps its InstanceState in
# (``sqlalchemy.orm.base.DEFAULT_STATE_ATTR``). readhook.py spells it
# ``instance._sa_instance_state``, inline: it runs on every attribute read.
INSTANCE_STATE = "_sa_instance_state"

# --- frames and code objects (parent sites, flush numbers) -----------------


def nested_code(function: Any, name: str, variable: str) -> Any:
    """The code of the function ``name`` defined in ``function`` that reads
    ``variable`` from it; None when this SQLAlchemy has no such function."""
    for const in getattr(getattr(function, "__code__", None), "co_consts", ()):
        if getattr(const, "co_name", None) == name and variable in const.co_freevars:
            return const
    return None


# The generator in loading.instances() that turns the rows of one result
# into instances; its frame has the locals ``cursor`` (the CursorResult,
# whose ``.context`` is the execution context) and ``context`` (the
# QueryContext). And the function of loading._instance_processor() that
# builds one instance from a row, with the local ``load_path``. 2.0.13+;
# None when gone (no parent site, no load path of a reread instance).
CHUNKS_CODE = nested_code(getattr(_loading, "instances", None), "chunks", "cursor")
CHUNKS_CURSOR = "cursor"
CHUNKS_CONTEXT = "context"
INSTANCE_CODE = nested_code(
    getattr(_loading, "_instance_processor", None), "_instance", "load_path"
)
INSTANCE_LOAD_PATH = "load_path"

# Session._flush (behind a generated wrapper, so found by name and file)
# holds the UOWTransaction of one flush in its local ``flush_context``.
# 2.0.13+; with no such frame no statement gets a flush number.
SESSION_FILE: str | None = getattr(_orm_session, "__file__", None)
FLUSH_FUNCTION = "_flush"
FLUSH_CONTEXT = "flush_context"

# --- descriptors, engines, drivers ----------------------------------------

# The descriptor of every mapped attribute; readhook.py replaces its
# ``__get__`` (defined on QueryableAttribute's subclass InstrumentedAttribute,
# 2.0.13+) while a check that needs reads is on.
ATTRIBUTE_DESCRIPTOR = _InstrumentedAttribute


def attribute_get(descriptor: type[Any] = ATTRIBUTE_DESCRIPTOR) -> Any:
    """The ``__get__`` readhook.py wraps and restores."""
    return descriptor.__get__


# ``engine.execution_options(...)`` returns an OptionEngine whose
# ``_proxied`` is the engine it was made from, and which shares its
# listeners. 2.0.13+.
OPTION_ENGINE_PARENT = "_proxied"


def option_engine_parent(engine: Any) -> Any:
    """The engine ``engine`` was made from with ``execution_options()``, or None."""
    return getattr(engine, OPTION_ENGINE_PARENT, None)


# Driver internals of the MySQL index check: SQLAlchemy's async adapter
# cursor (aiomysql, asyncmy) keeps the driver's cursor in ``_cursor`` (2.0.13+),
# and PyMySQL's cursor its result in ``_result``, whose
# ``unbuffered_active`` is True while an SSCursor has rows to fetch.
ADAPTED_CURSOR = "_cursor"
PYMYSQL_RESULT = "_result"
