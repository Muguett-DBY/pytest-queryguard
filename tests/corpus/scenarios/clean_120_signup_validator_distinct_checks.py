"""CLEAN: a sign-up validator that checks five different fields once each.

Shape from Prosopite #1 (a false positive there). One form submission checks
that the e-mail and name are free, that the referral customer exists, that the
chosen category exists and that the promo tag exists: five ``exists()``
queries of five different shapes, each run once. Nothing repeats.
"""

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import CLEAN, Ctx

VERDICT = CLEAN
KINDS = frozenset()
setup = shop.setup


def validate(session: Session, form: dict[str, object]) -> list[str]:
    checks = {
        "email taken": exists().where(shop.Customer.email == form["email"]),
        "name taken": exists().where(shop.Customer.name == form["name"]),
        "no referrer": ~exists().where(shop.Customer.id == form["referrer"]),
        "no category": ~exists().where(shop.Category.id == form["category"]),
        "no promo": ~exists().where(shop.Tag.name == form["promo"]),
    }
    return [error for error, check in checks.items() if session.scalar(select(check))]


def run(ctx: Ctx) -> None:
    form = {
        "email": "new@example.com",
        "name": "newcomer",
        "referrer": 3,
        "category": 2,
        "promo": "tag1",
    }
    with Session(ctx.engine()) as session:
        assert validate(session, form) == []
