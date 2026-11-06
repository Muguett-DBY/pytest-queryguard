"""The MCP server, driven over stdin and stdout like an agent drives it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pytest_queryguard import mcp

# pytester sessions in this file run in subprocesses; nothing to measure here.
pytestmark = pytest.mark.allow_nplusone

APP = """\
from sqlalchemy import ForeignKey, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customer"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    customer: Mapped[Customer] = relationship()


def names(session: Session) -> list[str]:
    orders = session.scalars(select(Order)).all()
    return [order.customer.name for order in orders]
"""

TEST = """\
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import Base, Customer, Order, names


def test_names():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for i in range(3):
            session.add(Order(customer=Customer(name=f"c{i}")))
        session.commit()
        assert len(names(session)) == 3
"""


class Server:
    def __init__(self, cwd: Path) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "pytest_queryguard.mcp"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.next_id = 0

    def send(self, message: dict[str, Any] | str) -> None:
        assert self.process.stdin is not None
        line = message if isinstance(message, str) else json.dumps(message)
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def receive(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        line = self.process.stdout.readline()
        assert line, "the server closed stdout"
        response: dict[str, Any] = json.loads(line)
        return response

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self.next_id}
        message["method"] = method
        if params is not None:
            message["params"] = params
        self.send(message)
        response = self.receive()
        assert response["id"] == self.next_id
        return response

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        result: dict[str, Any] = response["result"]
        return result

    def close(self) -> int:
        assert self.process.stdin is not None
        self.process.stdin.close()
        return self.process.wait(timeout=30)


@pytest.fixture
def project(pytester: pytest.Pytester) -> Path:
    pytester.makeini("[pytest]\n")
    pytester.makepyfile(app=APP, test_app=TEST)
    return pytester.path


@pytest.fixture
def server(project: Path) -> Any:
    server = Server(project)
    init = server.request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )
    assert init["result"]["protocolVersion"] == "2025-06-18"
    server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    yield server
    if server.process.poll() is None:
        server.close()


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    assert result["isError"] is False, result
    payload: dict[str, Any] = json.loads(result["content"][0]["text"])
    return payload


def test_initialize_and_list_tools(server: Server) -> None:
    tools = server.request("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["run_tests", "explain_finding", "compare"]
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
    assert server.request("ping")["result"] == {}
    assert server.close() == 0


def test_an_unknown_protocol_version_gets_the_newest(project: Path) -> None:
    server = Server(project)
    init = server.request("initialize", {"protocolVersion": "1999-01-01"})
    assert init["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]
    assert init["result"]["serverInfo"]["name"] == "pytest-queryguard"
    server.close()


def test_run_tests_returns_the_finding_and_the_patch(server: Server) -> None:
    result = _payload(server.call("run_tests", {"paths": ["test_app.py"]}))
    assert result["exit_code"] == 1
    assert result["timed_out"] is False
    assert result["complete"] is True
    assert result["tests"] == 1
    [finding] = result["findings"]
    assert finding["test"] == "test_app.py::test_names"
    assert finding["kind"] == "relationship-load"
    assert finding["label"] == "Order.customer"
    assert finding["file"] == "app.py"
    assert finding["function"] == "names"
    assert finding["count"] == 3
    assert "joinedload" in finding["fix_hint"]
    assert "Order.customer was lazy-loaded 3 times" in finding["message"]
    assert finding["docs"].endswith("#relationship-load")
    assert finding["stack"][0]["function"] == "test_names"
    assert "+++ b/app.py" in result["patch"]
    assert ".options(joinedload(Order.customer))" in result["patch"]
    assert result["patch_truncated"] is False
    assert "1 failed" in result["output_tail"]


def test_run_tests_passes_args(server: Server) -> None:
    result = _payload(server.call("run_tests", {"args": ["-k", "nothing_matches"]}))
    assert result["exit_code"] == 5  # no tests collected
    assert result["findings"] == []
    assert result["patch"] is None


def test_run_tests_stops_at_the_timeout(server: Server, project: Path) -> None:
    (project / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(60)\n"
    )
    result = _payload(
        server.call("run_tests", {"paths": ["test_slow.py"], "timeout": 2})
    )
    assert result["timed_out"] is True
    assert result["exit_code"] is None
    assert result["findings"] == []


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"args": "-x"}, "args must be a list of strings"),
        ({"args": [1]}, "args must be a list of strings"),
        ({"paths": ["--rootdir=/"]}, "paths must not start with '-'"),
        ({"timeout": 0}, "timeout must be between"),
        ({"timeout": "5"}, "timeout must be a number"),
        ({"shell": True}, "unknown arguments: shell"),
    ],
)
def test_run_tests_rejects_bad_arguments(
    server: Server, arguments: dict[str, Any], error: str
) -> None:
    result = server.call("run_tests", arguments)
    assert result["isError"] is True
    assert error in result["content"][0]["text"]


def test_explain_finding(server: Server) -> None:
    result = _payload(server.call("explain_finding", {"kind": "repeated-statement"}))
    assert "IN (...)" in result["fix"]
    assert result["docs"].endswith("#repeated-statement")
    bad = server.call("explain_finding", {"kind": "nope"})
    assert bad["isError"] is True
    assert "relationship-load" in bad["content"][0]["text"]


def test_compare(server: Server, project: Path) -> None:
    def write(name: str, statements: int, findings: list[dict[str, Any]]) -> str:
        doc = {
            "format": 1,
            "complete": True,
            "tests": {"t::a": {"statements": statements, "findings": findings}},
        }
        (project / name).write_text(json.dumps(doc))
        return name

    finding = {"kind": "relationship-load", "label": "A.b", "count": 4}
    finding |= {"file": "a.py", "function": "f"}
    result = _payload(
        server.call(
            "compare",
            {
                "base_json": write("base.json", 3, []),
                "head_json": write("head.json", 7, [finding]),
            },
        )
    )
    assert result["statements_before"] == 3
    assert result["statements_after"] == 7
    assert result["new_findings"] == 1
    assert "New findings" in result["report"]
    missing = server.call(
        "compare", {"base_json": "nope.json", "head_json": "head.json"}
    )
    assert missing["isError"] is True
    assert "cannot read nope.json" in missing["content"][0]["text"]


def test_protocol_errors(server: Server) -> None:
    assert server.request("tools/call", {"name": "rm"})["error"]["code"] == -32602
    assert server.request("resources/list")["error"]["code"] == -32601
    server.send("{not json")
    assert server.receive()["error"]["code"] == -32700
    server.send(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]))
    assert server.receive()["error"]["code"] == -32600
    # still serving
    assert server.request("ping")["result"] == {}


def test_tool_list_fits_the_schema() -> None:
    for tool in mcp._tool_list():
        schema = tool["inputSchema"]
        assert set(schema["required"]) <= set(schema["properties"])
        assert tool["description"].strip()
