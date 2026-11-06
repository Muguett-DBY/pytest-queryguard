"""BUG cartesian-load and unused-columns: an admin CSV export joins three collections.

Comments, tags and attachments of every article in one SELECT: the rows per
article are comments x tags x attachments. The export reads all three, so
the eager loads are needed; the way they are loaded is the problem. It also
loads all twelve columns of each article, the 2 KB ``body`` included, to
write its id and title.
"""

import csv
import io

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..domains import content
from ..kit import BUG, CARTESIAN, COLUMNS, Ctx

VERDICT = BUG
KINDS = frozenset({CARTESIAN, COLUMNS})
setup = content.setup


def export_articles(session: Session) -> str:
    stmt = (
        select(content.Article)
        .order_by(content.Article.id)
        .options(
            joinedload(content.Article.comments),
            joinedload(content.Article.tags),
            joinedload(content.Article.attachments),
        )
    )
    out = io.StringIO()
    writer = csv.writer(out)
    for article in session.scalars(stmt).unique():
        writer.writerow(
            [
                article.id,
                article.title,
                len(article.comments),
                ";".join(t.name for t in article.tags),
                sum(a.size for a in article.attachments),
            ]
        )
    return out.getvalue()


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        text = export_articles(session)
    assert text.count("\n") == 20
