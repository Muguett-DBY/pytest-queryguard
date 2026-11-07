"""BUG repeated-statement: ten lookups fanned out over a thread pool.

Each worker thread opens a session and fetches one row. The statements come
from several threads; the plugin counts statements from any thread.
"""

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from ..domains import shop
from ..kit import BUG, REPEAT, Ctx

VERDICT = BUG
KINDS = frozenset({REPEAT})
KNOWN = {
    "session-scope": "by design: with queryguard_scope = session each Session is counted on its own, and each runs one lookup"
}
setup = shop.setup


def run(ctx: Ctx) -> None:
    def fetch(product_id: int) -> str:
        with Session(ctx.engine()) as session:
            return session.get(shop.Product, product_id).name

    with ThreadPoolExecutor(max_workers=4) as pool:
        names = list(pool.map(fetch, range(1, 11)))
    assert len(names) == 10
