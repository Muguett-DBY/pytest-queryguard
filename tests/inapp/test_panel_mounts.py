"""The panel inside mounted ASGI applications."""

from __future__ import annotations

import html
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.routing import Mount

from pytest_queryguard.app import QueryGuardASGI
from pytest_queryguard.app._asgi import Receive, Scope, Send


def _mounted(middleware: QueryGuardASGI, mount_path: str = "/api") -> Starlette:
    child = Starlette(routes=[Mount(mount_path, app=middleware)])
    return Starlette(routes=[Mount("/nested", app=child)])


def _json_url(page: str) -> str:
    match = re.search(r'data-json-url="([^"]*)"', page)
    assert match is not None
    return html.unescape(match[1])


@pytest.mark.parametrize("panel_path", ["/_queryguard", "/debug/qg/"])
@pytest.mark.parametrize(
    ("server_root", "mount_path", "encoded_mount"),
    [
        ("", "/api", "/api"),
        ("/proxy", "/api", "/api"),
        ("", "/a&b", "/a%26b"),
        ("", "/a?b", "/a%3Fb"),
        ("", "/a#b", "/a%23b"),
        ("", "/café", "/caf%C3%A9"),
        ("", "/a%b", "/a%25b"),
    ],
)
def test_mounted_panel_fetches_its_own_history(
    watched: Engine,
    panel_path: str,
    server_root: str,
    mount_path: str,
    encoded_mount: str,
) -> None:
    inner = FastAPI()

    @inner.get("/query")
    def query() -> dict[str, bool]:
        with Session(watched) as session:
            session.execute(text("SELECT 1"))
        return {"ok": True}

    middleware = QueryGuardASGI(inner, panel=True, panel_path=panel_path, report=[])
    prefix = f"{server_root}/nested{encoded_mount}"
    panel_url = prefix + panel_path.rstrip("/")
    with TestClient(_mounted(middleware, mount_path), root_path=server_root) as client:
        assert client.get(prefix + "/query").json() == {"ok": True}
        page = client.get(panel_url + "/")
        assert page.status_code == 200
        json_url = _json_url(page.text)
        assert json_url == panel_url + "/requests.json"
        response = client.get(json_url)
        assert response.status_code == 200
        reports = response.json()["requests"]
        assert len(reports) == 1
        assert reports[0]["statement_count"] == 1
        assert reports[0]["path"] == f"{server_root}/nested{mount_path}/query"
        assert client.head(json_url).content == b""
        assert client.head(json_url).status_code == 200
        assert client.post(json_url).status_code == 405
        assert client.get(panel_url + "/missing").status_code == 404
    assert len(middleware.reports) == 1  # panel requests are not recorded


@pytest.mark.parametrize("peer", [("203.0.113.9", 4000), None])
@pytest.mark.parametrize("resource", ["/", "/requests.json"])
def test_mounted_panel_still_refuses_non_loopback_or_missing_peers(
    peer: tuple[str, int] | None, resource: str
) -> None:
    middleware = QueryGuardASGI(FastAPI(), panel=True, report=[])
    with TestClient(_mounted(middleware), client=peer) as client:
        response = client.get("/nested/api/_queryguard" + resource)
    assert response.status_code == 403
    assert middleware.reports == ()


@pytest.mark.parametrize(
    ("root", "path"),
    [
        ("/_queryguard", "/_queryguard"),
        ("/api", "/apix/_queryguard"),
        ("/api", "/api/ordinary"),
    ],
)
def test_panel_does_not_take_over_the_application_root_or_change_its_scope(
    root: str, path: str
) -> None:
    inner = FastAPI()
    received: list[tuple[str, str]] = []

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            received.append((scope["path"], scope.get("root_path", "")))
        await inner(scope, receive, send)

    middleware = QueryGuardASGI(application, panel=True, report=[])
    with TestClient(middleware, root_path=root) as client:
        assert client.get(path).status_code == 404
    assert received == [(path, root)]


def test_already_relative_scope_can_follow_the_panel_json_url() -> None:
    middleware = QueryGuardASGI(
        FastAPI(), panel=True, panel_path="/debug/qg", report=[]
    )
    with TestClient(middleware, root_path="/api") as client:
        page = client.get("/debug/qg")
        assert page.status_code == 200
        assert client.get(_json_url(page.text)).status_code == 200
    assert middleware.reports == ()


@pytest.mark.parametrize(("root", "prefix"), [("/", ""), ("/api/", "/api")])
def test_a_root_path_ending_in_a_slash_gives_a_same_host_json_url(
    root: str, prefix: str
) -> None:
    middleware = QueryGuardASGI(FastAPI(), panel=True, report=[])
    with TestClient(middleware, root_path=root) as client:
        page = client.get(prefix + "/_queryguard")
        assert page.status_code == 200
        json_url = _json_url(page.text)
        assert json_url == prefix + "/_queryguard/requests.json"
        assert client.get(json_url).status_code == 200
    assert middleware.reports == ()
