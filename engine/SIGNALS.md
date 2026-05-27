# RIFT Signal Factory

The signal factory is RIFT's core intelligence layer — 38 independent signals that detect weak patterns across funding, momentum, microstructure, volatility, cross-pair dynamics, seasonality, computed math, exchange stats, and real-time websocket data. Each signal returns a score from -1 (strong short) to +1 (strong long). The aggregator combines them into ranked opportunities.

Inspired by Renaissance Technologies / Medallion Fund: many weak, uncorrelated signals combined produce strong alpha. Grinold's Fundamental Law: IR = IC × sqrt(Breadth). More independent signals = higher information ratio.

## Architecture

```
Strategy on_candle()
    ↓
StrategyState (indicators + market context + ws_feed data)
    ↓
Signal Functions (38 registered via @signal decorator)
    ↓
SignalResult(name, score, reason, category, confidence)
    ↓
Aggregator (weighted average by confidence)
    ↓
Ranked Opportunities
```

### Key files

| File | Purpose |
|---|---|
| `src/rift/signals/base.py` | `Signal`, `SignalResult` dataclasses, `@signal` decorator, `_SIGNAL_REGISTRY`, `compute_all_signals()` |
| `src/rift/signals/aggregator.py` | `aggregate_signals()` weighted averaging, `rank_opportunities()` multi-coin ranking |
| `src/rift/signals/__init__.py` | Imports all signal modules to trigger `@signal` registration |
| `src/rift/signals/funding.py` | Funding rate signals (3) |
| `src/rift/signals/momentum.py` | Price momentum signals (3) |
| `src/rift/signals/microstructure.py` | Order flow and positioning signals (8) |
| `src/rift/signals/volatility.py` | Volatility regime signals (3) |
| `src/rift/signals/cross_pair.py` | Cross-asset relative value signals (4) |
| `src/rift/signals/seasonality.py` | Time-based pattern signals (3) |
| `src/rift/signals/computed.py` | Pure math signals derived from price/volume (8) |
| `src/rift/signals/hyperstats.py` | Scraped L/S ratio and leverage signals (3) |
| `src/rift/signals/realtime.py` | Websocket-fed signals — trade tape, spoofing, vaults (3) |
| `src/rift/ws_feed.py` | `LiveMarketFeed` — in-daemon websocket subscriber for live/sim/recon |
| `src/rift/signal_memory.py` | Signal hit rate learning (tracks historical accuracy) |

### Adding a new signal

1. Write a function in the appropriate category file (or create a new one):
```python
from rift.signals.base import signal, SignalResult

@signal("my_signal", "category_name", "One-line description of what it detects")
def my_signal(coin: str, state: dict) -> SignalResult:
    value = state.get("some_field", 0)
    if value > threshold:
        return SignalResult("my_signal", 0.5, "Reason text", "category_name", 0.4)
    return SignalResult("my_signal", 0, "", "category_name", 0)
```

2. If you created a new file, import it in `src/rift/signals/__init__.py`:
```python
import rift.signals.my_new_module  # noqa: F401
```

3. The `@signal` decorator auto-registers it. No other wiring needed.

### Score conventions

- **Score**: -1.0 to +1.0. Positive = bullish (long). Negative = bearish (short). Zero = no opinion.
- **Magnitude**: 0.1-0.3 = weak signal. 0.4-0.6 = moderate. 0.7+ = strong conviction.
- **Confidence**: 0.0-1.0. Historical reliability of this signal. Used as weight in aggregation.
- **Return zero** when data is missing — signals must be dormant, never hallucinate.

---

## Signal Reference (38 signals)

### Funding (3 signals)

| Signal | Description | Data source | Backtestable |
|---|---|---|---|
| `funding_extreme` | Funding rate beyond normal range — contrarian signal | HL funding API | Yes |
| `funding_divergence` | HL funding vs CEX average — cross-exchange arbitrage | predictedFundings API | Yes |
| `funding_zscore` | Funding rate z-score vs rolling window — statistical extreme | HL funding API | Yes |

### Momentum (3 signals)

| Signal | Description | Data source | Backtestable |
|---|---|---|---|
| `rsi_extreme` | RSI overbought/oversold — mean reversion signal | Candle OHLCV | Yes |
| `ema_trend` | EMA crossover — trend direction | Candle OHLCV | Yes |
| `price_momentum` | Rate of price change — momentum persistence | Candle OHLCV | Yes |

