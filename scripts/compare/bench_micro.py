"""Per-statement overhead of queryguard and the other SQLAlchemy N+1 tools.

Child: python bench_micro.py TOOL STATEMENTS BLOCKS -> JSON {loop: [us/stmt per block]}
TOOL: off | noop | queryguard | queryspy | querycount | sniffer | nplusone
(sniffer and nplusone: scripts/head_to_head/overhead.py)
"""

from __future__ import annotations

import gc
import json
import sys
import time

from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    children: Mapped[list[Kid]] = relationship()


class Kid(Base):
    __tablename__ = "kids"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))


def database():
    md = MetaData()
    table = Table(
        "plain", md, Column("id", Integer, primary_key=True), Column("n", String)
    )
    engine = create_engine("sqlite://", poolclass=StaticPool)
    md.create_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all(
            Item(
                id=i, name=f"item {i}", children=[Kid(id=i * 10 + j) for j in range(2)]
            )
            for i in range(1, 51)
        )
        s.commit()
    return engine, table


def core_loop(engine, table, n):
    with engine.connect() as conn:
        for i in range(n):
            conn.execute(select(table).where(table.c.id == i % 50)).all()


def orm_loop(engine, n):
    with Session(engine) as session:
        for i in range(n):
            session.execute(select(Item).where(Item.id == i % 50 + 1)).scalars().all()
            session.expunge_all()


def lazy_loop(engine, n):
    # One lazy relationship load (the N+1 statement itself) per iteration.
    with Session(engine) as session:
        items = session.scalars(select(Item)).all()
        for i in range(n):
            item = items[i % 50]
            session.expire(item, ["children"])
            _ = item.children  # the lazy load


def setup(tool, engine):
    if tool == "noop":

        def nop(*a, **k):
            return None

        event.listen(Session, "do_orm_execute", nop)
        event.listen(Session, "after_begin", nop)
        event.listen(Session, "after_flush", nop)
        event.listen(Engine, "before_cursor_execute", nop)
        event.listen(Engine, "after_cursor_execute", nop)
        return lambda: None, lambda: 0
    if tool == "queryguard":
        from pytest_queryguard.collector import QueryCollector, pop, push
        from pytest_queryguard.listeners import install

        install()
        holder = {}

        def start():
            holder["c"] = QueryCollector()
            push(holder["c"])

        def stop():
            pop(holder["c"])
            return len(holder["c"].statements)

        return start, stop
    if tool == "queryspy":
        from queryspy._recorder import Recorder
        from queryspy._recorder import start as qs_start
        from queryspy._recorder import stop as qs_stop

        holder = {}

        def start():
            holder["r"] = Recorder(capture_stacks=True)
            qs_start(holder["r"])

        def stop():
            qs_stop(holder["r"])
            return holder["r"].cursor_count

        return start, stop
    if tool == "querycount":
        from pytest_querycount import backends
        from pytest_querycount.recorder import Recorder

        backends.install_all()
        holder = {}

        def start():
            holder["r"] = Recorder()
            backends.push(holder["r"])

        def stop():
            backends.pop(holder["r"])
            return holder["r"].count

        return start, stop
    if tool == "sniffer":
        # n-plus-one-sniffer: its context manager on the engine, as documented
        from n_plus_one_sniffer import catch_n_plus_one

        holder = {}

        def start():
            holder["s"] = catch_n_plus_one(engine).start()

        def stop():
            sniffer = holder["s"]
            sniffer.stop()
            return sum(r.count for r in sniffer._records.values())

        return start, stop
    if tool == "nplusone":
        # nplusone: the import patches SQLAlchemy; its Profiler, with every
        # notification allowed, so the detection runs but never raises.
        import nplusone.ext.sqlalchemy  # noqa: F401
        from nplusone.core import profiler

        holder = {}
        allow = [{"label": "n_plus_one"}, {"label": "unused_eager_load"}]

        def start():
            holder["p"] = profiler.Profiler(whitelist=allow)
            holder["p"].__enter__()

        def stop():
            holder["p"].__exit__(None, None, None)
            return -1  # it does not count statements

        return start, stop
    return lambda: None, lambda: 0


def main():
    tool, n, blocks = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    engine, table = database()
    start, stop = setup(tool, engine)
    loops = {
        "core": lambda: core_loop(engine, table, n),
        "orm": lambda: orm_loop(engine, n),
        "lazy": lambda: lazy_loop(engine, n),
    }
    out = {k: [] for k in loops}
    for name, loop in loops.items():
        start()
        loop()
        stop()  # warm-up
        for _ in range(blocks):
            start()
            gc.collect()
            t = time.perf_counter()
            loop()
            el = time.perf_counter() - t
            seen = stop()
            if tool not in ("off", "noop", "nplusone") and name != "lazy" and seen < n:
                raise SystemExit(f"{tool} saw {seen} < {n}")
            out[name].append(el / n * 1e6)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
