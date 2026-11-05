"""The development panel: the last requests as JSON and as one HTML page.

Off by default. The page is self-contained (inline CSS and JS, no third-party
code) and renders every value with ``textContent``. It is a development aid,
not something to expose in production.

With a token, ``requests.json`` answers only ``Authorization: Bearer
<token>``. The page holds no data; it reads the token from its URL fragment
(``#token=...``), which the browser never sends, and puts it in that header.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import re
import threading
from collections import deque
from typing import Any

from .. import __version__ as version
from . import _core
from ._core import RequestReport

# Starlette's TestClient reports its client as "testclient".
_LOOPBACK_NAMES = frozenset(("localhost", "testclient"))
# And sends Host "testserver"; accepted only together with that client name,
# which no network peer has, as a search domain could resolve the bare name.
_TEST_CLIENT, _TEST_HOST = "testclient", "testserver"
# RFC 6750 b64token: fits a header and a URL fragment unchanged.
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*")
MIN_TOKEN_LENGTH = 16
UNAUTHORIZED = b"missing or wrong panel token"
CHALLENGE = ("www-authenticate", "Bearer")


class History:
    """The last ``size`` finished reports, newest last; thread-safe."""

    def __init__(self, size: int) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ValueError(f"panel_history must be an integer >= 1, got {size!r}")
        self._items: deque[RequestReport] = deque(maxlen=size)
        self._lock = threading.Lock()

    def add(self, report: RequestReport) -> None:
        with self._lock:
            self._items.append(report)

    def snapshot(self) -> tuple[RequestReport, ...]:
        with self._lock:
            return tuple(self._items)


def normalize_path(path: str) -> str:
    """``"/_queryguard/"`` -> ``"/_queryguard"``.

    ASCII only: WSGI hands the path over as latin-1-decoded bytes, so another
    character would never match there. No empty segment either: ``"//"``
    would normalise to ``""`` and take over every path of the application.
    """
    normalized = path.rstrip("/") if isinstance(path, str) else ""
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or not normalized
        or "//" in normalized
        or not normalized.isascii()
    ):
        raise ValueError(
            "panel_path must start with '/' and name a path of ASCII "
            f"characters, got {path!r}"
        )
    return normalized


def check_token(token: object, panel: bool, loopback_only: bool) -> bytes | None:
    """The panel token as bytes, or None when the panel has none.

    A panel that answers any client must have one: the loopback check only
    guards against accidents, the token is the access control.
    """
    if token is None:
        if panel and not loopback_only:
            raise ValueError(
                "panel_loopback_only=False needs panel_token, a secret of at "
                f"least {MIN_TOKEN_LENGTH} characters, such as "
                "secrets.token_urlsafe(32), read from the environment"
            )
        return None
    if not isinstance(token, str):
        raise TypeError(f"panel_token must be a string, got {type(token).__name__}")
    if len(token) < MIN_TOKEN_LENGTH or not _TOKEN.fullmatch(token):
        # The value is a secret: never in the message.
        raise ValueError(
            f"panel_token must have at least {MIN_TOKEN_LENGTH} characters, "
            "letters, digits and -._~+/ (secrets.token_urlsafe() makes one)"
        )
    return token.encode("ascii")


def authorized(token: bytes | None, header: str | None) -> bool:
    """Whether an ``Authorization`` header carries the panel token."""
    if token is None:
        return True
    if header is None:
        return False
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    # Headers arrive decoded as latin-1, so this gives back the bytes sent.
    sent = parts[1].strip().encode("latin-1", errors="replace")
    return hmac.compare_digest(sent, token)


def route(panel_path: str, path: str) -> str | None:
    """'html', 'json', 'missing' for a path under the panel; None otherwise."""
    if path in (panel_path, panel_path + "/"):
        return "html"
    if path == panel_path + "/requests.json":
        return "json"
    if path.startswith(panel_path + "/"):
        return "missing"
    return None


def is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)  # ::ffff:127.0.0.1
    return bool(address.is_loopback or (mapped is not None and mapped.is_loopback))


def host_name(host_header: str) -> str:
    """The name in a Host header: ``"[::1]:8000"`` -> ``"::1"``."""
    host = host_header.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def is_loopback_host(host_header: str) -> bool:
    """A Host header that names this machine: an address, localhost, *.localhost."""
    name = host_name(host_header)
    if name == "localhost" or name.endswith(".localhost"):
        return True
    # An address; not the "testclient" name, which is a client's, not a host's.
    return name not in _LOOPBACK_NAMES and is_loopback(name)


def allowed(
    client: str | None, loopback_only: bool, host_header: str | None = None
) -> bool:
    """Whether the panel answers this client.

    None (no address: a Unix socket peer, often a reverse proxy relaying any
    client) is refused while ``loopback_only`` is on. So is a loopback client
    whose Host header names another site: a page on that site whose name was
    rebound to 127.0.0.1 (DNS rebinding) could otherwise read the panel. A
    request without a Host header is not from a browser and is answered.
    """
    if not loopback_only:
        return True
    if client is None or not is_loopback(client):
        return False
    if host_header is None:
        return True
    if client == _TEST_CLIENT and host_name(host_header) == _TEST_HOST:
        return True
    return is_loopback_host(host_header)


def json_body(history: History) -> bytes:
    from ._listeners import _watched

    error = _core.internal_error()
    payload: dict[str, Any] = {
        "version": version,
        "watched_engines": len(_watched),
        "internal_error": f"{type(error).__name__}: {error}" if error else None,
        "requests": [r.to_dict() for r in reversed(history.snapshot())],
    }
    return json.dumps(payload, default=str).encode("utf-8")


def html_body(json_url: str) -> bytes:
    # The URL goes into an attribute, never into the script: the script and
    # the style stay byte for byte the same, so the CSP can list their hashes.
    page = _PAGE.replace("__JSON_URL__", html.escape(json_url, quote=True))
    return page.replace("__VERSION__", html.escape(version)).encode("utf-8")


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>queryguard panel</title>
<style>
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--card:#fff;
--bad:#b3261e;--badbg:#fbe9e7;--ok:#2e6b34;--code:#f3f2ee;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#a09f99;
--line:#2f2f2c;--card:#1e1e1c;--bad:#ff8a80;--badbg:#3a1d1a;--ok:#8fd694;--code:#262624;
color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px}
main{max-width:1100px;margin:0 auto}
header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline}
h1{font-size:18px;margin:0}
.note{color:var(--bad);font-weight:600}
.muted{color:var(--muted)}
.bar{display:flex;gap:12px;align-items:center;margin:12px 0}
button{font:inherit;padding:4px 10px;border:1px solid var(--line);
background:var(--card);color:var(--fg);border-radius:6px;cursor:pointer}
.wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;background:var(--card)}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);
white-space:nowrap}
th{font-weight:600;color:var(--muted)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
tr.row{cursor:pointer}
tr.row:hover td{background:var(--code)}
.badge{display:inline-block;padding:0 6px;border-radius:9px;font-size:12px;
background:var(--badbg);color:var(--bad);font-weight:600}
.ok{color:var(--ok)}
td.detail{white-space:normal;background:var(--bg)}
pre{background:var(--code);padding:8px;border-radius:6px;overflow-x:auto;
margin:6px 0;white-space:pre-wrap;word-break:break-word}
ol{padding-left:24px;margin:6px 0}
li{margin:4px 0}
code{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
</style></head>
<body><main id="panel" data-json-url="__JSON_URL__">
<header><h1>queryguard panel</h1>
<span class="note">Development only: never enable this in production.</span>
<span class="muted">pytest-queryguard __VERSION__</span></header>
<div class="bar"><button id="refresh" type="button">Refresh</button>
<label><input id="auto" type="checkbox"> refresh every 2 s</label>
<span id="status" class="muted" role="status"></span></div>
<div class="wrap"><table><thead><tr><th>Time</th><th>Request</th><th>Status</th>
<th>Queries</th><th>DB time</th><th>Total</th><th>Findings</th></tr></thead>
<tbody id="rows"></tbody></table></div>
</main>
<script>
"use strict";
const url = document.getElementById("panel").dataset.jsonUrl;
const expanded = new Set();
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}
function ms(s) {
  if (s === null || s === undefined) return "";
  return s < 0.001 ? (s * 1e6).toFixed(0) + " us" : (s * 1000).toFixed(1) + " ms";
}
function where(x) { return x.file ? " (" + x.file + ":" + x.line + " in " + x.function + ")" : ""; }
function detail(r) {
  const td = el("td", null, "detail"); td.colSpan = 7;
  if (r.error) td.append(el("p", "Incomplete: " + r.error, "note"));
  if (r.over_budget) td.append(el("p", "Over budget: " + r.statement_count + " statements, budget " + r.budget, "note"));
  for (const f of r.findings) td.append(el("pre", f.message));
  td.append(el("strong", "Statements" + (r.truncated ? " (first " + r.statements.length + " of " + r.statement_count + ")" : "")));
  const ol = el("ol");
  for (const q of r.statements) {
    const li = el("li");
    li.append(el("code", q.sql));
    li.append(el("div", ms(q.duration) + (q.relationship ? "  loads " + q.relationship : "") + where(q), "muted"));
    ol.append(li);
  }
  td.append(ol);
  return td;
}
function render(data) {
  const body = document.getElementById("rows");
  body.replaceChildren();
  for (const r of data.requests) {
    const tr = el("tr", null, "row");
    tr.tabIndex = 0;
    tr.append(el("td", new Date(r.started * 1000).toLocaleTimeString()));
    const name = el("td"); name.append(el("span", r.name));
    if (r.route && r.route !== r.path) name.append(el("span", "  " + r.route, "muted"));
    tr.append(name);
    tr.append(el("td", r.status));
    tr.append(el("td", r.statement_count + (r.budget !== null ? " / " + r.budget : ""), "num"));
    tr.append(el("td", ms(r.db_time), "num"));
    tr.append(el("td", ms(r.duration), "num"));
    const f = el("td");
    if (r.findings.length || r.over_budget) {
      const n = r.findings.length + (r.over_budget ? 1 : 0);
      f.append(el("span", n + (n === 1 ? " problem" : " problems"), "badge"));
    } else f.append(el("span", "none", "ok"));
    tr.append(f);
    const toggle = () => { expanded.has(r.id) ? expanded.delete(r.id) : expanded.add(r.id); render(data); };
    tr.addEventListener("click", toggle);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") toggle(); });
    body.append(tr);
    if (expanded.has(r.id)) { const d = el("tr"); d.append(detail(r)); body.append(d); }
  }
  const status = data.requests.length + " requests, " + data.watched_engines + " watched engines";
  document.getElementById("status").textContent =
    status + (data.internal_error ? "; switched off: " + data.internal_error : "");
}
function token() {
  // From #token=..., never from the query string: a fragment is not sent.
  const m = location.hash.slice(1).match(/(?:^|&)token=([^&]*)/);
  if (!m) return null;
  try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
}
let latest = 0;
async function load() {
  const status = document.getElementById("status");
  const n = ++latest;  // a slower, older answer must not replace a newer one
  try {
    const t = token();
    const headers = t ? {"Authorization": "Bearer " + t} : {};
    const res = await fetch(url, {cache: "no-store", headers: headers});
    if (n !== latest) return;
    if (res.status === 401) {
      stopAuto();  // the same token would fail every 2 s
      document.getElementById("rows").replaceChildren();
      status.textContent = t
        ? "The token in the address is wrong."
        : "This panel needs its token: open it as " + location.pathname + "#token=<token>.";
      return;
    }
    if (!res.ok) { status.textContent = "Could not load " + url + ": HTTP " + res.status; return; }
    const data = await res.json();
    if (n === latest) render(data);
  } catch (e) {
    if (n === latest) status.textContent = "Could not load " + url + ": " + e;
  }
}
let timer = null;
function stopAuto() {
  clearInterval(timer); timer = null;
  document.getElementById("auto").checked = false;
}
document.getElementById("refresh").addEventListener("click", load);
window.addEventListener("hashchange", load);  // a token pasted into the address
document.getElementById("auto").addEventListener("change", (e) => {
  clearInterval(timer); timer = e.target.checked ? setInterval(load, 2000) : null;
});
load();
</script></body></html>
"""


def _inline_hash(tag: str) -> str:
    """The CSP source of the one inline ``<tag>`` element of the page."""
    start = _PAGE.index(f"<{tag}>") + len(f"<{tag}>")
    body = _PAGE[start : _PAGE.index(f"</{tag}>", start)]
    if "__" in body:  # a placeholder would change the hash per request
        raise AssertionError(f"the panel's <{tag}> must not hold a placeholder")
    digest = hashlib.sha256(body.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


# No 'unsafe-inline': the page runs only its own script and style.
CONTENT_SECURITY_POLICY = (
    f"default-src 'none'; style-src {_inline_hash('style')}; "
    f"script-src {_inline_hash('script')}; connect-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
HEADERS = (
    ("cache-control", "no-store"),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("content-security-policy", CONTENT_SECURITY_POLICY),
)