### Microstructure (8 signals)

| Signal | Description | Data source | Backtestable |
|---|---|---|---|
| `oi_divergence` | OI direction vs price direction — trend confirmation or divergence | Market context API | Yes |
| `volume_imbalance` | Buy vs sell volume imbalance (CVD) — directional pressure | Candle volume + CVD | Yes |
| `volume_surge` | Volume >1.5x average — institutional activity confirmation | Candle volume | Yes |
| `oi_zscore` | OI z-score — extreme positioning detection | Market context API | Yes |
| `net_positioning` | Net long/short positioning delta — crowding detection | Market context API | Yes |
| `liquidation_proximity` | High OI + extreme funding + stretched price — cascade risk | Market context + funding | Yes |
| `whale_activity` | Volume >3x average + strong directional delta — large player | Volume + delta | Yes |
| `orderbook_imbalance` | Bid/ask depth ratio — short-term directional pressure | L2 orderbook snapshot | Yes |

### Volatility (3 signals)

| Signal | Description | Data source | Backtestable |
|---|---|---|---|
| `vol_mean_reversion` | Volatility at extremes — expect reversion to mean | Candle OHLCV | Yes |
| `squeeze_detection` | Bollinger inside Keltner — volatility compression before expansion | Candle OHLCV | Yes |
| `premium_extreme` | Mark vs oracle premium — funding mechanics force convergence | Market context API | Yes |

### Cross-Pair (4 signals)

| Signal | Description | Data source | Backtestable |
|---|---|---|---|
| `market_breadth` | % of market overbought/oversold by RSI — crowd sentiment | Cross-asset RSI scan | Yes |
| `avg_rsi_deviation` | Coin RSI vs market average — relative strength/weakness | Cross-asset RSI scan | Yes |
| `btc_lead_lag` | BTC moves first, alts follow 5-30min later — catch-up trade | BTC price + alt price | Yes |
| `correlation_breakdown` | Normally correlated pair diverging — convergence trade | Cross-asset RSI scan | Yes |

### Seasonality (3 signals)

| Signal | Description | Data source | Backtestable |
|---|---|---|---|
| `funding_settlement_window` | Approaching hourly funding settlement — pre-settlement drift | System clock + funding | Yes |
| `session_transition` | Asia/EU/US session boundaries — vol regime changes | System clock + price | Yes |
| `new_listing_spike` | New perp listing <72h old — extreme vol, fade retail crowd | Listing age + funding | Yes |

### Computed (8 signals)

| Signal | Description | Data source | Backtestable |
|---|---|---|---|
| `hurst_exponent` | Trend persistence (>0.5) vs mean reversion (<0.5) — regime detection | Price history (R/S analysis) | Yes |
| `return_autocorrelation` | Serial correlation of returns — momentum vs reversal regime | Price history | Yes |
| `return_kurtosis` | Fat tail detection — expect extreme moves, reduce size | Price history | Yes |
| `oi_acceleration` | Second derivative of OI — institutional accumulation/distribution | OI rate of change | Yes |
| `cvd_momentum` | Slope of cumulative volume delta — buying pressure trend | CVD + volume delta | Yes |
| `price_oracle_gap` | Perp price vs oracle price divergence — convergence trade | Price + oracle price | Yes |
| `predicted_actual_divergence` | Predicted funding flipping before actual — early entry window | Predicted + actual funding | Yes |
| `volume_weighted_rsi` | RSI weighted by relative volume — high-vol moves matter more | Price + volume history | Yes |

### HyperStats (3 signals)

| Signal | Description | Data source | Backtestable |
|---|---|---|---|
| `ls_ratio_extreme` | 75%+ traders on one side — retail overcrowded, contrarian | HyperStats scraper | Yes (if historical data exists) |
| `leverage_extreme` | Average leverage >7x — fragile market, cascade risk | HyperStats scraper | Yes (if historical data exists) |
| `unrealized_pnl` | Aggregate unrealized P&L — profit-taking or capitulation risk | HyperStats scraper | Yes (if historical data exists) |

### Realtime (3 signals) — LIVE ONLY

