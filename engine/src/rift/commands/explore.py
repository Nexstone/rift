"""Research Lab → EXPLORE commands.

Five commands powering the EXPLORE submenu of the Research Lab:

  rift indicators              browseable catalog of 50+ Indicator classes
  rift funding-browser         current funding + 7d history + extremes
  rift order-flow              taker ratio, buy/sell imbalance, position flow
  rift cross-asset             correlation matrix, lead-lag, beta vs BTC
  rift regime                  current vol + trend regime, historical breakdown

All emit NDJSON via _emit() so the TS Research Lab UI can render structured
results. Designed to operate on already-cached data when possible (no HL
network unless explicitly needed).
"""

from __future__ import annotations

import inspect as _inspect
import json
import os
from pathlib import Path
from typing import Any

import typer

from rift.commands._shared import app, _emit


# ─── rift indicators ─────────────────────────────────────────────────

# Hand-curated category map. The Indicator classes themselves don't carry
# category metadata; the source file uses section headers to group them.
# When new indicators are added to rift_engine/strategy.py, add them here.
_INDICATOR_CATEGORIES: dict[str, list[str]] = {
    "trend": [
        "SMA", "EMA", "HMA", "DEMA", "TEMA",
        "Supertrend", "ParabolicSAR", "AroonUp", "AroonDown",
        "LinRegSlope",
        "IchimokuTenkan", "IchimokuKijun", "IchimokuSenkouA", "IchimokuSenkouB",
    ],
    "momentum": [
        "RSI", "MACD", "MACDSignal", "MACDHistogram",
        "StochK", "StochD", "WilliamsR", "CCI", "ROC", "MFI",
        "ADX", "PlusDI", "MinusDI",
    ],
    "volatility": [
        "ATR", "ATR_SMA",
        "BollingerBands", "BBUpper", "BBLower", "BBWidth",
        "KeltnerUpper", "KeltnerLower",
        "DonchianUpper", "DonchianLower",
        "StdDev", "HistVol",
    ],
    "volume": ["OBV", "CMF", "VWAP", "VWAPStd", "VolRatio"],
    "structure": ["SwingHigh", "SwingLow", "PivotPoint"],
    "adaptive": ["KAMA", "AdaptiveRSI", "AdaptiveEMA", "VolatilityRegime"],
    "multi_timeframe": ["HTF"],
    "cross_asset": ["CrossAsset", "CrossCorrelation", "CrossBeta", "CrossLeadLag"],
    "order_flow": [
        "TakerRatio", "BuySellImbalance", "PositionFlow", "PnLFlow", "TradeIntensity",
    ],
}


def _describe_indicator(cls: type) -> dict:
    """Build a JSON description of one Indicator subclass."""
    doc = (cls.__doc__ or "").strip().split("\n")[0]
    sig = _inspect.signature(cls.__init__)
    params: list[dict] = []
    for name, p in sig.parameters.items():
        if name == "self":
            continue
        default: Any
        if p.default is _inspect.Parameter.empty:
            default = None
        elif hasattr(p.default, "params"):  # Indicator instance (HTF.inner)
            default = type(p.default).__name__
        else:
            default = p.default
        params.append({"name": name, "default": default})
    return {
        "name": cls.__name__,
        "description": doc,
        "params": params,
    }


