"""The page report: a request's findings added to its own HTML response.

Off by default (``Settings.page_report``). The middleware changes only a
complete ``text/html`` response that it can rewrite safely: a 2xx answer to a
method other than HEAD, with a ``Content-Length`` of at most ``MAX_BODY``
bytes, without ``Content-Encoding`` or ``Content-Range``, in a charset where
``</body>`` is ASCII. Anything else passes through untouched. Every value is
escaped: findings hold SQL text and labels from the application.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable, Sequence

from ._core import RequestReport

FOOTER = "footer"
CONSOLE = "console"
# The most bytes held back to add the report; a longer page passes through.
MAX_BODY = 4 * 1024 * 1024

_BODY_END = re.compile(rb"</body\s*>", re.IGNORECASE)
# A policy can also come from a <meta http-equiv> element of the page.
_CSP_META = re.compile(
    rb"http-equiv\s*=\s*[\"']?\s*content-security-policy", re.IGNORECASE
)
_CSP_HEADERS = frozenset(
    ("content-security-policy", "content-security-policy-report-only")
)
_NO_BODY = frozenset((204, 205, 206))

_ASIDE_STYLE = (
    "display:block;clear:both;margin:16px 0 0;padding:8px 12px;"
    "border:2px solid #b3261e;background:#fff;color:#1d1d1b;text-align:left;"
    "font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
)
_PRE_STYLE = "margin:8px 0 0;white-space:pre-wrap;word-break:break-word;font:inherit"


def eligible(
    status: int | None, headers: Iterable[tuple[str, str]], method: str | None
) -> int | None:
    """The declared body length when the response can get a page report, else None."""
    if method == "HEAD" or status is None or not 200 <= status < 300:
        return None
    if status in _NO_BODY:
        return None
    content_type: str | None = None
    length: str | None = None
    for raw_name, value in headers:
        name = raw_name.lower()
        if name == "content-type":
            content_type = value
        elif name == "content-length":
            if length is not None and length.strip() != value.strip():
                return None  # conflicting lengths: not ours to fix
            length = value
        elif name == "content-encoding":
            if value.strip().lower() not in ("", "identity"):
                return None  # gzip, br, ...: the bytes are not HTML
        elif name in ("content-range", "transfer-encoding"):
            return None
    if content_type is None or length is None:
        return None  # no Content-Length: a streamed body of unknown length
    media, _, params = content_type.partition(";")
    if media.strip().lower() != "text/html" or not _ascii_charset(params):
        return None
    length = length.strip()
    if not length.isdigit() or int(length) > MAX_BODY:
        return None
    return int(length)


def _ascii_charset(params: str) -> bool:
    """Whether the charset (default: any ASCII-compatible one) writes ASCII as is."""
    for param in params.split(";"):
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset":
            charset = value.strip().strip("\"'")
            try:
                return "</body>".encode(charset) == b"</body>"
            except (LookupError, ValueError):
                return False
    return True


def has_csp(headers: Iterable[tuple[str, str]]) -> bool:
    return any(name.lower() in _CSP_HEADERS for name, _ in headers)


def inject(
    body: bytes, report: RequestReport, mode: str, headers: Sequence[tuple[str, str]]
) -> bytes:
    """``body`` with the report before its last ``</body>``, or at its end.

    Console mode adds an inline script, which a Content-Security-Policy
    without ``'unsafe-inline'`` would block (and report). The middleware does
    not edit the application's policy: with a policy in a header or a
    ``<meta>`` element, the report is a footer without ``style`` attributes.
    """
    csp = has_csp(headers) or _CSP_META.search(body) is not None
    if mode == CONSOLE and not csp:
        snippet = console(report)
    else:
        snippet = footer(report, styled=not csp)
    ends = [match.start() for match in _BODY_END.finditer(body)]
    if not ends:
        return body + snippet
    return body[: ends[-1]] + snippet + body[ends[-1] :]


def summary(report: RequestReport) -> str:
    problems = len(report.findings) + (1 if report.over_budget else 0)
    text = f"queryguard: {problems} problem{'s' if problems != 1 else ''} in "
    text += f"{report.name}: {report.statement_count} statements"
    if report.budget is not None:
        text += f", budget {report.budget}"
    return text


def footer(report: RequestReport, *, styled: bool) -> bytes:
    """An ``<aside>`` with the summary, and the full report in ``<details>``."""
    aside = f' style="{_ASIDE_STYLE}"' if styled else ""
    pre = f' style="{_PRE_STYLE}"' if styled else ""
    text = (
        f'\n<aside id="queryguard-page-report"{aside}><details>'
        f"<summary>{html.escape(summary(report))}</summary>"
        f"<pre{pre}>{html.escape(report.message)}</pre></details></aside>\n"
    )
    # Character references keep the bytes ASCII whatever the page's charset.
    return text.encode("ascii", "xmlcharrefreplace")


def console(report: RequestReport) -> bytes:
    """A ``<script>`` that writes the report to the browser console."""
    data = {
        "request": report.name,
        "route": report.route,
        "statement_count": report.statement_count,
        "db_time": report.db_time,
        "budget": report.budget,
        "over_budget": report.over_budget,
        "findings": [
            {
                "kind": f.kind,
                "label": f.label,
                "file": f.file,
                "line": f.line,
                "function": f.function,
                "count": f.count,
            }
            for f in report.findings
        ],
    }
    args = f"{_script_json(report.message)}, {_script_json(data)}"
    return f"\n<script>console.warn({args});</script>\n".encode("ascii")


def _script_json(value: object) -> str:
    """JSON that cannot end or confuse the ``<script>`` element it is in.

    ``<``, ``>`` and ``&`` occur only inside JSON strings, where their
    ``\\u`` escapes mean the same; ``ensure_ascii`` escapes the rest,
    U+2028 and U+2029 included.
    """
    text = json.dumps(value, default=str)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def with_length(
    headers: Sequence[tuple[str, str]], length: int
) -> list[tuple[str, str]]:
    """The headers with the new ``Content-Length`` and without ``ETag``.

    The validator described the original bytes, so a changed page drops it.
    """
    fixed: list[tuple[str, str]] = []
    for name, value in headers:
        lower = name.lower()
        if lower == "etag":
            continue
        fixed.append((name, str(length) if lower == "content-length" else value))
    return fixed