| Signal | Description | Data source | Backtestable |
|---|---|---|---|
| `trade_tape_imbalance` | Per-trade buy/sell flow with large trade separation | Websocket `trades` channel | No (live only) |
| `spoofing_detection` | Phantom liquidity — orders placed then cancelled before fill | Websocket `l2Book` channel | No (live only) |
| `vault_smart_money` | Top vault position consensus — institutional flow | REST vault API polling | No (live only) |

---

## Backtestability

- **35 signals** are fully backtestable using historical candle, funding, OI, and market context data
- **3 signals** (realtime category) are live-only because they require websocket trade-level or order book change data that has no historical equivalent
- The 3 live-only signals act as **confidence modifiers** — they don't generate entries on their own but can boost or dampen conviction on entries that the backtested signals already approve
- The websocket collector (`scrapers/rift_ws_collector.py`) is accumulating historical websocket data. Once enough exists (months), a replay mechanism can make these signals backtestable too

---

## Data Flow

### How signals get their data

Signals receive a `state: dict` parameter. In live/sim mode, this state is populated from multiple sources:

```
StrategyState (built every 5 seconds in live.py / simulate.py)
├── indicators        ← Computed from candle history (RSI, EMA, BB, etc.)
├── funding_rate      ← HL REST API: /info {type: "clearinghouseState"}
├── predicted_funding ← HL REST API: /info {type: "predictedFundings"}
├── open_interest     ← HL REST API: /info {type: "metaAndAssetCtxs"}
├── premium           ← HL REST API: mark vs oracle from metaAndAssetCtxs
├── oracle_price      ← HL REST API: oracle price from metaAndAssetCtxs
├── day_volume        ← HL REST API: 24h volume from metaAndAssetCtxs
├── funding_divergence← HL REST API: predictedFundings (HL vs CEX avg)
├── market_breadth_*  ← HL REST API: cross-asset RSI scan
├── cvd               ← LiveMarketFeed websocket (cumulative volume delta)
├── volume_delta      ← LiveMarketFeed websocket (per-minute buy - sell)
└── relative_volume   ← LiveMarketFeed websocket (current vol / 60-min avg)
```

### LiveMarketFeed (`src/rift/ws_feed.py`)

Each live/sim/recon daemon instantiates its own `LiveMarketFeed` for the coin being traded. No external collector needed — works on any machine.

```python
feed = LiveMarketFeed(coin="BTC")
feed.start()  # background threads: WS connection + vault polling

# Every tick:
ws_data = feed.get_derived()
# Returns: {cvd, volume_delta, relative_volume, tape, orderflow, vault_positions}
```

**Subscribes to:**
- `trades` channel — aggregated into 1-minute buckets (buy/sell volume, large trade detection, imbalance, VWAP, tape speed)
- `l2Book` channel — tracks depth changes for spoofing detection (phantom bid/ask ratios)

**Polls via REST:**
- Vault positions — top 20 vaults by equity, every 15 minutes

**Properties:**
- Single coin subscription (not 50) — minimal bandwidth
- In-memory only — no disk I/O
- Thread-safe — daemon threads die with parent
- Auto-reconnect with exponential backoff
- Graceful shutdown with `feed.stop()`

### Confluence sizing (live.py / simulate.py)

The confluence logic at entry time checks 4 dimensions. Before the websocket wiring, `oi_roc`, `cvd`, and `relative_volume` were stuck at 0.0 (dead code). Now they have live data:

| Check | Field | Source |
|---|---|---|
| OI momentum agrees with direction | `strat_state.oi_roc` | Market context API |
| Premium agrees with direction | `strat_state.premium` | Market context API |
| Volume above average | `strat_state.relative_volume` | LiveMarketFeed websocket |
| CVD agrees with direction | `strat_state.cvd` | LiveMarketFeed websocket |

Confluence multiplier ranges from 0.5x (0% agreement) to 1.5x (100% agreement), applied to position size.

---

## Data Collectors

Three separate collection systems exist, each for a different purpose:

### 1. REST Collector (`scrapers/rift_data_collector.py`)

**Purpose:** Accumulate historical data for backtesting and npm bundling.

**Runs on:** Mac Mini (cron or daemon mode).

