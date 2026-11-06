# MCP server for coding agents (experimental)

Since 0.8.0.

```console
python -m pytest_queryguard.mcp
```

starts a [Model Context Protocol](https://modelcontextprotocol.io/) server
over stdio, so a coding agent such as Claude Code or Cursor can run your tests
with pytest-queryguard, read the findings as data and apply the suggested
patch. It is part of the package and needs no extra dependency: the server
speaks JSON-RPC 2.0, one message per line, with the standard library only.

It is experimental (see [Stability](stability.md#experimental)): the tools,
their arguments and the shape of their results may change in a minor release.

## Setup

Start the server with the Python interpreter of the project, the one whose
`python -m pytest` runs your tests, from the project directory: the tests run
in the server's working directory with that interpreter.

Claude Code, from the project directory:

```console
claude mcp add queryguard -- python -m pytest_queryguard.mcp
```

With uv, `claude mcp add queryguard -- uv run python -m pytest_queryguard.mcp`.

Cursor, in `.cursor/mcp.json` of the project:

```json
{
"mcpServers": {
    "queryguard": {
      "command": "python",
      "args": ["-m", "pytest_queryguard.mcp"]
    }
}
}
```

Use the full path of the virtual environment's `python` when the agent does
not start it with the environment activated.

## Tools

### run_tests

`run_tests(args=[], paths=[], timeout=300)` runs `python -m pytest <args> <paths>`
in the server's working directory, with `--queryguard-json` and
`--queryguard-write-patch` pointing at temporary files (so `args` must not
contain them). It returns pytest's rootdir, the exit status, whether the run timed
out or stopped early, the number of tests and statements, and every finding
with its test, `kind`, `label`, `file`, `line`, `function`, `count`,
`baselined`, the first frames of its call stack, its message, a fix hint
for its kind and its documentation link. When some `relationship-load`
finding has a certain parent query line, `patch` holds the unified diff
that adds the loader option (see
[A patch for the parent query](when-a-test-fails.md#a-patch-for-the-parent-query));
apply it with `git apply` from the returned `rootdir`; the files of the
findings are relative to it too. `output_tail` is the end of
pytest's output. `args` and `paths` are lists of strings; `paths` must not
start with `-`. The timeout is at most 1800 seconds; on a timeout pytest is
stopped with the processes it started (its process group on Linux and macOS,
`taskkill /T` on Windows), such as pytest-xdist workers.

### explain_finding

`explain_finding(kind)` returns what a finding kind means, how to fix it and the link to its section of
this documentation.

### compare

`compare(base_json, head_json)` compares two `--queryguard-json` files, as
`python -m pytest_queryguard.compare` does (see
[CI](ci.md#query-counts-as-json)): the counts of tests that run more or
fewer statements and of new and fixed findings, and the Markdown report.

## Limits

Results are bounded so that one call cannot flood the agent's context: at most
30 findings (`findings_total` gives the number of all of them and
`findings_truncated` says when some were left out), messages of 800
characters, labels of 300, 5 stack frames, a patch of 20,000 characters
(`patch_truncated`), the last 4,000 characters of pytest's output and a
compare report of 20,000 characters.

The process keeps only the end of pytest's output in memory, so a verbose
run does not grow the server.

## Security

The server runs one program, `python -m pytest` (on Windows also `taskkill`
to stop it at a timeout), with the interpreter it runs
on, in its working directory, without a shell. The arguments are passed to
pytest as they are, and pytest options can load plugins (`-p`) and read
configuration, so the server gives the agent what running pytest in that
directory gives it; do not expose it to clients you would not let run your
test suite. `compare` reads the two JSON files it is given. Nothing is sent
over the network.

## Protocol

The server answers `initialize`, `ping`, `tools/list` and `tools/call`, and
ignores notifications. It supports the MCP protocol versions 2025-11-25,
2025-06-18, 2025-03-26 and 2024-11-05, and answers a client that asks for
another version with the newest. Tool results are one `text` content item
holding a JSON object; a tool call with bad arguments returns `isError: true`
with the reason, and an unknown tool is a JSON-RPC error. Requests are handled
one at a time, and a running test session cannot be cancelled except by its
timeout.
