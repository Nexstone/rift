# MCP setup — wire RIFT into Claude, Cursor, or any AI agent

The Model Context Protocol (MCP) lets AI agents call RIFT's commands as tools. With this wired up, you can talk to Claude in plain English and have it run backtests, scan markets, propose trades, and (if you've issued an auth token) execute them on your behalf.

RIFT ships an MCP server at `rift serve` exposing 59 tools — every read-only research command plus the gated trade-execution ones.

This doc walks through the three most common clients: **Claude Desktop**, **Claude Code (CLI)**, and **Cursor**. The pattern generalizes to anything that speaks MCP over stdio.

---

## Before you start

Verify the MCP server runs:

```bash
rift serve --help    # confirms the command exists
```

If `rift` isn't on your PATH, use the absolute path: `/path/to/rift/packages/cli/bin/run.js`. You'll need that absolute path in each client's config.

Get the absolute path now:

```bash
realpath packages/cli/bin/run.js    # Linux
echo "$PWD/packages/cli/bin/run.js"  # cross-platform
```

---

## Claude Desktop (macOS / Windows)

Claude Desktop reads `claude_desktop_config.json`. Open or create it:

| Platform | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

Add RIFT under `mcpServers`:

```json
{
  "mcpServers": {
    "rift": {
      "command": "/absolute/path/to/rift/packages/cli/bin/run.js",
      "args": ["serve"]
    }
  }
}
```

Restart Claude Desktop. You should see a hammer icon in the chat input — clicking it lists 59 RIFT tools.

Try: *"Run a backtest of trend_follow on BTC 4h and tell me the Sharpe ratio."*

---

## Claude Code (CLI)

Claude Code reads `~/.claude.json` or per-project `.mcp.json`. From inside your `rift/` directory:

```bash
claude mcp add rift -- /absolute/path/to/rift/packages/cli/bin/run.js serve
```

Or edit the config by hand:

```json
{
  "mcpServers": {
    "rift": {
      "command": "/absolute/path/to/rift/packages/cli/bin/run.js",
      "args": ["serve"]
    }
  }
}
```

In a `claude` shell:

```
> /mcp
```

You should see `rift` listed as `connected`. Try:

```
> Use the doctor tool to check my install
```

---

## Cursor

Cursor reads `.cursor/mcp.json` in your project (or `~/.cursor/mcp.json` globally). Same shape:

```json
{
  "mcpServers": {
    "rift": {
      "command": "/absolute/path/to/rift/packages/cli/bin/run.js",
      "args": ["serve"]
    }
  }
}
```

Restart Cursor, open the agent panel, you'll see RIFT tools available.

---

## Any other MCP client

The protocol is JSON-RPC 2.0 over stdio. The server speaks MCP `2024-11-05`. Equivalent generic config:

```yaml
command: /absolute/path/to/rift/packages/cli/bin/run.js
args: [serve]
transport: stdio
```

If you're writing your own client, the smoke test in `engine/tests/test_mcp_smoke.py` shows the wire protocol — initialize handshake, `tools/list`, `tools/call`.

---

## What you can ask the agent to do

The 59 tools fall into eight groups:

| Group | What |
|---|---|
| **Read** | `doctor`, `state`, `list_strategies`, `list_pairs`, `data_inventory`, `holdings`, `balance` |
| **Research** | `backtest`, `compare`, `walk_forward`, `montecarlo`, `research` (full pipeline), `sweep`, `smart_sweep` |
| **Analysis** | `regime`, `cross_asset`, `funding_browser`, `feature_importance`, `signal_stats`, `signal_decay`, `signal_backfill`, `tca`, `tearsheet` |
| **Validation** | `validate_strategy`, `verify`, `quick_test`, `attribution` |
| **Workbench** | `workbench_create`, `workbench_show`, `workbench_update`, `workbench_generate`, `workbench_templates`, `workbench_components` |
| **Trading (read-only)** | `cost`, `audit_export`, `history` |
| **Trading (gated)** | `buy`, `sell`, `manual_trade`, `algo_start`, `algo_stop`, `algo_status`, `close_position`, `tighten_stop`, `reduce_position`, `transfer` |
| **Operations** | `set_proxy`, `clear_proxy`, `experiments`, `experiment_revert`, `save_optimized`, `add_lesson`, `lessons` |

Read-only tools run without any auth. Trade-execution tools require:

1. A wallet configured via `rift auth setup` (creates an API wallet, pairs it with your main wallet on-chain)
2. Builder fee approval (one-time, on-chain)
3. An issued authorization token scoped to what you want the agent to do

See [`AUTH_AND_EXECUTION.md`](../AUTH_AND_EXECUTION.md) for the four capability tiers (T0/T1/T2/T3) and how authorization tokens scope what an agent can execute.

---

## A realistic agent prompt

> *"Scan the market for opportunities scoring above 0.5 with at least 3 signal categories. Show me the top 3, then run the full research pipeline on the best one. If the strategy passes promotion gates and the Sharpe is above 1.0, propose a long position sized at 5% of my equity with a 2% stop. Wait for my approval before executing."*

That's a single sentence. The agent will call `scan_market`, then `research`, then `propose_trade`, then wait. Approval comes from you typing "yes" — the agent can't bypass the T3 capability gate, which requires a signed token in its proposal.

---

## Troubleshooting

**Tools don't show up in Claude / Cursor**: usually a path issue. The `command` field must be an *absolute* path that exists and is executable. Test from the shell: `/path/to/rift/packages/cli/bin/run.js serve` should print a startup line (then hang waiting for input — kill with Ctrl+C).

**Tool calls return errors immediately**: run `rift doctor` from the shell. Most failures are credential or data issues that the doctor surfaces clearly.

**Tools time out**: slow research operations (walk-forward, Monte Carlo) can take 30–60 seconds. Most clients have a default 30s timeout. Check your client's timeout setting and bump it.

**MCP smoke test in CI fails**: run `engine/tests/test_mcp_smoke.py` locally with `-v` to see the failure. The test guards against the most common regression — initialize handshake breaking, tools/list returning bad shape, tool calls erroring on read-only paths.

---

## Security note

The MCP server is a local process speaking stdio with one client at a time. It does **not** expose a network socket, listen on a port, or accept external connections. Everything stays inside the trust boundary of the user account that started the process. The AI agent calling tools is bounded by:

1. RIFT's capability tiers (T0–T3) — most tools are T0/T1 and run without any auth check
2. Authorization tokens — required for T3 execution; you issue them with explicit scope
3. Hyperliquid's API wallet model — the API wallet cannot withdraw funds; worst-case agent compromise costs you bad trades, not capital

You retain full control. The agent can't take an action you haven't authorized.