**Collects:**
- 1m / 5m / 15m / 1h candles for all 50 coins
- Funding rates (every 4 hours)
- Market context snapshots (OI, premium, volume — every 5 minutes)
- L2 orderbook snapshots (top 10 coins — every 5 minutes)
- Liquidation event detection (every 5 minutes)
- Whale trade detection (every 5 minutes)
- Vault positions (top 20 vaults — every 15 minutes)
- Coinalyze daily data (candles, OI, funding — once per day)

**Storage:** `~/.rift/data/` as Parquet files. ~20 MB/day.

### 2. WebSocket Collector (`scrapers/rift_ws_collector.py`)

**Purpose:** Accumulate historical websocket data (trade tape + order flow) for future backtest replay.

**Runs on:** Mac Mini (long-running daemon).

**Subscribes to:** `trades` + `l2Book` for all 50 coins via `wss://api.hyperliquid.xyz/ws`.

**Produces:**
- `~/.rift/data/_ws_trades/{COIN}/YYYY-MM-DD.parquet` — 1-minute trade buckets
- `~/.rift/data/_ws_orderflow/{COIN}/YYYY-MM-DD.parquet` — 5-minute order flow buckets

**Storage:** ~7-8 MB/day.

**Status check:** `python3 scrapers/rift_ws_collector.py --status`

### 3. In-Daemon Feed (`src/rift/ws_feed.py` — `LiveMarketFeed`)

**Purpose:** Provide real-time websocket data to live/sim/recon daemons.

**Runs on:** Any machine running `rift live`, `rift sim`, or `rift recon`.

**Subscribes to:** `trades` + `l2Book` for the single coin being traded.

**Stores:** In-memory only — no disk writes.

**Lifecycle:** Created when daemon starts, destroyed when daemon stops. No external dependency.

**Key difference from collectors:** This runs inside the trading process. No separate daemon needed. Any user who runs `rift live BTC-PERP trend_follow` automatically gets real-time websocket data feeding into their signals and confluence sizing.

---

## Coin Universe

The set of coins Scout (and other research surfaces) considers is constructed
at runtime from `rift_substrate.universe.Universe`, not hardcoded. There is no
fixed 50-coin list shipped with RIFT.

**Default behaviour:** Scout queries Hyperliquid live, filters out anything
with < $100k 24h notional volume, drops user-blacklisted coins (from
`~/.rift/validated_edge.json` if present, empty by default), and ranks the
remaining coins by volume. `--top N` (default 20) picks the top-N by volume.

**Composable selection primitives** (substrate-level — composable in Python
or via future workbench config):

| Constructor | What it does |
|-------------|--------------|
| `Universe.from_hl(min_volume_24h_usd, exclude, include_only)` | Live HL query with volume floor + manual filters |
| `Universe.from_hl_data(meta, asset_ctxs, ...)` | Same logic, but from pre-fetched HL data (no extra roundtrip) |
| `Universe.from_cache()` | Every coin you have local data for |
| `Universe.from_sectors(["L1", "Meme"])` | By vendored sector tags |
| `Universe.from_list(["BTC", "ETH"])` | Explicit list |
| `spec.top_by_volume(n)` | Method — sub-select the top N by 24h volume from any spec |
| `Universe.intersection / difference / union` | Set ops |

Power users compose these directly in Python; Scout's default path (top-N by
volume, with the user's blacklist applied) is one composition.

`kPEPE` and `kSHIB` represent 1000x units in HL's universe (Binance equivalent:
`1000PEPEUSDT`, `1000SHIBUSDT`).

---

## Scout — Multi-Timeframe Market Scanner

Scout scans the top coins on Hyperliquid using a two-phase approach: higher timeframe for directional bias, lower timeframe for entry timing. Only coins where multiple independent signal categories agree AND the lower timeframe confirms get surfaced.

### Two-phase architecture

```
Phase 1: BIAS (1h candles)
  ├── Run all 38 signals on 1h data
  ├── Require 3+ independent categories agreeing on direction
  ├── Require funding alignment (never fight funding)
  ├── Require above-average volume
  └── Kill combos with <45% historical hit rate

Phase 2: ENTRY (5m candles)
  ├── Run all 38 signals on 5m data for coins that passed Phase 1
  ├── If 5m agrees with 1h bias → strong setup
  ├── If 5m mildly opposes (pullback) → still valid entry (buy the dip)
  └── If 5m strongly opposes → skip (trend may be reversing)

Combined score = bias_score × 0.6 + entry_score × 0.4
```

