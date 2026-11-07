# Recipes

Short, runnable answers to "how do I use this with ...". Each page that shows
output was produced by running the code on it, and says with which versions.

| Page | What it covers |
|---|---|
| [FastAPI](recipes-fastapi.md) | `TestClient` and `httpx.AsyncClient` with dependency-overridden sessions |
| [Flask-SQLAlchemy](recipes-flask-sqlalchemy.md) | The test client and `db.session` |
| [SQLModel](recipes-sqlmodel.md) | `Session.exec()` and relationships |
| [Litestar](recipes-litestar.md) | `TestClient` with an overridden session dependency |
| [pytest-asyncio](recipes-pytest-asyncio.md) | Configuration pitfalls for async tests and fixtures |
| [Budgets for hot endpoints](recipes-budgets.md) | `max_queries` and `max_db_time` on the endpoints that matter |
| [A session per request in a test transaction](recipes-test-transaction.md) | Per-request sessions that join the test's rolled-back transaction, instead of one shared `Session` |
| [GraphQL resolvers](recipes-graphql.md) | Per-object resolvers: a batch loader or `selectinload` |
| [Background tasks in tests](recipes-background-tasks.md) | Tasks run in the test body, with their own session |
| [Factory Boy and seeding](recipes-factory-boy.md) | Seeding rows in the test body: fixtures, or ignoring write loops in tests |
| [Adopting on a legacy codebase](recipes-legacy.md) | Warn mode, a baseline, then fail |
| [CI](recipes-ci.md) | GitHub Actions, GitLab CI, and why there is no pre-commit hook |

The versions used for the output on these pages: Python 3.12, pytest 9.1.1,
SQLAlchemy 2.1.4, pytest-queryguard 0.4.0. Messages from older SQLAlchemy
releases differ in small ways, for example column aliases in the SQL.
Before Python 3.12 a list comprehension is a frame of its own, so a finding
raised inside one says `in <listcomp>` instead of the name of the function.
