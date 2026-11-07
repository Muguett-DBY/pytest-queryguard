"""Build the documentation site from docs/*.md into site/ (static HTML).

    uv run --group docs python scripts/build_docs.py [--strict] [--out DIR]

The pages and their order are listed in docs/site.toml. The builder only needs
the `markdown` and `pygments` packages (the `docs` dependency group). With
--strict, a broken link, a missing anchor, a page that is not in the
navigation, a page without a description or a link that leaves the
documentation without being an absolute URL fails the build.

Besides the pages, the site gets style.css, search.js and search-index.json
(client-side search, no third-party code), favicon.svg, social-preview.png,
sitemap.xml and robots.txt.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import markdown
import tomllib
from pygments.formatters import HtmlFormatter

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

EXTENSIONS = ["tables", "fenced_code", "toc", "admonition", "sane_lists", "codehilite"]
EXTENSION_CONFIGS = {
    "codehilite": {"css_class": "hl", "guess_lang": False},
    "toc": {"permalink": "#", "toc_depth": "2-3"},
}

CSS = """\
:root{color-scheme:light dark;--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;
--code:#f6f8fa;--link:#0550ae;--side:#f6f8fa;--accent:#0b6e4f}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;
--line:#3d444d;--code:#151b23;--link:#79c0ff;--side:#151b23;--accent:#56d4a4}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,sans-serif}
.layout{display:flex;min-height:100vh}
nav.side{width:17rem;flex:none;background:var(--side);border-right:1px solid var(--line);
padding:1.25rem 1rem;position:sticky;top:0;height:100vh;overflow-y:auto}
nav.side .brand{font-weight:700;font-size:1.05rem;display:block;margin-bottom:1rem;
color:var(--fg);text-decoration:none}
nav.side h2{font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;
color:var(--muted);margin:1.1rem 0 .3rem}
nav.side ul{list-style:none;margin:0;padding:0}
nav.side a{display:block;padding:.15rem .4rem;border-radius:4px;color:var(--fg);
text-decoration:none;font-size:.92rem}
nav.side a:hover{background:var(--line)}
nav.side a.current{background:var(--accent);color:#fff}
main{flex:1;min-width:0;max-width:52rem;padding:1.5rem 2rem 4rem}
a{color:var(--link)}
h1,h2,h3{line-height:1.25}
h2{border-bottom:1px solid var(--line);padding-bottom:.25rem;margin-top:2rem}
.headerlink{opacity:0;margin-left:.4rem;text-decoration:none}
:is(h1,h2,h3):hover .headerlink{opacity:.6}
code{background:var(--code);padding:.1em .3em;border-radius:4px;font-size:.9em}
pre{background:var(--code);padding:.8rem 1rem;border-radius:6px;overflow-x:auto;
border:1px solid var(--line)}
pre code{background:none;padding:0;font-size:.85rem}
.hl{background:var(--code)}
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;margin:1rem 0}
th,td{border:1px solid var(--line);padding:.35rem .6rem;text-align:left;vertical-align:top}
th{background:var(--side)}
.admonition{border-left:4px solid var(--accent);background:var(--side);
padding:.1rem 1rem;margin:1rem 0;border-radius:0 6px 6px 0}
.admonition-title{font-weight:700}
.pager{display:flex;justify-content:space-between;gap:1rem;margin-top:3rem;
border-top:1px solid var(--line);padding-top:1rem}
.pager a{display:block;text-decoration:none}
.pager small{display:block;color:var(--muted)}
.pager .next{text-align:right;margin-left:auto}
.edit{margin-top:1.5rem;font-size:.9rem}
.search{position:relative;margin-bottom:1rem}
.search input{width:100%;padding:.35rem .5rem;border:1px solid var(--line);border-radius:4px;
background:var(--bg);color:var(--fg);font:inherit;font-size:.9rem}
.search ul{position:absolute;z-index:2;left:0;right:0;top:100%;margin:.2rem 0 0;max-height:60vh;
overflow-y:auto;background:var(--bg);border:1px solid var(--line);border-radius:4px}
.search ul[hidden]{display:none}
.search li a{padding:.3rem .5rem}
.search li small{display:block;color:var(--muted);font-size:.78rem;line-height:1.3}
.search li.none{padding:.3rem .5rem;color:var(--muted);font-size:.85rem}
nav.side summary{display:none}
@media (max-width:48rem){.layout{flex-direction:column}
nav.side{width:auto;height:auto;position:static;border-right:0;border-bottom:1px solid var(--line)}
nav.side summary{display:list-item;cursor:pointer;font-weight:600;padding:.3rem 0}
main{padding:1rem}
:not(pre)>code{overflow-wrap:anywhere}}
"""


@dataclass
class Page:
    title: str
    source: Path
    url: str  # path of the output file relative to the site root, e.g. "faq.html"
    section: str
    description: str = ""
    edit_path: str = ""  # path of the source in the repository, e.g. "docs/faq.md"
    html: str = ""
    ids: frozenset[str] = frozenset()


def load_pages(config: dict) -> tuple[str, list[Page]]:
    pages: list[Page] = []
    for section in config["section"]:
        for entry in section["pages"]:
            source = (DOCS / entry["file"]).resolve()
            url = source.with_suffix(".html").name
            if source.parent == ROOT:
                url = source.stem.lower() + ".html"
            pages.append(
                Page(
                    entry["title"],
                    source,
                    url,
                    section["title"],
                    description=entry.get("description", ""),
                    edit_path=source.relative_to(ROOT).as_posix(),
                )
            )
    return config["title"], pages


def render(page: Page, errors: list[str]) -> None:
    md = markdown.Markdown(extensions=EXTENSIONS, extension_configs=EXTENSION_CONFIGS)
    body = md.convert(page.source.read_text(encoding="utf-8"))
    page.html = re.sub(
        r"<table>(.*?)</table>",
        r'<div class="table-wrap"><table>\1</table></div>',
        body,
        flags=re.DOTALL,
    )
    page.ids = frozenset(re.findall(r'\sid="([^"]+)"', body))
    if not re.search(r"<h1[ >]", body):
        errors.append(f"{page.source.name}: no top-level heading")


def check_and_rewrite_links(pages: list[Page], errors: list[str]) -> None:
    by_source = {page.source: page for page in pages}
    link = re.compile(r'(<a\s[^>]*?href=")([^"]*)(")')

    def rewrite(page: Page, match: re.Match[str]) -> str:
        href = html.unescape(match.group(2))
        parts = urlsplit(href)
        if parts.scheme in ("http", "https", "mailto"):
            return match.group(0)
        if parts.scheme or parts.netloc:
            errors.append(f"{page.source.name}: unsupported link {href!r}")
            return match.group(0)
        if not parts.path:
            target, fragment = page, parts.fragment
        else:
            resolved = (page.source.parent / unquote(parts.path)).resolve()
            target = by_source.get(resolved)
            if target is None:
                errors.append(
                    f"{page.source.name}: link {href!r} leaves the documentation or "
                    "points to a page that is not in docs/site.toml; use an absolute URL "
                    "for files outside it"
                )
                return match.group(0)
            fragment = parts.fragment
        if fragment and fragment not in target.ids:
            errors.append(
                f"{page.source.name}: no anchor #{fragment} in {target.source.name}"
            )
        new = "" if target is page and not fragment else target.url
        if fragment:
            new += "#" + fragment
        return f"{match.group(1)}{html.escape(new or '#')}{match.group(3)}"

    for page in pages:
        page.html = link.sub(lambda m, p=page: rewrite(p, m), page.html)


@dataclass
class Site:
    title: str
    tagline: str
    base_url: str  # with a trailing slash, e.g. "https://example.github.io/project/"
    repo_url: str
    branch: str
    has_image: bool


def head(site: Site, page: Page) -> str:
    title = f"{page.title} - {site.title}"
    if page.url == "index.html" and site.tagline:
        title = f"{site.title}: {site.tagline}"
    canonical = site.base_url + ("" if page.url == "index.html" else page.url)
    description = html.escape(page.description, quote=True)
    meta = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{html.escape(title)}</title>",
        f'<meta name="description" content="{description}">',
        f'<link rel="canonical" href="{canonical}">',
        '<link rel="icon" type="image/svg+xml" href="favicon.svg">',
        '<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{html.escape(site.title)}">',
        f'<meta property="og:title" content="{html.escape(title, quote=True)}">',
        f'<meta property="og:description" content="{description}">',
        f'<meta property="og:url" content="{canonical}">',
    ]
    if site.has_image:
        image = site.base_url + "social-preview.png"
        meta += [
            f'<meta property="og:image" content="{image}">',
            '<meta property="og:image:width" content="1280">',
            '<meta property="og:image:height" content="640">',
            '<meta name="twitter:card" content="summary_large_image">',
            f'<meta name="twitter:image" content="{image}">',
        ]
    meta.append('<link rel="stylesheet" href="style.css">')
    return "".join(meta)


def layout(site: Site, pages: list[Page], index: int) -> str:
    page = pages[index]
    nav: list[str] = []
    current_section = None
    for other in pages:
        if other.section != current_section:
            if current_section is not None:
                nav.append("</ul>")
            current_section = other.section
            nav.append(f"<h2>{html.escape(current_section)}</h2><ul>")
        css = ' class="current"' if other is page else ""
        nav.append(
            f'<li><a{css} href="{other.url}">{html.escape(other.title)}</a></li>'
        )
    nav.append("</ul>")
    pager = []
    if index > 0:
        prev = pages[index - 1]
        pager.append(
            f'<a class="prev" rel="prev" href="{prev.url}"><small>Previous</small>'
            f"&larr; {html.escape(prev.title)}</a>"
        )
    if index + 1 < len(pages):
        nxt = pages[index + 1]
        pager.append(
            f'<a class="next" rel="next" href="{nxt.url}"><small>Next</small>'
            f"{html.escape(nxt.title)} &rarr;</a>"
        )
    edit = html.escape(f"{site.repo_url}/edit/{site.branch}/{page.edit_path}")
    return (
        f'<!doctype html>\n<html lang="en"><head>{head(site, page)}</head>'
        '<body><div class="layout">'
        f'<nav class="side"><a class="brand" href="{pages[0].url}">{html.escape(site.title)}</a>'
        '<div class="search"><input type="search" id="q" placeholder="Search (press /)" '
        'aria-label="Search the documentation" autocomplete="off">'
        '<ul id="results" hidden></ul></div>'
        # The page list is a disclosure: open on wide screens; the inline script
        # closes it on narrow ones before the page is painted, so a phone shows
        # the content first. Without JavaScript it stays open.
        + '<details class="toc" open><summary>All pages</summary>'
        + "".join(nav)
        + "</details><script>if(matchMedia('(max-width:48rem)').matches)"
        "document.currentScript.previousElementSibling.open=false</script>"
        + f'</nav><main>{page.html}<p class="edit"><a href="{edit}">Edit this page on GitHub</a></p>'
        f'<div class="pager">{"".join(pager)}</div></main>'
        '</div><script src="search.js" defer></script></body></html>\n'
    )


def text_of(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


HEADING = re.compile(r"<h[1-3](?:\s[^>]*)?>.*?</h[1-3]>", re.DOTALL)


def search_index(pages: list[Page]) -> list[dict[str, str]]:
    """One entry per heading section of every page: title, link and plain text."""
    entries = []
    for page in pages:
        for part in re.split(r"(?=<h[1-3][\s>])", page.html):
            match = HEADING.match(part)
            heading = text_of(match.group(0)).removesuffix(" #") if match else ""
            found = re.search(r'\sid="([^"]+)"', match.group(0)) if match else None
            body = text_of(part[match.end() :] if match else part)
            if not heading and not body:
                continue
            first = heading == page.title or not heading
            entries.append(
                {
                    "title": page.title if first else f"{page.title}: {heading}",
                    "url": page.url
                    + (f"#{found.group(1)}" if found and not first else ""),
                    "text": body[:8000],
                }
            )
    return entries


SEARCH_JS = """\
(function () {
  var input = document.getElementById("q"), list = document.getElementById("results");
  var entries = null, loading = false, waiting = [];
  function load(done) {
    if (entries) return done();
    waiting.push(done);
    if (loading) return;
    loading = true;
    fetch("search-index.json").then(function (r) { return r.json(); }).then(function (data) {
      entries = data.map(function (e) {
        return { e: e, title: e.title.toLowerCase(), text: e.text.toLowerCase() };
      });
      waiting.forEach(function (fn) { fn(); });
      waiting = [];
    }).catch(function () {
      loading = false;
      waiting = [];
      show([], "Search needs the site to be served over http.");
    });
  }
  function show(items, message) {
    list.textContent = "";
    items.forEach(function (item) {
      var li = document.createElement("li"), a = document.createElement("a");
      var s = document.createElement("small"), at = item.text.indexOf(item.first);
      a.href = item.e.url;
      a.textContent = item.e.title;
      s.textContent = at < 0 ? "" : item.e.text.slice(Math.max(0, at - 30), at + 90);
      a.appendChild(s);
      li.appendChild(a);
      list.appendChild(li);
    });
    if (!items.length && message) {
      var none = document.createElement("li");
      none.className = "none";
      none.textContent = message;
      list.appendChild(none);
    }
    list.hidden = !items.length && !message;
  }
  function search() {
    var terms = input.value.toLowerCase().split(/\\s+/).filter(Boolean);
    if (!terms.length) return show([]);
    var found = [];
    entries.forEach(function (entry) {
      var score = 0;
      for (var i = 0; i < terms.length; i++) {
        var inTitle = entry.title.indexOf(terms[i]) >= 0;
        var inText = entry.text.indexOf(terms[i]) >= 0;
        if (!inTitle && !inText) return;
        score += (inTitle ? 10 : 0) + (inText ? 1 : 0);
      }
      found.push({ e: entry.e, text: entry.text, first: terms[0], score: score });
    });
    found.sort(function (a, b) { return b.score - a.score; });
    show(found.slice(0, 8), "No results.");
  }
  input.addEventListener("input", function () { load(search); });
  input.addEventListener("focus", function () { load(function () {}); });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") { list.hidden = true; input.blur(); }
    if (event.key === "/" && document.activeElement !== input) {
      event.preventDefault();
      input.focus();
    }
  });
  document.addEventListener("click", function (event) {
    if (!list.contains(event.target) && event.target !== input) list.hidden = true;
  });
})();
"""

FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="12" fill="#0b6e4f"/>'
    '<path d="M32 10 14 18v12c0 11 7.5 19 18 24 10.5-5 18-13 18-24V18z" fill="none" '
    'stroke="#fff" stroke-width="4" stroke-linejoin="round"/>'
    '<path d="M24 30h16M24 38h10" stroke="#fff" stroke-width="4" stroke-linecap="round"/>'
    "</svg>\n"
)


def sitemap(site: Site, pages: list[Page]) -> str:
    urls = [site.base_url] + [site.base_url + page.url for page in pages[1:]]
    body = "".join(f"  <url><loc>{html.escape(url)}</loc></url>\n" for url in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + body
        + "</urlset>\n"
    )


def build(out: Path, strict: bool) -> int:
    config = tomllib.loads((DOCS / "site.toml").read_text(encoding="utf-8"))
    site_title, pages = load_pages(config)
    image = DOCS / "assets" / "social-preview.png"
    site = Site(
        title=site_title,
        tagline=config.get("tagline", ""),
        base_url=config.get("base_url", "").rstrip("/") + "/",
        repo_url=config.get("repo_url", "").rstrip("/"),
        branch=config.get("branch", "main"),
        has_image=image.is_file(),
    )
    errors: list[str] = []
    listed = {page.source for page in pages}
    for path in sorted(DOCS.glob("*.md")):
        if path.resolve() not in listed:
            errors.append(f"{path.name} is not in docs/site.toml")
    for key in ("base_url", "repo_url"):
        if not config.get(key):
            errors.append(f"docs/site.toml has no {key}")
    for page in pages:
        if not page.source.is_file():
            errors.append(f"{page.source} does not exist")
        if not page.description.strip():
            errors.append(f"{page.title!r} has no description in docs/site.toml")
        elif len(page.description) > 300:
            errors.append(f"the description of {page.title!r} is over 300 characters")
    if errors:
        return report(errors, strict)
    for page in pages:
        render(page, errors)
    check_and_rewrite_links(pages, errors)
    out.mkdir(parents=True, exist_ok=True)
    pygments_css = HtmlFormatter(style="default", cssclass="hl").get_style_defs(".hl")
    dark = HtmlFormatter(style="github-dark", cssclass="hl").get_style_defs(".hl")
    (out / "style.css").write_text(
        CSS + pygments_css + "\n@media (prefers-color-scheme:dark){\n" + dark + "\n}\n",
        encoding="utf-8",
    )
    for index, page in enumerate(pages):
        (out / page.url).write_text(layout(site, pages, index), encoding="utf-8")
    (out / "search.js").write_text(SEARCH_JS, encoding="utf-8")
    (out / "search-index.json").write_text(
        json.dumps(search_index(pages), separators=(",", ":")), encoding="utf-8"
    )
    (out / "favicon.svg").write_text(FAVICON, encoding="utf-8")
    if site.has_image:
        shutil.copyfile(image, out / "social-preview.png")
    (out / "sitemap.xml").write_text(sitemap(site, pages), encoding="utf-8")
    (out / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\n\nSitemap: {site.base_url}sitemap.xml\n",
        encoding="utf-8",
    )
    print(f"built {len(pages)} pages into {out}")
    return report(errors, strict)


def report(errors: list[str], strict: bool) -> int:
    for message in errors:
        print(("error: " if strict else "warning: ") + message, file=sys.stderr)
    return 1 if errors and strict else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "site")
    args = parser.parse_args()
    return build(args.out, args.strict)


if __name__ == "__main__":
    sys.exit(main())