Bias and entry timeframes are defaults — set any pair via `--bias-tf` and `--entry-tf`. The two-timeframe pattern itself is Scout's opinionated workflow; users wanting different workflows can compose substrate primitives directly.

### Quality filters

Scout applies four filters before an opportunity is surfaced. These turned a -659% loser into a +50% winner over 2.5 years of backtested data:

1. **Category diversity (≥3)** — Signals must come from at least 3 independent categories (e.g., funding + microstructure + computed). Five momentum signals agreeing is weaker than three categories agreeing.

2. **Funding alignment (Scout default — configurable)** — Scout's default filter avoids fighting funding: long requires funding ≤ 0.02%/hr, short requires funding ≥ -0.02%/hr. This is opinionated for funding-aware strategies; market-making, basis trades, and HFT may want this disabled.

3. **Volume floor (rel_vol ≥ 1.0)** — Dead markets produce false signals. Require at least average volume for follow-through.

4. **Signal memory kill switch (<45% = skip)** — If a signal combination has historically won less than 45% of the time, skip entirely. Proven combos (>60%) get a score boost.

### Usage

**CLI:**
```bash
rift scout                                    # Default: 1h bias, 5m entry, top 20
rift scout --top 50                           # Scan more coins
rift scout --bias-tf 4h --entry-tf 15m        # Longer timeframes
rift scout --min 3                            # Require 3+ signals on bias TF
rift scout --tf 1h                            # Convenience alias: sets bias timeframe
```

**Python:**
```python
from rift.scout import scan_market

opportunities = scan_market(top_n=20, bias_tf="1h", entry_tf="5m")
for opp in opportunities:
    print(f"{opp.coin} {opp.direction} — score {opp.score:.3f}")
    print(f"  {opp.num_categories} categories, {opp.num_signals} signals")
    # Bias signals (from 1h)
    for s in opp.signals:
        if not s['name'].endswith('_entry'):
            print(f"    [bias] {s['name']}: {s['score']:+.3f}")
    # Entry signals (from 5m)
    for s in opp.signals:
        if s['name'].endswith('_entry'):
            print(f"    [entry] {s['name']}: {s['score']:+.3f}")
```

### Output format

Scout emits NDJSON:

```json
{"type": "progress", "coin": "BTC", "pct": 5, "phase": "bias"}
{"type": "progress", "coin": "BTC", "pct": 5, "phase": "entry", "bias": "SHORT", "bias_score": 0.419}
{"type": "result", "command": "scout", "opportunities": [...], "scanned": 20, "bias_tf": "1h", "entry_tf": "5m"}
```

Each opportunity is a complete **mission brief** for Recon:

| Field | Type | Description |
|---|---|---|
| `coin` | str | Coin symbol |
| `direction` | str | "LONG" or "SHORT" (from bias phase) |
| `score` | float | Combined score: bias × 0.6 + entry × 0.4 |
| `num_signals` | int | Total signals (bias + entry) |
| `num_categories` | int | Independent categories agreeing on direction |
| `categories` | list | All categories that fired (bias + entry) |
| `signals` | list | Bias signals + entry signals (entry suffixed with `_entry`) |
| `entry_price` | float | Current mid price |
| `stop_price` | float | Entry ± 2×ATR (from entry timeframe) |
| `target_price` | float | Entry ± 4×ATR (2:1 R/R) |
| `funding_rate` | float | Current hourly funding rate |
| `hit_rate` | float? | Historical win rate from signal memory |
| **`leverage`** | int | 1x, 2x, or 3x — from confidence + score |
| **`size_pct`** | float | Position size as % of equity — Kelly from signal memory |
| **`hold_type`** | str | "funding" / "momentum" / "mean_reversion" — from dominant signal categories |
| **`staleness_minutes`** | int | Opportunity expires after this — funding=60, momentum=15, mean_reversion=5 |
| **`confidence_tier`** | str | "high" (5+ cats, >60% hit rate) / "medium" (4 cats) / "low" |

### Signal memory

Scout uses `~/.rift/signal_memory.jsonl` — a growing lookup table of what actually works.

**Populating memory:**
- `rift signal-backfill --top 10` — replays historical candle data through all 38 signals, checks outcomes 12 candles later, records wins/losses
- Live/sim trade outcomes (when trades close)

