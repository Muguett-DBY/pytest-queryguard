"""The generated documentation site: metadata, sitemap, search index and navigation."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_docs.py"

pytestmark = [
    pytest.mark.skipif(not SCRIPT.is_file(), reason="scripts/ is not in the sdist"),
    pytest.mark.skipif(
        sys.version_info < (3, 11), reason="docs/site.toml needs tomllib"
    ),
]


def _load() -> ModuleType:
    pytest.importorskip("markdown")
    pytest.importorskip("pygments")
    spec = importlib.util.spec_from_file_location("build_docs_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("site")
    assert _load().build(out, strict=True) == 0
    return out


def _pages(site: Path) -> list[Path]:
    return sorted(site.glob("*.html"))


def test_every_page_has_title_description_and_open_graph(site: Path) -> None:
    pages = _pages(site)
    assert len(pages) > 20
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert re.search(r"<title>[^<]+</title>", text), page.name
        description = re.search(r'<meta name="description" content="([^"]+)"', text)
        assert description and len(description.group(1)) > 20, page.name
        for prop in ("og:title", "og:description", "og:url", "og:image"):
            assert f'<meta property="{prop}"' in text, (page.name, prop)
        assert '<link rel="canonical"' in text, page.name
        assert 'rel="icon"' in text, page.name


def test_no_markdown_is_left_unrendered(site: Path) -> None:
    # A line that starts with an HTML tag, such as "<table> may ...", opens a
    # raw HTML block that leaves the rest of the page as Markdown source.
    for page in _pages(site):
        text = re.sub(
            r"<pre.*?</pre>", "", page.read_text(encoding="utf-8"), flags=re.DOTALL
        )
        assert not re.search(r"^(#{1,6} |\|---|```)", text, flags=re.MULTILINE), (
            page.name
        )


def test_the_home_page_title_has_the_tagline(site: Path) -> None:
    text = (site / "index.html").read_text(encoding="utf-8")
    assert "<title>pytest-queryguard: " in text


def test_sitemap_lists_every_page_and_robots_points_to_it(site: Path) -> None:
    sitemap = (site / "sitemap.xml").read_text(encoding="utf-8")
    urls = re.findall(r"<loc>([^<]+)</loc>", sitemap)
    base = "https://maciejziel.github.io/pytest-queryguard/"
    assert urls[0] == base
    assert {u.removeprefix(base) for u in urls[1:]} == {
        p.name for p in _pages(site) if p.name != "index.html"
    }
    robots = (site / "robots.txt").read_text(encoding="utf-8")
    assert f"Sitemap: {base}sitemap.xml" in robots


def test_favicon_and_social_image_are_published(site: Path) -> None:
    assert (site / "favicon.svg").read_text(encoding="utf-8").startswith("<svg")
    png = (site / "social-preview.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(png[16:20], "big") == 1280
    assert int.from_bytes(png[20:24], "big") == 640


def test_search_index_finds_sections(site: Path) -> None:
    entries = json.loads((site / "search-index.json").read_text(encoding="utf-8"))
    assert all(set(e) == {"title", "url", "text"} for e in entries)
    hits = [e for e in entries if "queryguard_baseline" in e["text"]]
    assert any(e["url"].startswith("baseline.html") for e in hits)
    sections = [e for e in entries if e["url"].startswith("configuration.html#")]
    assert sections, "sections link to their heading anchors"
    assert "getElementById" in (site / "search.js").read_text(encoding="utf-8")
    assert 'id="q"' in (site / "index.html").read_text(encoding="utf-8")


def test_edit_links_and_previous_next(site: Path) -> None:
    faq = (site / "faq.html").read_text(encoding="utf-8")
    assert (
        'href="https://github.com/MaciejZiel/pytest-queryguard/edit/main/docs/faq.md"'
        in faq
    )
    log = (site / "changelog.html").read_text(encoding="utf-8")
    assert "/edit/main/CHANGELOG.md" in log
    assert 'rel="prev"' in faq and 'rel="next"' in faq
    first = (site / "index.html").read_text(encoding="utf-8")
    assert 'rel="prev"' not in first and 'rel="next"' in first
    pages = _pages(site)
    last = max(pages, key=lambda p: ('rel="next"' not in p.read_text("utf-8"), 0))
    assert 'rel="next"' not in last.read_text(encoding="utf-8")


def test_a_page_without_a_description_fails_the_strict_build(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# Home\n\ntext\n", encoding="utf-8")
    (docs / "site.toml").write_text(
        'title = "T"\nbase_url = "https://x.test/"\nrepo_url = "https://x.test/r"\n'
        '[[section]]\ntitle = "S"\npages = [{ title = "Home", file = "index.md" }]\n',
        encoding="utf-8",
    )
    module.DOCS = docs
    module.ROOT = tmp_path
    assert module.build(tmp_path / "out", strict=True) == 1
    assert "has no description" in capsys.readouterr().err


def test_a_link_to_a_missing_anchor_fails_the_strict_build(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A renamed heading breaks every link to it; the build has to say where."""
    module = _load()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text(
        "# Home\n\n[ok](other.md#real) [same page](#home) "
        "[broken](other.md#gone) [broken here](#nowhere)\n",
        encoding="utf-8",
    )
    (docs / "other.md").write_text("# Other\n\n## Real\n\ntext\n", encoding="utf-8")
    (docs / "site.toml").write_text(
        'title = "T"\nbase_url = "https://x.test/"\nrepo_url = "https://x.test/r"\n'
        '[[section]]\ntitle = "S"\npages = [\n'
        '  { title = "Home", file = "index.md", description = "d" },\n'
        '  { title = "Other", file = "other.md", description = "d" },\n]\n',
        encoding="utf-8",
    )
    module.DOCS = docs
    module.ROOT = tmp_path
    assert module.build(tmp_path / "out", strict=True) == 1
    err = capsys.readouterr().err
    assert "index.md: no anchor #gone in other.md" in err
    assert "index.md: no anchor #nowhere in index.md" in err
    assert "#real" not in err and "#home" not in err