@app.command("indicators")
def indicators_cmd(
    category: str = typer.Option(
        "", "--category",
        help="Filter to one category: trend, momentum, volatility, volume, structure, "
             "adaptive, multi_timeframe, cross_asset, order_flow",
    ),
    search: str = typer.Option("", "--search", help="Case-insensitive substring match on name or description"),
) -> None:
    """Browse the indicator catalog (every Indicator class shipped with the engine)."""
    from rift_engine import strategy as _strat

    categorized: dict[str, list[dict]] = {}
    seen: set[str] = set()
    total = 0

    cats_to_show = [category] if category else list(_INDICATOR_CATEGORIES.keys())
    for cat in cats_to_show:
        if cat not in _INDICATOR_CATEGORIES:
            _emit({"type": "error", "msg": f"Unknown category '{cat}'. Valid: {list(_INDICATOR_CATEGORIES.keys())}"})
            raise typer.Exit(code=1)
        items: list[dict] = []
        for name in _INDICATOR_CATEGORIES[cat]:
            cls = getattr(_strat, name, None)
            if cls is None:
                # Indicator listed in the category map but not present in strategy.py.
                # Surface so the map can be updated.
                items.append({"name": name, "description": "(missing from rift_engine.strategy)", "params": []})
                seen.add(name)
                total += 1
                continue
            d = _describe_indicator(cls)
            if search and search.lower() not in d["name"].lower() and search.lower() not in d["description"].lower():
                continue
            items.append(d)
            seen.add(name)
            total += 1
        if items:
            categorized[cat] = items

    # Cross-check: any Indicator subclass present in strategy.py but missing
    # from the category map? Only worth surfacing when showing the full catalog;
    # when a single category is filtered, "uncategorized" would otherwise just
    # mirror every indicator outside that filter.
    uncategorized: list[dict] = []
    if not category:
        # Build the full set of mapped names across ALL categories.
        all_mapped = {n for names in _INDICATOR_CATEGORIES.values() for n in names}
        for attr_name in dir(_strat):
            if attr_name in all_mapped or attr_name.startswith("_"):
                continue
            obj = getattr(_strat, attr_name)
            if isinstance(obj, type) and issubclass(obj, _strat.Indicator) and obj is not _strat.Indicator:
                uncategorized.append(_describe_indicator(obj))

    _emit({
        "type": "result", "command": "indicators",
        "filter": {"category": category or "all", "search": search or None},
        "total": total,
        "categories": categorized,
        "uncategorized": uncategorized,
    })


# ─── rift funding-browser ────────────────────────────────────────────

@app.command("funding-browser")
def funding_browser_cmd(
    coins: str = typer.Option("", "--coins", help="Comma-separated coin list (default: all cached coins)"),
    top: int = typer.Option(20, "--top", help="Number of coins to show, ranked by absolute current funding"),
    lookback_days: int = typer.Option(7, "--days", help="History window for stats (default 7d)"),
) -> None:
    """Browse funding rates across coins — current + window stats + extremes.

    Pulls from ~/.rift/data/<COIN>/funding/rates.parquet (cached via
    `rift sync`). Skips coins without a funding cache.
    """
    import polars as pl
    import time

    data_dir = Path.home() / ".rift" / "data"
    if not data_dir.exists():
        _emit({"type": "result", "command": "funding-browser", "coins": [], "msg": "No cached data. Run: rift sync"})
        return

    requested: set[str] | None = None
    if coins:
        requested = {c.strip().upper() for c in coins.split(",") if c.strip()}

    now_ms = int(time.time() * 1000)
    window_ms = lookback_days * 24 * 60 * 60 * 1000
    cutoff_ms = now_ms - window_ms

    rows: list[dict] = []
    for coin_dir in sorted(data_dir.iterdir()):
        if not coin_dir.is_dir() or coin_dir.name.startswith("_"):
            continue
        coin = coin_dir.name
        if requested is not None and coin not in requested:
            continue
        funding_file = coin_dir / "funding" / "rates.parquet"
        if not funding_file.exists():
            continue
        try:
            df = pl.read_parquet(funding_file)
            if len(df) == 0:
                continue
            df = df.sort("timestamp")
            current_rate = float(df["funding_rate"][-1])
            recent = df.filter(pl.col("timestamp") >= cutoff_ms)
            if len(recent) == 0:
                continue
            rates = recent["funding_rate"].to_numpy()
            mean_rate = float(rates.mean())
            max_rate = float(rates.max())
            min_rate = float(rates.min())
            std = float(rates.std()) if len(rates) > 1 else 0.0
            zscore = (current_rate - mean_rate) / std if std > 1e-12 else 0.0
            rows.append({
                "coin": coin,
                "current_rate": current_rate,
                "current_pct_per_hour": current_rate * 100,
                "mean_rate": mean_rate,
                "max_rate": max_rate,
                "min_rate": min_rate,
                "std": std,
                "zscore": zscore,
                "samples": len(recent),
            })
        except Exception as e:
            _emit({"type": "warning", "msg": f"Skipping {coin}: {e}"})
            continue

    # Sort by absolute current rate (most extreme first)
    rows.sort(key=lambda r: abs(r["current_rate"]), reverse=True)
    rows = rows[:top]

    _emit({
        "type": "result", "command": "funding-browser",
        "lookback_days": lookback_days,
        "as_of_ms": now_ms,
        "coins": rows,
    })


