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
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, NamedTuple

from . import __version__
from . import compare as compare_module
from .findings import docs_url
from .sarif import _RULES

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


def _run(command: list[str], timeout: float) -> tuple[int | None, str]:
    """Exit status (None on timeout) and the combined output of a command."""
    posix = os.name == "posix"
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,  # stdin is the protocol stream
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=posix,  # so a timeout stops pytest-xdist workers too
        # SARIF paths relative to the rootdir, as in --queryguard-json
        env={k: v for k, v in os.environ.items() if k != "GITHUB_WORKSPACE"},
    )
    try:
        out, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if posix:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        out, _ = process.communicate()
        return None, out.decode("utf-8", "replace")
    return process.returncode, out.decode("utf-8", "replace")


def _sarif_messages(path: Path) -> dict[tuple[str, str, str | None, str | None], str]:
    """(kind, label, file, function) -> the finding's message, from the SARIF file."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        results = doc["runs"][0]["results"]
    except (OSError, ValueError, LookupError, TypeError):
        return {}
    messages = {}
    for result in results:
        try:
            kind = result["ruleId"].removeprefix("queryguard/")
            location = result["locations"][0]
            file = location["physicalLocation"]["artifactLocation"]["uri"]
            logical = location.get("logicalLocations") or [{}]
            key = (kind, result["properties"]["label"], file, logical[0].get("name"))
            messages[key] = result["message"]["text"]
        except (LookupError, TypeError, AttributeError):
            continue
    return messages


def run_tests(arguments: dict[str, Any]) -> dict[str, Any]:
    args = _strings(arguments, "args")
    paths = _strings(arguments, "paths")
    if any(p.startswith("-") for p in paths):
        raise ToolError("paths must not start with '-'; put options in args")
    timeout = arguments.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ToolError("timeout must be a number of seconds")
    if not 0 < timeout <= MAX_TIMEOUT:
        raise ToolError(f"timeout must be between 0 and {MAX_TIMEOUT} seconds")
    with tempfile.TemporaryDirectory(prefix="queryguard-mcp-") as tmp:
        out = Path(tmp)
        command = [sys.executable, "-m", "pytest", *args, *paths]
        # last, so that they win over the same flags in args
        command += [
            f"--queryguard-json={out / 'run.json'}",
            f"--queryguard-write-patch={out / 'fix.patch'}",
            f"--queryguard-sarif={out / 'run.sarif'}",
        ]
        status, output = _run(command, float(timeout))
        try:
            doc = json.loads((out / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        try:
            patch = (out / "fix.patch").read_text(encoding="utf-8")
        except OSError:
            patch = ""
        messages = _sarif_messages(out / "run.sarif")
    tests = doc.get("tests") if isinstance(doc.get("tests"), dict) else {}
    findings = []
    for nodeid, entry in tests.items():
        for f in entry.get("findings", []):
            key = (f["kind"], f["label"], f.get("file"), f.get("function"))
            rule = _RULES.get(f["kind"])
            item = {
                "test": nodeid,
                "kind": f["kind"],
                "label": _clip(f["label"], MAX_LABEL)[0],
                "file": f.get("file"),
                "line": f.get("line"),
                "function": f.get("function"),
                "count": f.get("count"),
                "baselined": f.get("baselined", False),
                "stack": f.get("stack", [])[:MAX_STACK],
                "message": _clip(messages.get(key, ""), MAX_MESSAGE)[0] or None,
                "fix_hint": rule.fix if rule else None,
                "docs": docs_url(f["kind"]),
            }
            findings.append(item)
    patch_text, patch_cut = _clip(patch, MAX_PATCH)
    output_text, _ = _clip(output, MAX_OUTPUT, tail=True)
    return {
        "command": command[1:-3],
        "exit_code": status,
        "timed_out": status is None,
        "complete": bool(doc.get("complete")),
        "tests": len(tests),
        "statements": sum(int(e.get("statements", 0)) for e in tests.values()),
        "findings_total": len(findings),
        "findings": findings[:MAX_FINDINGS],
        "findings_truncated": len(findings) > MAX_FINDINGS,
        "patch": patch_text or None,
        "patch_truncated": patch_cut,
        "output_tail": output_text,
    }


def explain_finding(arguments: dict[str, Any]) -> dict[str, Any]:
    kind = _string(arguments, "kind")
    rule = _RULES.get(kind)
    if rule is None:
        raise ToolError(f"unknown kind {kind!r}; known kinds: {', '.join(_RULES)}")
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
        {"kind": {"type": "string", "enum": list(_RULES)}},
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
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "Invalid Request")
    request_id = message.get("id")
    method = message.get("method")
    if "id" not in message:
        return None  # notifications: initialized, cancelled, ...
    params = message.get("params") or {}
    try:
        if not isinstance(method, str) or not isinstance(params, dict):
            raise _RpcError(-32600, "Invalid Request")
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


def serve(stdin: IO[str], stdout: IO[str]) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response: dict[str, Any] | None = _error(None, -32700, "Parse error")
        else:
            try:
                response = handle(message)
            except Exception as exc:  # noqa: BLE001  # keep serving after a bug
                request_id = message.get("id") if isinstance(message, dict) else None
                response = _error(request_id, -32603, f"Internal error: {exc}")
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> int:
    stdin = open(sys.stdin.fileno(), encoding="utf-8", closefd=False)  # noqa: SIM115
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)  # noqa: SIM115
    with stdin, stdout:
        serve(stdin, stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