**Using memory:**
- Combos with <45% hit rate are killed entirely (not traded)
- Combos with >60% hit rate get a +0.10 score boost
- Memory is queried at three levels: exact combo match → individual signal averages → coin+direction baseline

### Websocket soak

By default, Scout runs a 2-minute websocket soak before scanning. This subscribes to `trades` + `bbo` + `activeAssetCtx` for all top N coins via `MultiCoinFeed`, collecting real trade flow and live market context.

After the soak:
- CVD is from real trade flow, not candle approximation
- `trade_tape_imbalance` signal fires (real buy/sell imbalance)
- `orderbook_imbalance` signal fires (real bid/ask ratio from BBO)
- `activeAssetCtx` provides live funding, OI, oracle price without REST polling

```bash
rift scout                    # default: 120s soak
rift scout --soak 300         # 5 minute soak for deeper data
rift scout --no-soak          # skip soak (faster, approximate data)
```

Without soak, Scout approximates CVD from candle direction and the 3 realtime signals stay dormant.

---

## Recon — Trade Executor

Recon is the soldier. Scout delivers the mission brief, Recon confirms and executes.

### Flow

```
$ rift recon

  Soaking live data (120s)...
  Scanning with live data...

  SCOUT RESULTS

  [1]  SHORT PENGU   score=0.489  low     1x  size=0.6%  hold=momentum  cats=3
  [2]  SHORT FARTCOIN score=0.474  medium  2x  size=1.2%  hold=funding   cats=4
  [3]  SHORT ONDO    score=0.364  medium  2x  size=0.9%  hold=momentum  cats=5

  Pick [1-3] or q to quit: 2

  RECON — SHORT FARTCOIN
  ● Starting tape confirmation (120s)...
  ● Tape confirmed SHORT — imbalance -0.73 (185 trades)
  ● Executing SHORT FARTCOIN $240 @ $0.22718
  ● Stop: $0.23120 | Target: $0.21923
  ● Monitoring (funding hold, max 8h)...
  ...
  ● Target hit at $0.21930
  ● Outcome recorded to signal memory (+3.47%)

  ╔═══════════════════════════════════════╗
  ║  RIFT RECON                           ║
  ║  SHORT FARTCOIN (MEDIUM)              ║
  ║  2x | funding                         ║
  ║  Entry:    $0.22718                    ║
  ║  Exit:     $0.21930 (target)           ║
  ║  P&L:      +$8.34 (+3.5%)             ║
  ║  Funding:  +$1.20                      ║
  ║  nexstone.io/rift                     ║
  ╚═══════════════════════════════════════╝
```

### Architecture

```
Scout scan_market()
    ↓ returns list[Opportunity] with complete mission brief
    ↓
CLI presents numbered picker (stderr)
    ↓ user picks or --auto N
    ↓
run_recon(opportunity)
    │
    ├── Trading Gates (first time only)
    │   ├── Gate 1: Disclaimer acceptance → ~/.rift/accepted_disclaimer
    │   ├── Gate 2: Wallet auth → ~/.rift/api_key (guided setup)
    │   └── Gate 3: Builder fee check → on-chain approval
    │
    ├── Phase A: Setup
    │   ├── Create exchange/info clients
    │   ├── Set leverage from opportunity.leverage
    │   └── Compute size_usd = equity × size_pct × leverage (volume capped at 1% of 24h)
    │
    ├── Phase B: Tape Confirmation (2 min)
    │   ├── Start LiveMarketFeed(coin) — ephemeral daemon
    │   ├── Poll tape imbalance every 5s
    │   ├── Confirm: imbalance agrees with direction + > 10 trades
    │   └── Abort if not confirmed within window
    │
    ├── Phase C1: Pullback Entry (up to 2 min)
    │   ├── Wait for 0.2% price retracement against direction
    │   ├── LONG: wait for dip below confirmation price
    │   ├── SHORT: wait for bounce above confirmation price
    │   └── Timeout → enter at current market price
    │
    ├── Phase C2: Limit-First Execution
    │   ├── Post limit order at current mid (zero slippage)
    │   ├── Wait 30s for fill
    │   ├── If filled → place stop loss separately
    │   ├── If not filled → cancel limit, escalate to IOC market order
    │   └── Sim mode: realistic slippage (0.05%) + fees (0.135% per side)
    │
    ├── Phase D: Monitor (with dynamic stops)
    │   ├── Price tracking, excursion, funding collection
    │   ├── Live tape/CVD in heartbeats from websocket
    │   ├── Dynamic stop management by hold_type (see below)
    │   ├── Stall detection: tighten stop if <0.1% range over 2× staleness
    │   ├── Exit: target hit, stop hit, max hold, stall, or Ctrl+C
    │   └── Max hold: funding=8h, momentum=4h, mean_reversion=30min
    │
    └── Phase E: Close + Record
        ├── Close position with retry (1%, 2%, 3% slippage)
        ├── Record outcome to signal memory (Scout gets smarter)
        ├── Save trade log to ~/.rift/recon/ or ~/.rift/recon_sim/
        ├── Emit shareable card
        └── feed.stop() — banish the daemon
```