# ─── rift order-flow ─────────────────────────────────────────────────

@app.command("order-flow")
def order_flow_cmd(
    coins: str = typer.Option("", "--coins", help="Comma-separated coin list (default: cached coins with fill data)"),
    top: int = typer.Option(20, "--top", help="Number of coins to show, ranked by absolute buy/sell imbalance"),
    lookback_hours: int = typer.Option(24, "--hours", help="History window for aggregation"),
) -> None:
    """Aggregate ground-truth taker / buy-sell flow per coin.

    Reads cached fill parquets at ~/.rift/data/<COIN>/<YYYY-MM-DD>.parquet
    (produced by `rift sync`). Returns top N by absolute imbalance.
    """
    import polars as pl
    import time
    from datetime import datetime, timedelta, timezone

    data_dir = Path.home() / ".rift" / "data"
    if not data_dir.exists():
        _emit({"type": "result", "command": "order-flow", "coins": [], "msg": "No cached data. Run: rift sync"})
        return

    requested: set[str] | None = None
    if coins:
        requested = {c.strip().upper() for c in coins.split(",") if c.strip()}

    now = datetime.now(timezone.utc)
    cutoff_ms = int((now - timedelta(hours=lookback_hours)).timestamp() * 1000)

    rows: list[dict] = []
    for coin_dir in sorted(data_dir.iterdir()):
        if not coin_dir.is_dir() or coin_dir.name.startswith("_"):
            continue
        coin = coin_dir.name
        if requested is not None and coin not in requested:
            continue
        # Fill files at <COIN>/fills/YYYYMMDD.parquet (produced by `rift sync`)
        fills_dir = coin_dir / "fills"
        if not fills_dir.is_dir():
            continue
        fill_files = sorted(fills_dir.glob("????????.parquet"))
        if not fill_files:
            continue
        # Take enough recent days to cover the lookback (+2 safety for boundary)
        recent_files = fill_files[-(lookback_hours // 24 + 2):]
        try:
            dfs = [pl.read_parquet(f) for f in recent_files]
            if not dfs:
                continue
            df = pl.concat(dfs)
            df = df.filter(pl.col("timestamp") >= cutoff_ms)
            if len(df) == 0:
                continue

            # Buy = side B, Sell = side A
            buy_vol = float(df.filter(pl.col("side") == "B")["size"].sum())
            sell_vol = float(df.filter(pl.col("side") == "A")["size"].sum())
            total_vol = buy_vol + sell_vol
            if total_vol < 1e-9:
                continue
            imbalance = (buy_vol - sell_vol) / total_vol  # range [-1, +1]

            # Taker ratio: fraction of fills that crossed the spread (= aggressor)
            # Field 'crossed' = True when the trade was an aggressive market hit.
            if "crossed" in df.columns:
                taker_ratio = float(df["crossed"].mean())
            else:
                taker_ratio = float("nan")

            # Position flow approximation: opens - closes
            # 'is_open' = True if the fill opened/increased a position.
            if "is_open" in df.columns:
                opens = float(df.filter(pl.col("is_open"))["size"].sum())
                closes = float(df.filter(~pl.col("is_open"))["size"].sum())
                net_flow = opens - closes
            else:
                opens = closes = net_flow = float("nan")

            rows.append({
                "coin": coin,
                "fills": len(df),
                "buy_volume": buy_vol,
                "sell_volume": sell_vol,
                "total_volume": total_vol,
                "imbalance": imbalance,
                "imbalance_pct": imbalance * 100,
                "taker_ratio": taker_ratio,
                "opens": opens,
                "closes": closes,
                "net_flow": net_flow,
            })
        except Exception as e:
            _emit({"type": "warning", "msg": f"Skipping {coin}: {e}"})
            continue

    rows.sort(key=lambda r: abs(r["imbalance"]), reverse=True)
    rows = rows[:top]

    _emit({
        "type": "result", "command": "order-flow",
        "lookback_hours": lookback_hours,
        "as_of_ms": int(now.timestamp() * 1000),
        "coins": rows,
    })


# ─── rift cross-asset ────────────────────────────────────────────────

@app.command("cross-asset")
def cross_asset_cmd(
    coins: str = typer.Option(
        "BTC,ETH,SOL,SUI,AVAX,NEAR,LINK,DOGE", "--coins",
        help="Comma-separated coin list to include in the matrix",
    ),
    tf: str = typer.Option("1h", "--tf", help="Timeframe for candle data"),
    lookback_candles: int = typer.Option(720, "--lookback", help="Candles to use (720 1h = 30 days)"),
    benchmark: str = typer.Option("BTC", "--benchmark", help="Beta-vs-benchmark coin"),
    max_lag: int = typer.Option(6, "--max-lag", help="Lead-lag search window (candles)"),
) -> None:
    """Cross-asset correlation matrix + lead-lag + beta-vs-benchmark.

    Reads cached candles per coin. Outputs:
      - corr: NxN correlation matrix of log returns
      - lead_lag: for each non-benchmark coin, best correlation across [-max_lag, +max_lag] candle shifts
      - beta: OLS beta of each coin vs benchmark on log returns
    """
    import numpy as np
    import polars as pl

    coin_list = [c.strip().upper() for c in coins.split(",") if c.strip()]
    benchmark = benchmark.upper()
    if benchmark not in coin_list:
        coin_list.insert(0, benchmark)

    data_dir = Path.home() / ".rift" / "data"

    series: dict[str, np.ndarray] = {}
    skipped: list[dict] = []
    for coin in coin_list:
        candle_file = data_dir / coin / tf / "candles.parquet"
        if not candle_file.exists():
            # try other layouts
            candle_file = data_dir / coin / tf / "data.parquet"
        if not candle_file.exists():
            skipped.append({"coin": coin, "reason": f"no cached {tf} candles"})
            continue
        try:
            df = pl.read_parquet(candle_file).sort("timestamp" if "timestamp" in pl.read_parquet(candle_file).columns else "time")
            close_col = "close" if "close" in df.columns else "c"
            closes = df[close_col].to_numpy().astype(float)
            if len(closes) < lookback_candles + 1:
                skipped.append({"coin": coin, "reason": f"only {len(closes)} candles cached (<{lookback_candles+1} needed)"})
                continue
            recent = closes[-(lookback_candles + 1):]
            log_returns = np.diff(np.log(recent))
            series[coin] = log_returns
        except Exception as e:
            skipped.append({"coin": coin, "reason": str(e)})
            continue

    if benchmark not in series:
        _emit({
            "type": "error",
            "msg": f"Benchmark {benchmark} not available in cached data: {skipped}",
        })
        raise typer.Exit(code=1)

    # Align to same length (some coins may have shorter histories)
    n = min(len(s) for s in series.values())
    aligned = {c: s[-n:] for c, s in series.items()}
    available_coins = list(aligned.keys())

    # Correlation matrix
    matrix = np.zeros((len(available_coins), len(available_coins)))
    for i, ci in enumerate(available_coins):
        for j, cj in enumerate(available_coins):
            if i == j:
                matrix[i][j] = 1.0
            else:
                matrix[i][j] = float(np.corrcoef(aligned[ci], aligned[cj])[0, 1])
    corr_dict: dict[str, dict[str, float]] = {}
    for i, ci in enumerate(available_coins):
        corr_dict[ci] = {cj: round(matrix[i][j], 4) for j, cj in enumerate(available_coins)}

    # Lead-lag against benchmark (positive lag = benchmark leads coin)
    bench_returns = aligned[benchmark]
    lead_lag: list[dict] = []
    for coin in available_coins:
        if coin == benchmark:
            continue
        coin_returns = aligned[coin]
        best_corr = 0.0
        best_lag = 0
        for lag in range(-max_lag, max_lag + 1):
            if lag == 0:
                c = float(np.corrcoef(bench_returns, coin_returns)[0, 1])
            elif lag > 0:
                # benchmark leads: bench_returns[:-lag] vs coin_returns[lag:]
                c = float(np.corrcoef(bench_returns[:-lag], coin_returns[lag:])[0, 1])
            else:
                # coin leads
                k = -lag
                c = float(np.corrcoef(bench_returns[k:], coin_returns[:-k])[0, 1])
            if abs(c) > abs(best_corr):
                best_corr = c
                best_lag = lag
        lead_lag.append({"coin": coin, "best_corr": round(best_corr, 4), "best_lag": best_lag})

    # Beta vs benchmark: cov(coin, bench) / var(bench)
    bench_var = float(np.var(bench_returns))
    betas: list[dict] = []
    for coin in available_coins:
        if coin == benchmark:
            continue
        cov = float(np.cov(aligned[coin], bench_returns)[0, 1])
        beta = cov / bench_var if bench_var > 1e-12 else float("nan")
        betas.append({"coin": coin, "beta": round(beta, 4)})

    _emit({
        "type": "result", "command": "cross-asset",
        "tf": tf,
        "lookback_candles": n,
        "benchmark": benchmark,
        "available_coins": available_coins,
        "skipped": skipped,
        "corr": corr_dict,
        "lead_lag": lead_lag,
        "beta": betas,
    })


# ─── rift regime ─────────────────────────────────────────────────────

@app.command("regime")
def regime_cmd(
    coin: str = typer.Option("BTC", "--coin", help="Coin to classify regime for"),
    tf: str = typer.Option("1h", "--tf", help="Timeframe"),
    lookback_candles: int = typer.Option(720, "--lookback", help="Candles to analyze (720 1h = 30 days)"),
    vol_short: int = typer.Option(14, "--vol-short", help="ATR period"),
    vol_long: int = typer.Option(100, "--vol-long", help="Lookback for vol percentile"),
    trend_period: int = typer.Option(14, "--trend-period", help="ADX period for trend classification"),
) -> None:
    """Classify current vol + trend regime; report historical breakdown.

    Vol regime: ATR percentile vs trailing window
      low      ATR < 33rd percentile
      normal   33rd ≤ ATR < 67th percentile
      high     ATR ≥ 67th percentile

    Trend regime: ADX-based
      bull     ADX > 20 and +DI > -DI
      bear     ADX > 20 and -DI > +DI
      chop     ADX ≤ 20
    """
    import numpy as np
    import polars as pl

    data_dir = Path.home() / ".rift" / "data"
    candle_file = data_dir / coin.upper() / tf / "candles.parquet"
    if not candle_file.exists():
        candle_file = data_dir / coin.upper() / tf / "data.parquet"
    if not candle_file.exists():
        _emit({"type": "error", "msg": f"No cached {coin.upper()} {tf} candles. Run: rift fetch {coin.upper()} --tf {tf}"})
        raise typer.Exit(code=1)

    df = pl.read_parquet(candle_file).sort("timestamp" if "timestamp" in pl.read_parquet(candle_file).columns else "time")
    if len(df) < lookback_candles:
        _emit({"type": "warning", "msg": f"Only {len(df)} cached candles; analysis window reduced from {lookback_candles}."})
        lookback_candles = len(df)
    df = df.tail(lookback_candles)

    high = df["high" if "high" in df.columns else "h"].to_numpy().astype(float)
    low = df["low" if "low" in df.columns else "l"].to_numpy().astype(float)
    close = df["close" if "close" in df.columns else "c"].to_numpy().astype(float)

    # True Range
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])

    # ATR (Wilder smoothing)
    atr = np.zeros_like(tr)
    atr[:vol_short] = tr[:vol_short].mean()
    for i in range(vol_short, len(tr)):
        atr[i] = (atr[i - 1] * (vol_short - 1) + tr[i]) / vol_short

    # Vol regime classification: rolling percentile of ATR
    def _vol_label(i: int) -> str:
        start = max(0, i - vol_long)
        window = atr[start:i + 1]
        if len(window) < 10:
            return "warmup"
        p33 = np.percentile(window, 33)
        p67 = np.percentile(window, 67)
        v = atr[i]
        if v < p33:
            return "low"
        elif v < p67:
            return "normal"
        return "high"

    vol_labels = [_vol_label(i) for i in range(len(atr))]

    # ADX + DI for trend classification
    up_move = np.diff(high, prepend=high[0])
    down_move = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # Smooth DM
    smooth_plus = np.zeros_like(plus_dm)
    smooth_minus = np.zeros_like(minus_dm)
    smooth_tr = np.zeros_like(tr)
    smooth_plus[:trend_period] = plus_dm[:trend_period].sum()
    smooth_minus[:trend_period] = minus_dm[:trend_period].sum()
    smooth_tr[:trend_period] = tr[:trend_period].sum()
    for i in range(trend_period, len(tr)):
        smooth_plus[i] = smooth_plus[i - 1] - (smooth_plus[i - 1] / trend_period) + plus_dm[i]
        smooth_minus[i] = smooth_minus[i - 1] - (smooth_minus[i - 1] / trend_period) + minus_dm[i]
        smooth_tr[i] = smooth_tr[i - 1] - (smooth_tr[i - 1] / trend_period) + tr[i]

    plus_di = np.where(smooth_tr > 0, 100 * smooth_plus / smooth_tr, 0.0)
    minus_di = np.where(smooth_tr > 0, 100 * smooth_minus / smooth_tr, 0.0)
    dx_sum = plus_di + minus_di
    dx = np.where(dx_sum > 0, 100 * np.abs(plus_di - minus_di) / dx_sum, 0.0)
    adx = np.zeros_like(dx)
    adx[:trend_period * 2] = dx[:trend_period * 2].mean()
    for i in range(trend_period * 2, len(dx)):
        adx[i] = (adx[i - 1] * (trend_period - 1) + dx[i]) / trend_period

    def _trend_label(i: int) -> str:
        if i < trend_period * 2:
            return "warmup"
        if adx[i] <= 20:
            return "chop"
        return "bull" if plus_di[i] > minus_di[i] else "bear"

    trend_labels = [_trend_label(i) for i in range(len(adx))]

    # Aggregate breakdown
    def _hist(labels: list[str]) -> dict[str, float]:
        valid = [l for l in labels if l != "warmup"]
        if not valid:
            return {}
        return {k: round(valid.count(k) / len(valid) * 100, 1) for k in set(valid)}

    vol_breakdown = _hist(vol_labels)
    trend_breakdown = _hist(trend_labels)

    current = {
        "vol_regime": vol_labels[-1],
        "trend_regime": trend_labels[-1],
        "atr": float(atr[-1]),
        "adx": float(adx[-1]),
        "plus_di": float(plus_di[-1]),
        "minus_di": float(minus_di[-1]),
        "close": float(close[-1]),
    }

    _emit({
        "type": "result", "command": "regime",
        "coin": coin.upper(),
        "tf": tf,
        "candles_analyzed": lookback_candles,
        "current": current,
        "vol_breakdown_pct": vol_breakdown,
        "trend_breakdown_pct": trend_breakdown,
    })
