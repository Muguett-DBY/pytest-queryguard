from __future__ import annotations

from .api import (
    NPlusOneDetected,
    Query,
    QueryBudgetExceeded,
    QueryGuardFixture,
    QueryLog,
    max_db_time,
    max_queries,
    record_queries,
)
from .blocks import ignore
from .events import CallSite
from .warnings import (
    QueryGuardDeprecationWarning,
    QueryGuardInternalWarning,
    QueryGuardWarning,
)

__version__ = "0.7.0"

__all__ = [
    "CallSite",
    "NPlusOneDetected",
    "Query",
    "QueryBudgetExceeded",
    "QueryGuardDeprecationWarning",
    "QueryGuardFixture",
    "QueryGuardInternalWarning",
    "QueryGuardWarning",
    "QueryLog",
    "__version__",
    "ignore",
    "max_db_time",
    "max_queries",
    "record_queries",
]

# The names above are defined in submodules that are free to move. Report the
# package as their module, so that tracebacks, reprs and pickles show the
# documented import path (``pytest_queryguard.NPlusOneDetected``) and stay the
# same when a class moves to another file.
for _name in __all__:
    _obj = globals()[_name]
    if isinstance(getattr(_obj, "__module__", None), str):
        _obj.__module__ = __name__
del _name, _obj
