"""BUG relationship-load: a Jinja template iterates a lazy collection.

``{% for b in a.books %}`` in a template is the view-layer version of the lazy
load loop; the template engine runs the attribute access, not the application.
"""

from jinja2 import Template
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domains import library
from ..kit import BUG, REL, Ctx

VERDICT = BUG
KINDS = frozenset({REL})
PATCH = True
setup = library.setup

PAGE = Template(
    "{% for a in authors %}<h2>{{ a.name }}</h2>"
    "{% for b in a.books %}<li>{{ b.title }}</li>{% endfor %}{% endfor %}"
)


def run(ctx: Ctx) -> None:
    with Session(ctx.engine()) as session:
        html = PAGE.render(authors=session.scalars(select(library.Author)).all())
    assert "<li>book18</li>" in html
