"""An MCP server for coding agents (experimental).

    python -m pytest_queryguard.mcp

Speaks the Model Context Protocol over stdio: JSON-RPC 2.0 messages, one per
line, on stdin and stdout. It needs no dependency beyond the plugin. Tools:

- ``run_tests``: run pytest in the server's working directory and return the
  findings of each test, the patch for the relationship-load findings whose
  parent query line is certain, and the end of pytest's output;
- ``explain_finding``: what a finding kind means and how to fix it;
- ``compare``: compare two ``--queryguard-json`` files.

The server only ever starts ``python -m pytest`` with the interpreter it runs
on, in its working directory, without a shell. Every answer is bounded in
size and says when something was cut.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, NamedTuple

import pytest

from . import __version__, sarif
from . import compare as compare_module
from .findings import docs_url

# Newest first; the first one is answered to a client that asks for another.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

MAX_FINDINGS = 30
MAX_MESSAGE = 800
MAX_LABEL = 300
MAX_STACK = 5
MAX_PATCH = 20_000
MAX_OUTPUT = 4_000
MAX_REPORT = 20_000
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 1_800

_OWN_FLAGS = ("--queryguard-json", "--queryguard-write-patch")
_ROOTDIR_ENV = "PYTEST_QUERYGUARD_MCP_ROOTDIR"


class ToolError(Exception):
    """A problem with a tool call, reported to the agent as the tool's result."""


def _clip(text: str, limit: int, *, tail: bool = False) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if tail:
        return "[...cut...]\n" + text[-limit:], True
    return text[:limit] + "\n[...cut...]", True


