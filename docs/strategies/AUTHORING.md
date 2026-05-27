# Authoring a strategy

This doc walks through writing a strategy from scratch with the RIFT SDK, using `trend_follow` as the reference. Read [`AUTH_AND_EXECUTION.md`](../AUTH_AND_EXECUTION.md) first if you plan to deploy live.

---

## The shape of a strategy

A RIFT strategy is one class that inherits from `Strategy`, declares its indicators, and emits `Signal` objects from `on_candle`. That's it.

```python
from rift_engine.strategy import (
    EMA, Candle, Indicator, Param, Signal, Strategy, StrategyState, register,
)
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True)
class MyConfig:
    fast: Annotated[int, Param("Fast EMA", min=5, max=50, step=5)] = 12
    slow: Annotated[int, Param("Slow EMA", min=20, max=200, step=10)] = 26
    sl_pct: Annotated[float, Param("Stop loss %", min=0.005, max=0.05, step=0.005)] = 0.02


@register("my_strategy")
class MyStrategy(Strategy):
    """One-line description shown in `rift strategies list`."""

    config_class = MyConfig
    default_interval = "1h"

    def indicators(self) -> dict[str, Indicator]:
        return {
            "ema_fast": EMA(period=self.config.fast),
            "ema_slow": EMA(period=self.config.slow),
        }

    def on_candle(self, candle: Candle, state: StrategyState) -> Signal | None:
        fast = state.indicators.get("ema_fast", float("nan"))
        slow = state.indicators.get("ema_slow", float("nan"))

        import math
        if math.isnan(fast) or math.isnan(slow):
            return None  # warmup

        if fast > slow and state.position == 0:
            return Signal.long(size=0.2, sl=candle.close * (1 - self.config.sl_pct))
        if fast < slow and state.position > 0:
            return Signal.close()
        return None
```

Save as `strategies/my_strategy.py`. Run `rift strategies list` — it appears. Run `rift research my_strategy --pair BTC --tf 1h` — the full validation pipeline executes.

**Important:** `state.position` is a signed float, not a `Position` object. Positive = long, negative = short, zero = flat. Don't try to dereference `state.position.side` — that's a different attribute (`Position.side`) on the algo-loop's internal position object, not what the SDK passes you.

---

## The five pieces

### 1. Config dataclass

Defines tunable parameters. `Param(...)` annotations are picked up by `rift sweep` (parameter sweep), `rift smart-sweep` (Bayesian-optimized), and the workbench UI.

```python
@dataclass(frozen=True)
class MyConfig:
    threshold: Annotated[float, Param("Entry threshold", min=0.5, max=2.0, step=0.1)] = 1.0
```

Use `frozen=True` so the config is hashable — RIFT keys cache lookups on config hashes for reproducibility.

### 2. `config_class`

Tells the framework which dataclass to instantiate. RIFT will pass an instance to `self.config`.

### 3. `default_interval`

The timeframe this strategy is built for. `rift algo my_strategy --pair BTC` uses this if no `--tf` is provided.

### 4. `indicators()`

Returns a dict of indicators the framework will compute incrementally on every candle. Available primitives in `rift_engine.strategy`:

- **`EMA(period)`** / **`SMA(period)`** — moving averages
- **`RSI(period)`** / **`MACD(fast, slow, signal)`** / **`BBANDS(period, std)`** — oscillators / bands
- **`ATR(period)`** — average true range
- **`CCI(period)`** / **`ADX(period)`** / **`AROON(period)`** — trend strength
- **`OBV()`** / **`KAMA(period, fast, slow)`** / **`HURST(window)`** — flow / regime

Or compose your own: any `Indicator` subclass with an `update(candle) -> float` method works.

### 5. `on_candle(candle, state) -> Signal | None`

Called once per closed candle. Return a `Signal` or `None`. Available signal shapes:

```python
Signal.long(size=0.2)                            # market long, 20% of equity
Signal.long(size=0.2, sl=42000)                  # with stop loss price
Signal.long(size=0.2, sl=42000, tp=46000)        # with take-profit
Signal.short(size=0.15)                          # market short
Signal.close()                                   # close current position
Signal.close(pct=0.5)                            # close half (partial exit)
```

`size` is the fraction of equity to deploy. `0.2` means 20%. The portfolio supervisor (if running) may scale this down via the gate file.

`state.indicators` is a dict of the latest indicator values keyed by the names you returned from `indicators()`. `state.position` is a **signed float** — positive means you're long, negative means you're short, zero means you're flat. `state.equity` is current equity.

Common patterns:

```python
# Are we in any position?
if state.position != 0:
    ...

# Are we long?
if state.position > 0:
    ...

# Are we flat?
if state.position == 0:
    ...
```

---

## Required hygiene

### Warmup