def test_inline_code_wraps_on_narrow_screens(site: Path) -> None:
    """A long inline name would otherwise make the whole page scroll sideways
    on a phone; code blocks keep their own horizontal scroll."""
    css = (site / "style.css").read_text(encoding="utf-8")
    start = css.index("{", css.index("@media (max-width:48rem)")) + 1
    depth, end = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(css[end], 0)
        end += 1
    narrow = re.sub(r"\s+", "", css[start:end])
    assert re.search(r":not\(pre\)>code\{[^}]*overflow-wrap:anywhere", narrow)
    wide = re.sub(r"\s+", "", css[:start] + css[end:])
    assert "overflow-wrap:anywhere" not in wide


def test_the_page_list_collapses_on_narrow_screens(site: Path) -> None:
    """On a phone the list of pages would push the content two screens down."""
    page = (site / "faq.html").read_text(encoding="utf-8")
    nav = page[page.index('<nav class="side">') : page.index("</nav>")]
    details = nav[nav.index('<details class="toc" open>') : nav.index("</details>")]
    assert "<summary>All pages</summary>" in details
    assert 'href="getting-started.html"' in details
    assert 'id="q"' not in details  # the search box stays outside, always visible
    script = nav[nav.index("</details>") :]
    assert "matchMedia('(max-width:48rem)')" in script
    css = (site / "style.css").read_text(encoding="utf-8")
    assert "nav.side summary{display:none}" in css


def test_the_docs_link_of_every_finding_kind_has_its_page_and_anchor(
    site: Path,
) -> None:
    # The report prints these links (docs_url); a renamed heading breaks them.
    from pytest_queryguard.findings import _KIND_DOCS, DOCS_URL, docs_url

    for kind, target in _KIND_DOCS.items():
        assert docs_url(kind) == DOCS_URL + target
        page, _, anchor = target.partition("#")
        text = (site / page).read_text(encoding="utf-8")
        if anchor:
            assert f'id="{anchor}"' in text, f"{kind}: no #{anchor} in {page}"
