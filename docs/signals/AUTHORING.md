# Authoring custom scout signals

RIFT's scout aggregates evidence from a stack of signals to score opportunities. The 9 built-in categories ship with the framework (funding, momentum, microstructure, volatility, cross-pair, seasonality, computed, hyperstats, realtime). You can add your own.

This doc walks through writing a custom signal, registering it, and using it from `rift scout`.

---

## TL;DR

```bash
# Scaffold a new signal
rift new my_signal --type signal

# Edit the generated file
$EDITOR strategies/signals/my_signal.py

# Verify it loads + fires
rift scout --top 5 --min 1 --no-soak
```

Your signal appears alongside the built-ins in every `rift scout` invocation.

---

## Where signals live

Scout looks in two directories at scan time:

| Path | Purpose |
|---|---|
| `<repo>/strategies/signals/*.py` | Repo-bundled custom signals. Committed to git. Loaded by both the repo's clone and any forked copy. |
| `~/.rift/signals/*.py` | Per-user signals. Not committed. Live only on your machine. |

Filenames starting with `_` are skipped (use this for partial drafts, shared helpers, or anything you don't want auto-discovered). Filenames must start with a letter and contain only `[A-Za-z0-9_]`.

---

## The signal API

A signal is a function decorated with `@signal(...)` that returns a `SignalResult`. Public import surface:

```python
from rift_strategies_sdk import signal, SignalResult
```

That's the whole authoring surface — no internals needed.

### `SignalResult` fields

```python
@dataclass
class SignalResult:
    name: str         # signal name, must match the @signal decorator's name
    score: float      # range [-1.0, +1.0]
                      #   +1.0 = strong LONG conviction
                      #    0.0 = no opinion (silently dropped by aggregator)
                      #   -1.0 = strong SHORT conviction
    reason: str       # human-readable explanation — shown in `rift scout` output
    category: str     # one of: funding, momentum, microstructure,
                      #         volatility, cross_pair, seasonality
                      # determines which "agreeing categories" bucket this counts toward
    confidence: float # range [0.0, 1.0] — how reliable this signal has been
                      # historically. Used as a weight multiplier in aggregation.
```

### `@signal` decorator

```python
@signal(
    name: str,           # must match SignalResult.name
    category: str,       # see SignalResult.category
    description: str,    # one-liner that surfaces in `rift signal-stats`
    weight: float = 1.0  # base weight in aggregation (multiplied by confidence)
)
def my_signal(coin: str, state: dict) -> SignalResult:
    ...
```

### Function signature

Every signal function takes `(coin, state)` and returns `SignalResult`:

- **`coin`** — ticker symbol like `"BTC"`, `"ETH"`. Always uppercase.
- **`state`** — dict of available market context. Common keys:

| Key | Type | Description |
|---|---|---|
| `mid_price` | float | Current mid price |
| `funding_rate` | float | Current 1h funding rate (e.g. `0.0001` = 0.01%) |
| `predicted_funding` | float | Next-settlement predicted funding |
| `open_interest` | float | USD-denominated open interest |
| `oracle_price` | float | Oracle price (may differ from mid for funding calc) |
| `premium` | float | Mark - oracle (positive = premium) |
| `atr_pct` | float | 14-period ATR as fraction of mid |
| `day_volume` | float | 24h USD volume |
| `cvd` | float | Cumulative volume delta |
| `volume_delta` | float | Last-bar buy minus sell volume |
| `relative_volume` | float | Current bucket vs rolling average |
| `candles_1h` | list | List of `{o,h,l,c,v,t}` bars on the bias timeframe |
| `candles_5m` | list | List of `{o,h,l,c,v,t}` bars on the entry timeframe |
| `indicators` | dict | Pre-computed indicators (EMAs, RSI, etc.) when available |
| `tape` | dict | Recent tape stats (when websocket soak data is present) |
| `orderflow` | dict | L2-book derived stats |

Any value may be missing. Use `state.get("key", default)` defensively.

---

## A worked example

A simple momentum-divergence signal that fires when price is making new highs but volume is falling off:

```python
"""volume_divergence — fires when price highs aren't backed by rising volume."""

from __future__ import annotations

from rift_strategies_sdk import signal, SignalResult


@signal(
    name="volume_divergence",
    category="momentum",
    description="Price making highs / lows on falling volume — momentum exhaustion",
    weight=1.1,
)
def volume_divergence(coin: str, state: dict) -> SignalResult:
    candles = state.get("candles_1h") or []
    if len(candles) < 20:
        return SignalResult(
            name="volume_divergence", score=0.0,
            reason="Insufficient history",
            category="momentum", confidence=0.0,
        )

    recent = candles[-5:]
    earlier = candles[-20:-5]

    recent_high = max(float(c["h"]) for c in recent)
    earlier_high = max(float(c["h"]) for c in earlier)
    recent_vol = sum(float(c["v"]) for c in recent) / len(recent)
    earlier_vol = sum(float(c["v"]) for c in earlier) / len(earlier)

    # Price making highs but volume fading → bearish divergence
    if recent_high > earlier_high and recent_vol < earlier_vol * 0.7:
        strength = (earlier_vol - recent_vol) / earlier_vol
        return SignalResult(
            name="volume_divergence",
            score=-min(1.0, strength * 2),  # negative = short signal
            reason=f"New highs on {(recent_vol/earlier_vol)*100:.0f}% of prior volume",
            category="momentum",
            confidence=0.55,
        )

    # Same pattern but inverted (price lows on falling volume = bullish exhaustion)
    recent_low = min(float(c["l"]) for c in recent)
    earlier_low = min(float(c["l"]) for c in earlier)
    if recent_low < earlier_low and recent_vol < earlier_vol * 0.7:
        strength = (earlier_vol - recent_vol) / earlier_vol
        return SignalResult(
            name="volume_divergence",
            score=+min(1.0, strength * 2),
            reason=f"New lows on {(recent_vol/earlier_vol)*100:.0f}% of prior volume",
            category="momentum",
            confidence=0.55,
        )

    return SignalResult(
        name="volume_divergence", score=0.0,
        reason="No divergence pattern",
        category="momentum", confidence=0.0,
    )
```

Save as `strategies/signals/volume_divergence.py`. Run `rift scout` — your signal now contributes to every opportunity's score.

---

## Rules of the road

### Return `score=0.0` when you have no opinion

A score of exactly `0.0` is silently dropped by the aggregator. Use this for warmup, missing data, or "no pattern matched right now." Don't return random noise.

### Score magnitude should reflect conviction, not arbitrary scaling

`±1.0` = "I'm as confident as I can be." `±0.3` = "lean this way." `0.0` = "no read." The aggregator weighs your score multiplied by your `confidence` — don't double-discount by capping at `0.5` if you really mean `1.0`.

### Set `confidence` based on what you actually know

`confidence` is your honest estimate of how reliable this signal has been. If you backtested it and saw 60% hit rate, set `confidence=0.6`. If you wrote it last night and have no data, set `confidence=0.3` (conservative). The aggregator uses this to weight your signal against others — overstating it inflates your influence dishonestly.

### Categories matter for scout's "agreeing categories" filter

Scout requires opportunities to have signals from **at least 3 independent categories** agreeing on direction. If you put your signal in the wrong category, you may inflate confluence inappropriately. Pick the category that best describes the *evidence type* (e.g. a volume-based signal is `microstructure`, not `momentum`).

### Wrap defensive code

The aggregator catches exceptions from individual signals and continues — a single broken signal won't crash scout. But this means **your bugs are silent.** Use `try/except` sparingly inside your signal; let real errors surface in dev rather than masking them.

### Don't reach into the registry directly

The `@signal` decorator handles registration. Don't import `_SIGNAL_REGISTRY` or mutate it. If you need conditional registration, control it at module-load time (e.g., `if some_condition: @signal(...) def ...`).

---

## Iteration workflow

```bash
# Author
$EDITOR strategies/signals/my_signal.py

# Smoke — confirm it loads + fires
rift scout --top 5 --min 1 --no-soak

# Inspect its output
rift scout --top 50 --min 1 --no-soak | jq '.opportunities[] | .signals[] | select(.name == "my_signal")'

# See how often it fires across recent scans
rift signal-stats

# Once you're happy, commit it (if it's in strategies/signals/)
git add strategies/signals/my_signal.py
git commit -m "Add my_signal scout signal"

# Or keep it personal in ~/.rift/signals/
cp strategies/signals/my_signal.py ~/.rift/signals/
rm strategies/signals/my_signal.py
```

---

## What doesn't get auto-loaded

- Files starting with `_` (e.g. `_helpers.py`) — load these explicitly via `import` inside another signal file if you want shared utilities.
- Files in subdirectories — scout only scans the top level of `strategies/signals/` and `~/.rift/signals/`. If you want a signal to apply to scout, put it directly in those dirs.
- Files with non-letter starting characters (e.g. `1_aroon.py`) — match `[A-Za-z][A-Za-z0-9_]*` or the loader skips them.

---

## What about backtesting?

User-authored signals are picked up by scout but the `rift research` and `rift backtest` flows don't currently re-discover them on every run. To validate a signal's historical edge, write a strategy that uses it explicitly (see [`docs/strategies/AUTHORING.md`](../strategies/AUTHORING.md)) — that's the validation surface RIFT enforces.

The wallet-validation feature in the Nexstone terminal is the production-grade alternative: it tracks every signal's realized hit rate across thousands of opportunities and applies time-decayed Bayesian-shrunk calibration. RIFT-OSS gives you the authoring surface; the terminal gives you the long-term outcome accounting.
