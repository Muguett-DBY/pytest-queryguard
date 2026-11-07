# Recipe: GraphQL resolvers

GraphQL libraries such as Strawberry, Graphene and Ariadne call a field's
resolver once per object. A query like

```graphql
{ books { title author { name } } }
```

runs the `books` resolver once and the `author` resolver once per book. When
`author` reads a lazy relationship, that is an N+1 that no loop in your code
shows. pytest-queryguard sees the SQL, not the GraphQL layer, so it reports it
like any other N+1, at the line of the resolver.

This page uses no GraphQL library: a few lines of plain Python play the
executor, calling the resolvers the way a library does. The two fixes are the
same with a library: load the relationship in the parent resolver's query
(`selectinload`), or batch the per-object lookups with a data loader. The
project was run for this page with SQLAlchemy 2.0.13 and 2.1.1; a test builds
it from the files of this page and runs it.

The layout is `pytest.ini`, `app/__init__.py` and `tests/__init__.py` (both
empty), `app/models.py`, `app/loader.py`, `app/schema.py`, `tests/conftest.py`
and `tests/test_books.py`.

## The application

`pytest.ini`:

```ini
[pytest]
pythonpath = .
```

`app/models.py`:

```python
from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "author"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class Book(Base):
    __tablename__ = "book"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"))
    author: Mapped[Author] = relationship()
```

`app/loader.py` is a minimal data loader. `load(key)` returns an awaitable;
the keys asked for while the resolvers of one level run are collected, and
one call of the batch function loads them all. Strawberry's
`strawberry.dataloader.DataLoader` and `aiodataloader` (used with Graphene)
work the same way, with caching and more this one leaves out:

```python
import asyncio
from collections.abc import Callable, Hashable, Sequence
from typing import Any


class DataLoader:
    def __init__(self, batch_load: Callable[[list[Hashable]], Sequence[Any]]) -> None:
        self.batch_load = batch_load
        self.waiting: dict[Hashable, asyncio.Future[Any]] = {}

    def load(self, key: Hashable) -> "asyncio.Future[Any]":
        if key not in self.waiting:
            loop = asyncio.get_running_loop()
            if not self.waiting:
                loop.call_soon(self._dispatch)
            self.waiting[key] = loop.create_future()
        return self.waiting[key]

    def _dispatch(self) -> None:
        waiting, self.waiting = self.waiting, {}
        try:
            values = self.batch_load(list(waiting))
        except Exception as error:  # every waiting resolver gets the error
            for future in waiting.values():
                future.set_exception(error)
            return
        for future, value in zip(waiting.values(), values):
            future.set_result(value)
```

`app/schema.py` has the resolvers and `execute()`, which plays the executor
for `{ books { title author { name } } }`: it calls the `books` resolver, then
the `author` resolver of every book concurrently, as an async GraphQL library
does. There are three versions of the `author` resolver and the root resolver:

```python
import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.loader import DataLoader
from app.models import Author, Book


class Context:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.authors = DataLoader(self.load_authors)

    def load_authors(self, ids: list[int]) -> list[Author]:
        stmt = select(Author).where(Author.id.in_(ids))
        found = {author.id: author for author in self.session.scalars(stmt)}
        return [found[i] for i in ids]


# Query.books
def books(context: Context) -> list[Book]:
    return list(context.session.scalars(select(Book)))


def books_with_authors(context: Context) -> list[Book]:
    stmt = select(Book).options(selectinload(Book.author))
    return list(context.session.scalars(stmt))


# Book.author, called once per book
async def author(book: Book, context: Context) -> Author:
    return book.author


async def author_from_loader(book: Book, context: Context) -> Author:
    return await context.authors.load(book.author_id)


def execute(session: Session, root=books, field=author) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        context = Context(session)
        found = root(context)
        authors = await asyncio.gather(*(field(book, context) for book in found))
        return [
            {"title": book.title, "author": {"name": writer.name}}
            for book, writer in zip(found, authors)
        ]

    return asyncio.run(run())
```

## The tests

`tests/conftest.py` seeds five books by five authors in a fixture, so the
seeding is not measured:

```python
from collections.abc import Iterator

import pytest
from app.models import Author, Base, Book
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(Author(id=i, name=f"author {i}") for i in range(1, 6))
        session.add_all(Book(id=i, title=f"book {i}", author_id=i) for i in range(1, 6))
        session.commit()
        yield session
    engine.dispose()
```

`tests/test_books.py` runs the query three ways:

```python
from app import schema


def test_resolver_per_book(session):
    result = schema.execute(session)
    assert result[0] == {"title": "book 1", "author": {"name": "author 1"}}


def test_selectinload_in_the_root_resolver(session):
    result = schema.execute(session, root=schema.books_with_authors)
    assert len(result) == 5


def test_data_loader(session):
    result = schema.execute(session, field=schema.author_from_loader)
    assert result[4] == {"title": "book 5", "author": {"name": "author 5"}}
```

The first test fails. The finding points at the resolver, and `parents
loaded at` names the root resolver whose query loaded the books. The resolver
ran in an asyncio task, and the `called from` lines go through the event loop
back to `execute()` and the test:

```
queryguard: N+1 detected

  Book.author was lazy-loaded 5 times
  first at app/schema.py:34 in author
    called from app/schema.py:51 in execute
    called from tests/test_books.py:5 in test_resolver_per_book
  parents loaded at app/schema.py:24 in books
  kind: relationship-load  label: Book.author
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT author.id, author.name FROM author WHERE author.id = ?
  fix: load it with the parent query, e.g.
       select(Book).options(joinedload(Book.author))
```

The other two pass:

- `books_with_authors` loads the authors with the books, in a second
  `SELECT ... WHERE author.id IN (...)`, so the resolver reads a loaded
  relationship. This is the simplest fix when the root resolver knows which
  fields will be asked for; the `info` argument that GraphQL libraries pass to
  a resolver describes the selected fields, so the option can be added only
  when `author` is in the query.
- `author_from_loader` asks the loader for the author by `author_id`, a
  column that is already loaded, so it does not touch the relationship. The
  five calls are collected and the loader runs one `SELECT ... WHERE author.id
  IN (...)`. This works for any resolver, wherever its parent came from.

A data loader whose batch function still runs one query per key (a loop over
the keys with `session.get()`) is reported as a repeated statement, with the
batch function as the call site. Create the loaders per request (here, per
`Context`): the loaders of the libraries cache by key, and one shared across
requests would serve one request's rows to another.
