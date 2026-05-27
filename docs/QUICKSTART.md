# Quickstart — 10 minutes from clone to first backtest

This is the fastest path from `git clone` to a sealed, reproducible research run on the bundled BTC dataset. No wallet, no live trading, no AWS keys required.

By the end you'll have run the same validation pipeline RIFT uses to gate strategies for live deployment, against the reference `trend_follow` strategy.

---

## Prerequisites

You need three things on your machine. `rift doctor` (later) will verify each:

| Tool | Why | Install |
|---|---|---|
| **`uv`** | Python package manager that auto-installs Python 3.13 | `brew install uv` (macOS) · `curl -LsSf https://astral.sh/uv/install.sh \| sh` (Linux/WSL) |
| **Node 20+** | The CLI is TypeScript | `brew install node@20` · `nvm install 20` · or your distro's nodejs package |
| **`pnpm` 9+** | Node workspace manager | `npm install -g pnpm` or `corepack enable && corepack prepare pnpm@latest --activate` |

If you already have all three, run `uv --version`, `node --version`, `pnpm --version` to confirm.

## 1. Clone + install (3 minutes)

```bash
git clone <repo-url> rift
cd rift

# Python deps — uv installs Python 3.13 in an isolated venv at engine/.venv
cd engine && uv sync && cd ..

# TypeScript CLI
pnpm install
cd packages/cli && pnpm build && cd ../..

# Make `rift` callable from anywhere (one-time)
export PATH="$PWD/packages/cli/bin:$PATH"
# Persist by adding the above line to your ~/.zshrc or ~/.bashrc
```

## 2. Verify (30 seconds)

```bash
rift doctor
```

You should see green checks for Python, Node, the engine, Polars/NumPy/PyArrow, and Hyperliquid API connectivity. The wallet/builder-fee checks will fail — that's expected for research-only mode.

## 3. Run the research pipeline (5 minutes)

```bash
rift research trend_follow --pair BTC --tf 4h
```

If you haven't run `rift sync` yet, this auto-fetches recent BTC candles from the Hyperliquid REST API on demand (roughly the last 10 months of 4h candles). For the full 2-year historical window the docstring's metrics are calibrated to, run `rift sync --coins BTC --tf 4h` first (one-time, requires AWS keys in `~/.rift/.env`).

The pipeline runs:

| Step | What it does |
|---|---|
| Backtest | Vectorized historical simulation on real candle + funding data |
| Walk-forward | 6-month train / 3-month test windows, rolled across the series |
| Monte Carlo | 10,000 bootstrap resamples of the trade sequence |
| Multi-pair | Same strategy applied to 3 other coins for robustness |
| Feature importance | Which indicators drove the signal |
| Volatility forecast | Forward-looking ATR/regime estimate |
| Purged CV | k-fold CV with embargo + purge to defeat lookahead |
| Alpha decay | Half-life of the signal's IC |
| Capacity | Max trade size before market impact eats the edge |
| Promotion gates | DSR ≥ 0.85, CV pass rate ≥ 70%, drawdown limits, etc. |
| Sealed bundle | Content-addressed reproducibility manifest written to `~/.rift/bundles/` |

If you ran sync first (full 2-year archive), you'll see roughly:

```
Return: 25.0% / Sharpe: 0.71 / Max drawdown: -6.88% / 33 trades
Walk-forward: 70% profitable windows
Monte Carlo p(profit): 91.13%
Purged CV: 4/5 folds positive
Promotion: PASS (5/5 gates)
Grade: C — marginally profitable, edge weak
```

Without sync (auto-fetched ~10-month window), the numbers will differ — you'll see a smaller sample, narrower confidence intervals, and a grade that reflects the shorter regime mix. This is expected — backtest results are window-dependent, and the framework reports honestly on whatever window you give it. That's the point of the validation pipeline: it makes the dependence visible instead of pretending it doesn't exist.

The framework's honest verdict on the full 2-year window is `Grade C` (marginal). RIFT does not pretend `trend_follow` is alpha — it's a public, decades-old signal shipped as a learning template.

## 4. View the tearsheet

```bash
cat ~/.rift/reports/trend_follow_BTC_4h_tearsheet.md
```

Or open the sealed bundle for the cryptographic manifest:

```bash
ls ~/.rift/bundles/
cat ~/.rift/bundles/<hash>.json | jq .
```

## 5. What's next

- **Modify `trend_follow`** to learn the SDK: `packages/strategies-sdk/src/rift_strategies_sdk/examples/trend_follow.py` is ~15 lines of actual logic. See [`docs/strategies/authoring.md`](strategies/authoring.md).
- **Build your own strategy**: `rift new my_strategy` scaffolds a working skeleton in `strategies/`.
- **Wire up an AI agent**: see [`docs/mcp/setup.md`](mcp/setup.md) to drive RIFT from Claude Desktop / Cursor / any MCP client.
- **Sync real data** (optional, ~$2 in AWS S3 fees): set AWS keys in `~/.rift/.env`, run `rift sync --coins BTC,ETH,SOL --tf 1h,4h`.
- **Go live** (when you have a real validated strategy of your own — *not* with `trend_follow`): see [`docs/AUTH_AND_EXECUTION.md`](AUTH_AND_EXECUTION.md) for the trust architecture before paying real money to test the order path.

---

## Common stumbles

**`rift: command not found`** — You skipped the `export PATH=...` step. Use the full path `./packages/cli/bin/run.js` until you symlink or PATH-add.

**`Hyperliquid API` check fails** — Some networks block HL's IP. Run `rift set-proxy socks5://<host>:<port>` if you need a SOCKS5 proxy.

**`Cannot import rift_*`** — `cd engine && uv sync` didn't complete cleanly. Re-run; look for a real error in the output.

**`rift doctor` reports `.env permissions ... 644`** — Run `chmod 600 ~/.rift/.env`. RIFT will warn but not block.
