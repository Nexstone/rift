"""MCP smoke test — speak JSON-RPC 2.0 to `rift serve` over stdio.

Mirrors what Claude Desktop / Claude Code / Cursor do when connecting to
the RIFT MCP server: initialize handshake → tools/list → a few
representative read-only tool calls. All tool calls in this test are
side-effect-free; no money moves.

This test is marked `slow` because it spawns the TypeScript CLI as a
subprocess and requires `packages/cli/dist/` to exist (i.e. `pnpm build`
must have already run). In CI it lives in its own job that runs after
the TS build. Locally:

    cd packages/cli && pnpm build
    uv run --project engine pytest engine/tests/test_mcp_smoke.py -m slow

Failure modes this guards against:
  - MCP server fails to boot (missing dep, import error, port collision)
  - tools/list returns the wrong shape or unexpected tool count
  - A representative tool call crashes the server or returns a malformed
    response (catches accidental regressions in serve.ts wrappers)
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
RIFT_CLI_BIN = REPO_ROOT / "packages" / "cli" / "bin" / "run.js"
RIFT_CLI_DIST = REPO_ROOT / "packages" / "cli" / "dist"

# Lower bound — if a few tools get retired we still want the test to
# pass. Upper bound catches accidental over-registration. Adjust
# intentionally on real changes.
EXPECTED_TOOL_COUNT_MIN = 50
EXPECTED_TOOL_COUNT_MAX = 80


pytestmark = pytest.mark.slow


def _require_built_cli() -> None:
    """Skip the test if the TypeScript CLI hasn't been built."""
    if not RIFT_CLI_BIN.exists():
        pytest.skip(f"TS CLI binary not found at {RIFT_CLI_BIN} — run `pnpm build`")
    if not RIFT_CLI_DIST.exists():
        pytest.skip(f"TS CLI dist not found at {RIFT_CLI_DIST} — run `pnpm build`")


def _send(proc: subprocess.Popen, request: dict, timeout: float = 30.0) -> dict:
    """Send a JSON-RPC request and read the matching response."""
    line = (json.dumps(request) + "\n").encode()
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(line)
    proc.stdin.flush()

    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = proc.stdout.readline()
        if not raw:
            raise RuntimeError(f"MCP server closed stdout before responding to id={request.get('id')}")
        try:
            msg = json.loads(raw.decode().strip())
        except json.JSONDecodeError:
            continue
        if msg.get("id") == request.get("id"):
            return msg
    raise TimeoutError(f"No response to id={request.get('id')} within {timeout}s")


def _notify(proc: subprocess.Popen, method: str) -> None:
    """Send a JSON-RPC notification (no id, no response expected)."""
    assert proc.stdin is not None
    line = json.dumps({"jsonrpc": "2.0", "method": method}).encode() + b"\n"
    proc.stdin.write(line)
    proc.stdin.flush()


@pytest.fixture(scope="module")
def mcp_server():
    """Spawn `rift serve` as a subprocess and tear it down after the test module."""
    _require_built_cli()

    proc = subprocess.Popen(
        [str(RIFT_CLI_BIN), "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Give Node a moment to boot the SDK and register tools.
    time.sleep(2)

    if proc.poll() is not None:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        pytest.fail(f"MCP server exited at startup (code {proc.returncode})\nstderr: {stderr[:1000]}")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_mcp_initialize(mcp_server: subprocess.Popen) -> None:
    """Server responds to the MCP initialize handshake with the expected shape."""
    resp = _send(mcp_server, {
        "jsonrpc": "2.0", "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "rift-smoke", "version": "0.1"},
            "capabilities": {},
        },
    })
    assert "error" not in resp, f"initialize failed: {resp}"
    result = resp["result"]
    assert result.get("protocolVersion"), "missing protocolVersion in initialize response"
    server_info = result.get("serverInfo") or {}
    assert server_info.get("name"), "missing serverInfo.name"

    _notify(mcp_server, "notifications/initialized")


def test_mcp_tools_list(mcp_server: subprocess.Popen) -> None:
    """tools/list returns the expected number of tools with valid shape."""
    resp = _send(mcp_server, {
        "jsonrpc": "2.0", "id": 2,
        "method": "tools/list",
    })
    assert "error" not in resp, f"tools/list failed: {resp}"
    tools = resp["result"]["tools"]
    count = len(tools)
    assert EXPECTED_TOOL_COUNT_MIN <= count <= EXPECTED_TOOL_COUNT_MAX, (
        f"tools/list returned {count} tools; expected {EXPECTED_TOOL_COUNT_MIN}-{EXPECTED_TOOL_COUNT_MAX}. "
        f"Update bounds in this test if the change is intentional."
    )
    # Every tool has a name and description
    for tool in tools:
        assert tool.get("name"), f"tool missing name: {tool}"
        assert tool.get("description"), f"tool {tool.get('name')} missing description"

    # Sanity-check that core tools are present
    names = {t["name"] for t in tools}
    expected_core = {"doctor", "list_strategies", "cost"}
    missing = expected_core - names
    assert not missing, f"expected core tools missing from MCP surface: {missing}"


def test_mcp_doctor_call(mcp_server: subprocess.Popen) -> None:
    """The `doctor` tool returns a non-empty response without error."""
    resp = _send(mcp_server, {
        "jsonrpc": "2.0", "id": 3,
        "method": "tools/call",
        "params": {"name": "doctor", "arguments": {}},
    })
    assert "error" not in resp, f"doctor call failed: {resp}"
    result = resp["result"]
    assert not result.get("isError"), f"doctor returned isError: {result}"
    content = result.get("content") or []
    assert content, "doctor returned empty content"
    text = content[0].get("text", "")
    assert text, "doctor returned empty text"


def test_mcp_list_strategies_call(mcp_server: subprocess.Popen) -> None:
    """The `list_strategies` tool returns the expected JSON shape."""
    resp = _send(mcp_server, {
        "jsonrpc": "2.0", "id": 4,
        "method": "tools/call",
        "params": {"name": "list_strategies", "arguments": {}},
    })
    assert "error" not in resp, f"list_strategies call failed: {resp}"
    result = resp["result"]
    assert not result.get("isError"), f"list_strategies returned isError: {result}"
    content = result.get("content") or []
    assert content, "list_strategies returned empty content"
    text = content[0].get("text", "")
    data = json.loads(text)
    # Expect at least the validated key — may be empty if no validated edge yet.
    assert "validated" in data or "custom" in data, (
        f"list_strategies response missing both 'validated' and 'custom' keys: {data}"
    )


def test_mcp_cost_call(mcp_server: subprocess.Popen) -> None:
    """The `cost` tool returns a structured pre-trade quote."""
    resp = _send(mcp_server, {
        "jsonrpc": "2.0", "id": 5,
        "method": "tools/call",
        "params": {
            "name": "cost",
            "arguments": {"pair": "BTC", "notional_usd": 1000.0},
        },
    })
    assert "error" not in resp, f"cost call failed: {resp}"
    result = resp["result"]
    assert not result.get("isError"), f"cost returned isError: {result}"
    content = result.get("content") or []
    assert content, "cost returned empty content"
    text = content[0].get("text", "")
    data = json.loads(text)
    assert data.get("pair") == "BTC", f"cost returned wrong pair: {data}"
    cost_breakdown = data.get("cost") or {}
    # We don't assert on the magnitude — fees change. Just shape.
    assert "total_bps" in cost_breakdown, f"cost missing total_bps: {data}"
    assert "total_usd" in cost_breakdown, f"cost missing total_usd: {data}"
