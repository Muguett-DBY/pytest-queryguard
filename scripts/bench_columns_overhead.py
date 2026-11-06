"""Time what the experimental unused-columns check costs.

Usage::

    uv run python scripts/bench_columns_overhead.py [--reads 200000] [--runs 7]

Three configurations, each in a fresh Python process, interleaved ``--runs``
times (median reported). In all three the plugin's listeners are installed
and a collector is active, as in the body of a measured test, so the
difference is the check alone:

* ``off``: the check is not enabled, nothing of it is installed (the default);
* ``idle``: ``unused_columns.enable()`` was called but no test body is being
  tracked (fixtures, setup and teardown of every test while the check is on);
* ``on``: enabled and tracking, as around the body of each test.

Two loops: attribute reads of loaded instances of a 12-column class, in
nanoseconds per read, and ORM ``SELECT`` statements that load 50 such rows
each, in microseconds per statement.
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time

from sqlalchemy import create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Wide(Base):
    __tablename__ = "wide"

    id: Mapped[int] = mapped_column(primary_key=True)
    c1: Mapped[str] = mapped_column(default="v")
    c2: Mapped[str] = mapped_column(default="v")
    c3: Mapped[str] = mapped_column(default="v")
    c4: Mapped[str] = mapped_column(default="v")
    c5: Mapped[str] = mapped_column(default="v")
    c6: Mapped[str] = mapped_column(default="v")
    c7: Mapped[str] = mapped_column(default="v")
    c8: Mapped[str] = mapped_column(default="v")
    c9: Mapped[str] = mapped_column(default="v")
    c10: Mapped[str] = mapped_column(default="v")
    c11: Mapped[str] = mapped_column(default="v")


def _child(mode: str, reads: int) -> None:
    from pytest_queryguard import unused_columns
    from pytest_queryguard.collector import QueryCollector, pop, push
    from pytest_queryguard.listeners import install

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as seed:
        seed.add_all(Wide(id=i) for i in range(1, 51))
        seed.commit()
    install()
    collector = QueryCollector()
    collector.max_events = 1000  # a long loop: keep the stored events bounded
    push(collector)
    tracker = None
    if mode != "off":
        unused_columns.enable()
    if mode == "on":
        tracker = unused_columns.start()
    session = Session(engine)
    rows = session.scalars(select(Wide)).all()

    def attributes() -> None:
        for i in range(reads):
            row = rows[i % 50]
            row.c1  # noqa: B018
            row.c2  # noqa: B018

    def selects() -> None:
        for _ in range(500):
            session.scalars(select(Wide)).all()
            session.expunge_all()

    out = []
    attributes()
    start = time.perf_counter()
    attributes()
    out.append((time.perf_counter() - start) / (reads * 2) * 1e9)
    selects()
    start = time.perf_counter()
    selects()
    out.append((time.perf_counter() - start) / 500 * 1e6)
    if tracker is not None:
        unused_columns.stop(tracker)
    pop(collector)
    print(f"{out[0]:.1f} {out[1]:.1f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reads", type=int, default=200_000)
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--child", choices=("off", "idle", "on"))
    args = parser.parse_args()
    if args.child:
        _child(args.child, args.reads)
        return
    times: dict[str, list[tuple[float, float]]] = {"off": [], "idle": [], "on": []}
    for _ in range(args.runs):
        for mode, values in times.items():
            out = subprocess.run(
                [sys.executable, __file__, "--child", mode, "--reads", str(args.reads)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.split()
            values.append((float(out[0]), float(out[1])))
    print(f"median of {args.runs} runs")
    print(f"{'mode':<6}{'ns/attribute read':>20}{'us/select of 50 rows':>24}")
    for mode, values in times.items():
        read = statistics.median(v[0] for v in values)
        select_ = statistics.median(v[1] for v in values)
        print(f"{mode:<6}{read:>20.1f}{select_:>24.1f}")


if __name__ == "__main__":
    main()