### Usage

```bash
rift recon                              # interactive: scan → pick → execute
rift recon --auto 1                     # auto-pick top opportunity
rift recon --no-soak                    # fast scan without soak
rift recon --confirm 300                # 5 min tape confirmation
rift recon --bias-tf 4h --entry-tf 15m  # custom timeframes
rift recon --sim                        # paper trade — no auth, no orders, no risk
rift recon --sim --auto 1 --no-soak     # fast sim of top pick
```

### Sim mode (`--sim`)

Paper trades against live Hyperliquid prices without placing real orders. Same full pipeline — soak, scan, tape confirmation, stop/target monitoring, funding tracking — just no exchange interaction.

**What sim mode does:**
- Skips all trading gates (no disclaimer, no auth, no builder fee)
- Fills at mid price instantly (no slippage simulation)
- Checks stop/target locally against live price feed
- Applies real funding rates from the HL API
- Uses $10,000 simulated equity for position sizing
- Saves trade logs to `~/.rift/recon_sim/` (separate from real trades)
- Records outcomes to signal memory (Scout learns from sim trades too)

**What sim mode is for:**
- Building trust — run 10-20 sim trades to see if Scout picks winners before risking real money
- Forward-testing the signal factory on live data (complements the historical backfill)
- Accumulating signal memory data points without financial risk
- After 50+ sim trades, you have statistically meaningful data on Scout's accuracy

### Institutional execution

Recon uses execution techniques from institutional trading desks:

**Pullback entry** — after tape confirms, Recon waits up to 2 minutes for a 0.2% retracement before entering. For a SHORT, it waits for a small bounce. For a LONG, it waits for a small dip. This gives a better entry price and confirms the move has follow-through when the pullback fails and price resumes. If no pullback occurs within the window, it enters at market.

**Limit-first execution** — instead of IOC market orders (1% slippage tolerance), Recon posts a limit order at the current mid price and waits 30 seconds for a fill. Limit fills cost zero slippage. If the limit doesn't fill, it cancels and escalates to an IOC market order. In sim mode, pullback entries get 70% less simulated slippage.

**Dynamic stop management** — the stop adapts based on hold_type:

| Hold Type | Stop Behavior |
|---|---|
| **Momentum** | Move to breakeven after 1× ATR profit. Then trail at peak price minus 1.5× ATR. Locks in profit as the trend extends. |
| **Funding** | Widen stop to 1.5× ATR after 2 hours if funding is paying. The edge is from funding collection, not price — give more room. |
| **Mean reversion** | Tighten to 0.5× ATR after half the max hold. These are quick trades — cut losers fast. |
| **All types** | If price moves less than 0.1% over 2× staleness window, close on stall. Dead trades tie up capital. |

### Mission brief fields (from Scout)

| Field | How it's computed |
|---|---|
| `leverage` | High confidence + score ≥ 0.5 → 3x. Medium + ≥ 0.35 → 2x. Else 1x. Max 3x. |
| `size_pct` | Half-Kelly from signal memory hit rate × confluence multiplier (0.5-1.5x). Floor 0.5%, cap 5%. |
| `hold_type` | "funding" if 2+ funding/seasonality signals. "mean_reversion" if 2+ volatility signals. Else "momentum". |
| `staleness_minutes` | Funding: 60min. Momentum: 15min. Mean reversion: 5min. Halved if realtime signals present. |
| `confidence_tier` | High: 5+ categories + >60% hit rate. Medium: 4+ categories. Low: 3 categories. |

