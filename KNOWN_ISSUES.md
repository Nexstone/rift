# Known Issues

This file lists known limitations, sharp edges, and behaviors that may surprise new users. We believe in surfacing these up front rather than letting users discover them in production.

If you encounter something not on this list that you think should be, open an issue.

---

## Strategy library

### Only one reference strategy ships

RIFT ships with exactly one strategy: `trend_follow`. It is explicitly framed as a **demo / reference implementation**, not as production alpha. The framework's value is in the validation pipeline, the algo runner, the MCP integration, and the substrate primitives — not in shipped strategies.

If you want to trade live, write your own strategy. The strategies SDK (`packages/strategies-sdk/`) shows you how.

The `trend_follow` strategy's docstring includes validated metrics on a specific historical window. Those numbers are **not predictions**. Public EMA-crossover signal is decades-old and has been arbitraged. Do your own out-of-sample research before deploying capital.

## Platform support

### Tested platforms

| Platform | CI tested | Status |
|---|---|---|
| macOS (Apple Silicon + Intel) | ✅ | Primary dev environment |
| Ubuntu Linux | ✅ | CI green; recommended for servers |
| Windows via WSL2 | ⚠️ | Should work (it's just Linux), but untested by maintainers |
| Native Windows | ❌ | Not supported. Use WSL2. |
| FreeBSD / other Unix | ❌ | Not tested |

### Python version

Pinned to **3.13** via `.python-version` and CI. Python 3.14 *should* work (per `requires-python=">=3.13"`) but is not regularly tested.

### Node version

Pinned to **20+** via `package.json` engines. Tested on Node 20 and Node 22.

## Live trading

### Daemon under `kill -9` may leave open orders

If you SIGKILL the algo daemon (rather than SIGINT/SIGTERM), the daemon does not get a chance to cancel its open stop-loss order on Hyperliquid. The position itself remains protected by the on-exchange stop, but the daemon's local state file becomes inconsistent with the exchange state.

On daemon restart, `_check_orphaned_positions()` will detect any open position and either reattach or surface a warning. Verify with `rift algo status` and `rift more balance` after any unclean shutdown.

### Hyperliquid websocket reconnection

Brief WebSocket disconnects are handled automatically. Extended disconnects (>30s) may cause the algo daemon to miss candle boundaries. The next candle boundary will resync state from the REST API.

If you suspect a missed signal during a network outage, check `~/.rift/algo/logs/<session>.log` for `reconnect` events.

### Builder fee tampering protection

`packages/trade/src/rift_trade/builder_fee.py` is hash-sealed at release time via `scripts/seal_release.py`. Any modification to that file — even whitespace — will cause `get_builder_info()` to return an invalid fee, causing all orders to be rejected by Hyperliquid.

If you fork RIFT and modify `builder_fee.py`, you must either:
1. Re-run `python scripts/seal_release.py` after every change, or
2. Leave the seal empty (dev mode), in which case the integrity check is bypassed.

## Data

### Historical data requires AWS credentials

`rift sync` downloads historical candles, funding, and L2 books from Hyperliquid's public S3 archive. The data is public but S3 access requires *your own AWS account* (free tier is sufficient — you'll pay roughly $2 for a full historical pull). See `.env.example`.

If you don't want to set up AWS, RIFT still works for live trading — research mode just won't have history.

### L2 order book data is partial

L2 books are downloaded for the most-traded coins only. Rare or low-volume pairs may have gaps. Check `rift data-inventory` for what's available.

## Research pipeline

### Backtest results depend on data freshness

If you backtest a strategy and the data is older than ~24 hours, the framework will warn but not block. For the most accurate results, run `rift sync` before any meaningful research run.

### Promotion gates are configurable

The default promotion gates in `pyproject.toml` are RIFT's opinion on what makes a strategy worth deploying. They are not the only valid choice. Lowering them may make more strategies "pass" but the resulting strategies are more likely to fail in live trading.

### Walk-forward is not magic

Walk-forward validation reduces overfitting risk but does not eliminate it. A strategy that passes walk-forward on 2 years of data may still fail in the next regime. RIFT does not predict the future.

## Distribution

### Single-binary installers not yet available

For v0.1, RIFT installs from source: `git clone` + `uv sync` + `pnpm install`. Future releases will offer Homebrew (macOS), `pip install rift-engine` (PyPI), and `npm install -g @nexstone/rift-cli` (npm) as one-line install paths.

Until then, follow the install instructions in [`README.md`](README.md) or [`docs/INSTALL.md`](docs/INSTALL.md).

### No published Docker image yet

A maintained Dockerfile is planned but not yet shipped. If you build one, please contribute it back.

## Documentation

### v0.1 documentation set

The following docs ship in v0.1 and are the canonical references:

- [`docs/QUICKSTART.md`](docs/QUICKSTART.md) — 10-minute path from clone to a sealed research bundle
- [`docs/INSTALL.md`](docs/INSTALL.md) — full install for macOS, Ubuntu/Debian, WSL2
- [`docs/strategies/AUTHORING.md`](docs/strategies/AUTHORING.md) — write a Python strategy from scratch with the SDK
- [`docs/signals/AUTHORING.md`](docs/signals/AUTHORING.md) — add custom scout signals via the `@signal` decorator
- [`docs/research/METHODOLOGY.md`](docs/research/METHODOLOGY.md) — what the 14 research stages compute and what they can't protect against
- [`docs/mcp/SETUP.md`](docs/mcp/SETUP.md) — wire RIFT into Claude Desktop / Claude Code / Cursor as an MCP server
- [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md) — auto-generated per-command help (regenerate via `scripts/gen_cli_reference.sh`)
- [`docs/AUTH_AND_EXECUTION.md`](docs/AUTH_AND_EXECUTION.md) — three-layer key model, capability tiers, audit substrate
- [`docs/BACKUP_AND_STATE.md`](docs/BACKUP_AND_STATE.md) — what's in `~/.rift/`, backup strategies, machine-to-machine migration
- [`docs/RUNBOOK_ALGO_MONITORING.md`](docs/RUNBOOK_ALGO_MONITORING.md) — daily/weekly checks for a long-running algo daemon
- [`engine/SIGNALS.md`](engine/SIGNALS.md) — indicator + signal library reference (38 signals across 9 categories)

If something is missing or unclear, open an issue — we'll prioritize gaps based on what users actually hit.
