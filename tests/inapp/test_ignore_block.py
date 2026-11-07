"""An ignore() block in the application leaves out the in-app findings too (#234)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pytest_queryguard import app as queryguard
from pytest_queryguard import blocks, ignore
from pytest_queryguard.collector import QueryCollector

from .conftest import Order


def _loop(session: Session) -> None:
    for order in session.scalars(select(Order)).all():
        _ = order.customer.name


def test_a_block_leaves_out_the_finding(watched: Engine) -> None:
    with (
        Session(watched) as session,
        queryguard.track(report=[]) as report,
        ignore("relationship-load"),
    ):
        _loop(session)
    assert report.findings == ()
    assert report.statement_count > 1  # still counted for budgets


def test_without_the_block_it_is_reported(watched: Engine) -> None:
    with Session(watched) as session, queryguard.track(report=[]) as report:
        _loop(session)
    (finding,) = report.findings
    assert finding.label == "Order.customer"


def test_a_block_used_by_the_report_is_noted_for_the_test(watched: Engine) -> None:
    # In a measured test, the plugin's stale-block report counts a block that
    # left out an in-app finding (#284).
    target = QueryCollector()
    token = blocks.record_entered(target)
    try:
        block = ignore("relationship-load")
        with Session(watched) as session, queryguard.track(report=[]), block:
            _loop(session)
    finally:
        blocks.stop_recording(token)
    assert list(target.blocks_entered.values()) == [block]
    assert list(target.blocks_used or ()) == [block]