Indicators need history. The first N candles produce `nan` until enough data has accumulated. Always guard against `nan` at the top of `on_candle`:

```python
import math
if math.isnan(fast) or math.isnan(slow):
    return None
```

### Promotion gates

If you want your strategy to pass through `rift research` validation, declare your gates explicitly. RIFT's defaults are conservative; declare looser ones if your strategy's nature demands them — but be honest:

```python
@register("my_strategy")
class MyStrategy(Strategy):
    promotion_gates = {
        "min_dsr": 0.85,           # Deflated Sharpe — defeat multiple-testing inflation
        "min_cv_pass_rate": 0.70,  # Purged k-fold CV pass rate
        "min_sharpe_per_fold": 0.0,
        "min_capacity_usd": 10_000.0,
        "min_observations": 1000,
        "min_trades": 25,
        "max_dd_pct": 0.25,
    }
```

A slow trend-follower legitimately makes 30–50 trades over 2 years. Setting `min_trades=25` is honest. Setting `min_trades=5` because your strategy only fired once on a cherry-picked year is dishonest — and `rift research` will tell you so.

### Walk-forward window sizes

Tell the framework what's appropriate for your strategy's natural cadence:

```python
class MyStrategy(Strategy):
    recommended_train_months = 6
    recommended_test_months = 3
```

If your strategy needs 200 candles of warmup on 4h candles, that's ~33 days. Don't expect a 1-month train window to produce stable parameters.

---

## What NOT to do

These will get your strategy rejected by `rift research` or, worse, silently produce overfit garbage:

- **Don't peek**: `state` only contains data up to the candle you're being called with. Don't reference `state.candles[i+1]`. The framework doesn't expose forward data; if you find a way to, you're using the wrong API.
- **Don't fit on test data**: the framework's walk-forward splits the data for you. If you write logic that says "trade only when X happened in the test period," your backtest will look amazing and your live trade will lose money.
- **Don't ignore funding**: HL perps charge funding every hour. Strategies that hold for hours-days need to account for it. The framework adds funding to your P&L automatically — but if your edge is smaller than the funding cost, you have no edge.
- **Don't hardcode coin-specific magic numbers**: if your strategy only works on BTC because you tuned thresholds for BTC's volatility, declare that in your docstring AND in `COIN_CONFIGS`:

```python
COIN_CONFIGS = {"BTC": MyConfig(threshold=1.0), "ETH": MyConfig(threshold=1.4)}
```

`rift algo my_strategy --pair ETH` will pick up the ETH-specific config.

---

## Iteration workflow

```bash
# 1. Write the strategy
$EDITOR strategies/my_strategy.py

# 2. Quick smoke — fail fast if it crashes
rift quick-test my_strategy --pair BTC

# 3. Full backtest
rift backtest my_strategy --pair BTC --tf 1h

# 4. If the backtest looks good — validate honestly
rift research my_strategy --pair BTC --tf 1h

# 5. Sweep parameters if research grades poorly but you suspect there's edge
rift sweep my_strategy --pair BTC --tf 1h
rift smart-sweep my_strategy --pair BTC --tf 1h    # Bayesian, faster

# 6. Walk-forward stability check — does the strategy survive on data it hasn't seen?
rift walk-forward my_strategy --pair BTC --tf 1h

# 7. Multi-pair check — does it work on more than the coin you tuned for?
rift verify my_strategy --pair ETH --tf 1h
rift verify my_strategy --pair SOL --tf 1h
```

Iterate. The framework will tell you when your strategy is overfit. Listen to it.

---

## Going live

If `rift research my_strategy` passes 5/5 promotion gates AND the walk-forward analysis shows >70% profitable windows AND the strategy reproduces on at least 2 other coins, you have something worth small live capital.

```bash
# Make sure auth + builder fee are set up (one-time)
rift auth setup
rift approve-builder-fee

# Run live (real money)
rift algo my_strategy --pair BTC

# Or in a portfolio with risk controls
$EDITOR strategies/configs/my_portfolio.yaml
rift portfolio-start strategies/configs/my_portfolio.yaml
```

Read [`AUTH_AND_EXECUTION.md`](../AUTH_AND_EXECUTION.md) and [`KNOWN_ISSUES.md`](../../KNOWN_ISSUES.md) before risking real money.

---

## Reference

The bundled `trend_follow` is intentionally small (~15 lines of logic). Open it and read every line:

```bash
$EDITOR packages/strategies-sdk/src/rift_strategies_sdk/examples/trend_follow.py
```

The docstring above the class explains:
- What the strategy does (EMA crossover, bidirectional)
- Why it's framed as demo-only (public, decades-old signal)
- What the validated metrics on the bundled dataset are
- What to try modifying to learn the SDK

It's the only strategy that ships, deliberately. The framework is the product. Build your own edge.