def _strings(arguments: dict[str, Any], name: str) -> list[str]:
    value = arguments.get(name, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ToolError(f"{name} must be a list of strings")
    return list(value)


def _string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{name} must be a non-empty string")
    return value


def _kill(process: subprocess.Popen[bytes]) -> None:
    """Stop the process and the processes it started (pytest-xdist workers)."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    except OSError:
        pass  # already gone
    if process.poll() is None:
        process.kill()


def _run(
    command: list[str], timeout: float, env: dict[str, str]
) -> tuple[int | None, str]:
    """Exit status (None on timeout) and the end of the combined output."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,  # stdin is the protocol stream
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=os.name == "posix",
        env=env,
    )
    # only the end is kept, so a verbose run cannot fill the memory
    tail: deque[bytes] = deque(maxlen=MAX_OUTPUT // 256 + 2)

    def read() -> None:
        stream = process.stdout
        assert stream is not None
        while chunk := stream.read(256):
            tail.append(chunk)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        status: int | None = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(process)
        process.wait()
        status = None
    # a process that inherited the pipe may keep it open; do not wait for it
    reader.join(timeout=5)
    if not reader.is_alive() and process.stdout is not None:
        process.stdout.close()
    return status, b"".join(list(tail)).decode("utf-8", "replace")


def _finding(nodeid: str, f: Any) -> dict[str, Any] | None:
    if not isinstance(f, dict) or not isinstance(f.get("kind"), str):
        return None
    kind = f["kind"]
    rule = sarif.rule(kind)
    stack = f.get("stack")
    message = f.get("message")
    return {
        "test": nodeid,
        "kind": kind,
        "label": _clip(str(f.get("label", "")), MAX_LABEL)[0],
        "file": f.get("file"),
        "line": f.get("line"),
        "function": f.get("function"),
        "count": f.get("count"),
        "baselined": bool(f.get("baselined")),
        "stack": stack[:MAX_STACK] if isinstance(stack, list) else [],
        "message": _clip(message, MAX_MESSAGE)[0] if isinstance(message, str) else None,
        "fix_hint": rule.fix if rule else None,
        "docs": docs_url(kind),
    }


def _statements(entry: dict[str, Any]) -> int:
    value = entry.get("statements")
    return value if isinstance(value, int) else 0


def run_tests(arguments: dict[str, Any]) -> dict[str, Any]:
    args = _strings(arguments, "args")
    paths = _strings(arguments, "paths")
    if any(p.startswith("-") for p in paths):
        raise ToolError("paths must not start with '-'; put options in args")
    if any(a.startswith(_OWN_FLAGS) for a in args):
        raise ToolError(f"args must not contain {' or '.join(_OWN_FLAGS)}")
    timeout = arguments.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ToolError("timeout must be a number of seconds")
    if not 0 < timeout <= MAX_TIMEOUT:
        raise ToolError(f"timeout must be between 0 and {MAX_TIMEOUT} seconds")
    with tempfile.TemporaryDirectory(prefix="queryguard-mcp-") as tmp:
        out = Path(tmp)
        env = dict(os.environ)
        env[_ROOTDIR_ENV] = str(out / "rootdir")
        # before args, so that a "--" in args cannot turn them into paths
        command = [sys.executable, "-m", "pytest", "-p", "pytest_queryguard.mcp"]
        command += [
            f"{_OWN_FLAGS[0]}={out / 'run.json'}",
            f"{_OWN_FLAGS[1]}={out / 'fix.patch'}",
            *args,
            *paths,
        ]
        status, output = _run(command, float(timeout), env)
        doc = _read(out / "run.json")
        patch = _text(out / "fix.patch")
        rootdir = _text(out / "rootdir") or None
    tests = doc.get("tests") if isinstance(doc, dict) else None
    if not isinstance(tests, dict):
        tests = {}
    findings = []
    for nodeid, entry in tests.items():
        raw = entry.get("findings") if isinstance(entry, dict) else None
        for f in raw if isinstance(raw, list) else []:
            item = _finding(nodeid, f)
            if item is not None:
                findings.append(item)
    patch_text, patch_cut = _clip(patch, MAX_PATCH)
    output_text, _ = _clip(output, MAX_OUTPUT, tail=True)
    return {
        "command": ["pytest", *args, *paths],
        "rootdir": rootdir,
        "exit_code": status,
        "timed_out": status is None,
        "complete": bool(isinstance(doc, dict) and doc.get("complete")),
        "tests": len(tests),
        "statements": sum(
            _statements(e) for e in tests.values() if isinstance(e, dict)
        ),
        "findings_total": len(findings),
        "findings": findings[:MAX_FINDINGS],
        "findings_truncated": len(findings) > MAX_FINDINGS,
        "patch": patch_text or None,
        "patch_truncated": patch_cut,
        "output_tail": output_text,
    }


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def pytest_configure(config: pytest.Config) -> None:
    """With ``-p pytest_queryguard.mcp``: tell run_tests the rootdir."""
    target = os.environ.get(_ROOTDIR_ENV)
    if target and not hasattr(config, "workerinput"):
        Path(target).write_text(str(config.rootpath), encoding="utf-8")


def explain_finding(arguments: dict[str, Any]) -> dict[str, Any]:
    kind = _string(arguments, "kind")
    rule = sarif.rule(kind)
    if rule is None:
        raise ToolError(f"unknown kind {kind!r}; known kinds: {', '.join(sarif.KINDS)}")
    return {
        "kind": kind,
        "summary": rule.short,
        "description": rule.full,
        "fix": rule.fix,
        "docs": docs_url(kind),
    }


def compare(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        base = compare_module.load(Path(_string(arguments, "base_json")))
        head = compare_module.load(Path(_string(arguments, "head_json")))
    except compare_module.CompareError as exc:
        raise ToolError(str(exc)) from None
    result = compare_module.compare(base, head)
    report, cut = _clip(compare_module.markdown(result), MAX_REPORT)
    return {
        "statements_before": result.total_before,
        "statements_after": result.total_after,
        "tests_more": len(result.more),
        "tests_fewer": len(result.fewer),
        "new_findings": len(result.new),
        "fixed_findings": len(result.fixed),
        "complete": result.complete,
        "report": report,
        "report_truncated": cut,
    }


class Tool(NamedTuple):
    run: Callable[[dict[str, Any]], dict[str, Any]]
    description: str
    properties: dict[str, Any]
    required: list[str]


_STRING_LIST = {"type": "array", "items": {"type": "string"}}
TOOLS: dict[str, Tool] = {
    "run_tests": Tool(
        run_tests,
        (
            "Run pytest with pytest-queryguard in the project directory and return "
            "each test's N+1 and query findings (kind, label, file, line, function, "
            "count, message, fix hint, call stack), the statement totals, a patch "
            "for git apply when one could be made, and the end of pytest's output."
        ),
        {
            "args": {
                **_STRING_LIST,
                "description": "pytest options, e.g. ['-k', 'orders', '-x']",
            },
            "paths": {
                **_STRING_LIST,
                "description": "test files, directories or node ids",
            },
            "timeout": {
                "type": "number",
                "description": f"seconds, default {DEFAULT_TIMEOUT}, at most {MAX_TIMEOUT}",
            },
        },
        [],
    ),
    "explain_finding": Tool(
        explain_finding,
        (
            "Explain a pytest-queryguard finding kind (relationship-load, "
            "repeated-statement, column-load, ...): what it means, how to fix it, "
            "and the documentation link."
        ),
        {"kind": {"type": "string", "enum": list(sarif.KINDS)}},
        ["kind"],
    ),
    "compare": Tool(
        compare,
        (
            "Compare two --queryguard-json files (base and head run): tests that "
            "run more or fewer statements and findings that are new or fixed, as "
            "counts and a Markdown report."
        ),
        {
            "base_json": {
                "type": "string",
                "description": "path of the base run's file",
            },
            "head_json": {
                "type": "string",
                "description": "path of the changed run's file",
            },
        },
        ["base_json", "head_json"],
    ),
}


def _tool_list() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }
        for name, (_, description, properties, required) in TOOLS.items()
    ]


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    tool = TOOLS.get(name) if isinstance(name, str) else None
    if tool is None:
        raise _RpcError(-32602, f"Unknown tool: {name}")
    arguments = params.get("arguments") or {}
    try:
        if not isinstance(arguments, dict):
            raise ToolError("arguments must be an object")
        unknown = set(arguments) - set(tool.properties)
        if unknown:
            raise ToolError(f"unknown arguments: {', '.join(sorted(unknown))}")
        result = tool.run(arguments)
    except ToolError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    text = json.dumps(result, indent=1, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def handle(message: Any) -> dict[str, Any] | None:
    """The response to one JSON-RPC message, or None for a notification."""
    if isinstance(message, dict) and "id" not in message:
        return None  # notifications (initialized, cancelled, ...) get no answer
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "Invalid Request")
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    try:
        if not isinstance(method, str):
            raise _RpcError(-32600, "Invalid Request")
        if not isinstance(params, dict):  # MCP's params are always an object
            raise _RpcError(-32602, "Invalid params")
        if method == "initialize":
            asked = params.get("protocolVersion")
            result: dict[str, Any] = {
                "protocolVersion": asked
                if asked in PROTOCOL_VERSIONS
                else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "pytest-queryguard", "version": __version__},
                "instructions": "Run the project's tests with run_tests to find "
                "N+1 queries; apply the returned patch with git apply.",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tool_list()}
        elif method == "tools/call":
            result = _call(params)
        else:
            raise _RpcError(-32601, f"Method not found: {method}")
    except _RpcError as exc:
        return _error(request_id, exc.code, str(exc))
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def serve(stdin: IO[bytes], stdout: IO[bytes]) -> None:
    """Answer the JSON-RPC messages of ``stdin``, one per line, on ``stdout``."""
    for line in stdin:
        if not line.strip():
            continue
        try:
            # a line that is not UTF-8 is one bad message, not the end of the server
            message = json.loads(line.decode("utf-8"))
        except ValueError:  # UnicodeDecodeError included
            response: dict[str, Any] | None = _error(None, -32700, "Parse error")
        else:
            try:
                response = handle(message)
            except Exception as exc:  # noqa: BLE001  # keep serving after a bug
                request_id = message.get("id") if isinstance(message, dict) else None
                response = _error(request_id, -32603, f"Internal error: {exc}")
        if response is not None:
            # ASCII: a lone surrogate ("\ud800" is valid JSON) cannot be
            # encoded as UTF-8, but its escape can be written
            stdout.write(json.dumps(response).encode("ascii") + b"\n")
            stdout.flush()


def main() -> int:
    stdin = open(sys.stdin.fileno(), "rb", closefd=False)  # noqa: SIM115
    stdout = open(sys.stdout.fileno(), "wb", closefd=False)  # noqa: SIM115
    with stdin, stdout:
        serve(stdin, stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