### Feedback loop

Every Recon trade outcome is recorded to `~/.rift/signal_memory.jsonl`. This is the feedback loop that makes Scout smarter over time:

1. Scout uses signal memory to kill bad combos (< 45% hit rate)
2. Scout boosts proven combos (> 60% hit rate)
3. Kelly sizing adapts to actual win rate and avg P&L
4. More trades = more data = better filtering = higher edge

### Key files

| File | Purpose |
|---|---|
| `src/rift/recon.py` | Recon executor — confirm, execute, monitor, report |
| `src/rift/scout.py` | Scout scanner — bias + entry + mission brief |
| `src/rift/ws_feed.py` | `LiveMarketFeed` (single coin) + `MultiCoinFeed` (soak) |
| `src/rift/signal_memory.py` | Hit rates, Kelly sizing, outcome recording |
| `src/rift/signals/aggregator.py` | Opportunity dataclass with mission brief fields |
| `src/rift/trading_gates.py` | Disclaimer, auth, builder fee checks |

---

## Trading Gates

Before any real trade executes — via Recon, live trading, or manual trade — three safety gates fire in order. All gates are persistent: returning users pass through instantly.

### Gate 1: Disclaimer

```
  ⚠  TRADING DISCLAIMER

  You are about to trade real funds on Hyperliquid.
  RIFT is experimental open-source software.
  You can lose your entire position.

  Accept and continue? [y/N]:
```

Acceptance saved to `~/.rift/accepted_disclaimer`. Shared across all commands. Once accepted, never prompts again.

### Gate 2: Wallet Auth

```
  🔑  WALLET SETUP

  1. Go to app.hyperliquid.xyz → API → Create API Wallet
  2. Copy the private key (starts with 0x)
  3. Paste it below

  API wallet private key (0x...):
  Main wallet address (or Enter to use derived):
```

Saved to `~/.rift/api_key` with `chmod 600`. Also sets `RIFT_API_KEY` environment variable for the session.

**CLI commands:**
```bash
rift auth setup    # guided wallet key setup
rift auth status   # show current auth state (key masked)
rift auth clear    # remove saved key
```

### Gate 3: Builder Fee

Checks on-chain whether the user's wallet has approved RIFT's 0.1% builder fee. If not approved, directs user to run:

```bash
rift approve-builder-fee <main-wallet-private-key>
```

This is a one-time on-chain transaction signed by the main wallet (not the API wallet). Always required (RIFT is mainnet-only).

### Where gates fire

| Command | Disclaimer | Auth | Builder Fee |
|---|---|---|---|
| `rift scout` | No | No | No |
| `rift recon` | Yes (after pick) | Yes | Yes |
| `rift live` | Yes | Yes | Yes |
| `rift manual-trade` | Yes | Yes | Yes |
| `rift sim` | No (paper trading) | No | No |

Scout is ungated — anyone can scan the market freely. Gates only fire when real money is at risk.

### Implementation

All gates live in `src/rift/trading_gates.py`. The combined check:

```python
from rift.trading_gates import require_trading_ready

result = require_trading_ready()
if result is None:
    return  # user declined or setup incomplete
private_key, account_address = result
```

---

## Trade Logging

Every Recon trade saves a JSON session log to `~/.rift/recon/`:

```
~/.rift/recon/20260505_235030_FARTCOIN_short.json
```

Contains the complete trade record:

| Field | Description |
|---|---|
| `coin`, `direction` | What was traded |
| `entry_price`, `exit_price` | Fill prices |
| `pnl_usd`, `pnl_pct` | Profit/loss |
| `funding_collected` | Funding payments received/paid |
| `exit_reason` | "target", "stop_loss", "max_hold", "user" |
| `leverage`, `size_pct` | Position sizing from mission brief |
| `hold_type`, `confidence_tier` | Trade classification |
| `score`, `num_categories` | Signal factory metrics |
| `signal_names` | Which signals fired |
| `hit_rate` | Historical hit rate at entry |
| `hold_minutes` | How long the position was held |
| `max_favorable`, `max_adverse` | Peak excursion (MFE/MAE) |
| `initial_equity`, `final_equity` | Account state before/after |
| `started_at`, `ended_at` | Timestamps |

Trade outcomes are also recorded to `~/.rift/signal_memory.jsonl` for the feedback loop.
