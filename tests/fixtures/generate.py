"""Generate synthetic test-only candle + funding fixtures for CI.

These are NOT real Hyperliquid data — they're synthetic series designed
to satisfy the integration test's structural requirements (enough candles
to backtest, funding rates aligned with candles, trend_follow strategy
generates trades). Real validation happens locally with `rift sync`.

Outputs to `tests/fixtures/data/{COIN}/{interval}/candles.parquet` and
`tests/fixtures/data/{COIN}/funding/rates.parquet`. Re-run via:

    engine/.venv/bin/python tests/fixtures/generate.py

Committed into the repo so CI can use them without an AWS sync.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl


FIXTURES_ROOT = Path(__file__).resolve().parent / "data"


def generate_btc_4h_candles(n_bars: int = 600, seed: int = 42) -> pl.DataFrame:
    """Generate ~3.5 months of 4h BTC candles with realistic trend regimes.

    600 bars × 4h = 2400 hours = 100 days. Engineered to have at least one
    full bull → bear → bull regime cycle so trend_follow's EMA50/200 cross
    fires multiple times.
    """
    rng = np.random.default_rng(seed)
    BASE_TS = 1_730_000_000_000  # ~2024-10-27
    INTERVAL_MS = 4 * 60 * 60 * 1000  # 4h
    timestamps = (BASE_TS + np.arange(n_bars) * INTERVAL_MS).astype(np.int64)

    # Trend regimes: bull, bear, bull, sideways → drift switches signs
    drifts = np.zeros(n_bars)
    drifts[:200] = 0.0015          # bull (~0.15% per bar)
    drifts[200:400] = -0.0012       # bear
    drifts[400:500] = 0.0010        # bull
    drifts[500:] = 0.0000           # sideways
    noise = rng.normal(0, 0.012, n_bars)  # ~1.2% per-bar vol

    log_returns = drifts + noise
    log_returns[0] = 0
    closes = 70_000.0 * np.exp(np.cumsum(log_returns))

    # OHLC from closes + intra-bar noise
    intra = rng.normal(0, 0.003, n_bars) * closes
    highs = closes + np.abs(intra)
    lows = closes - np.abs(intra)
    opens = np.empty(n_bars)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    # Ensure OHLC consistency
    highs = np.maximum.reduce([opens, highs, lows, closes])
    lows = np.minimum.reduce([opens, highs, lows, closes])

    volumes = rng.uniform(50_000, 500_000, n_bars)
    num_trades = rng.integers(1_000, 20_000, n_bars)

    return pl.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
        "num_trades": num_trades,
    })


def generate_btc_funding(timestamps_ms: np.ndarray, seed: int = 43) -> pl.DataFrame:
    """Generate hourly funding rates spanning the candle timestamps."""
    rng = np.random.default_rng(seed)
    start = int(timestamps_ms[0])
    end = int(timestamps_ms[-1])
    hour_ms = 60 * 60 * 1000
    n_funding = (end - start) // hour_ms + 1
    funding_ts = np.array([start + i * hour_ms for i in range(n_funding)], dtype=np.int64)
    # Realistic HL funding: mostly 0.001%-0.01% per hour, occasional spikes
    funding_rate = rng.normal(0.00001, 0.000015, n_funding)
    funding_rate = np.clip(funding_rate, -0.00050, 0.00050)
    premium = rng.normal(0, 0.0005, n_funding)
    return pl.DataFrame({
        "timestamp": funding_ts,
        "funding_rate": funding_rate,
        "premium": premium,
    })


def main() -> None:
    """Generate + write all fixtures."""
    FIXTURES_ROOT.mkdir(parents=True, exist_ok=True)

    # BTC 4h candles
    btc_4h = generate_btc_4h_candles()
    out_candles = FIXTURES_ROOT / "BTC" / "4h"
    out_candles.mkdir(parents=True, exist_ok=True)
    candles_path = out_candles / "candles.parquet"
    btc_4h.write_parquet(candles_path)
    print(f"  Wrote {candles_path} ({len(btc_4h)} 4h candles)")

    # BTC funding (hourly)
    funding_df = generate_btc_funding(btc_4h["timestamp"].to_numpy())
    out_funding = FIXTURES_ROOT / "BTC" / "funding"
    out_funding.mkdir(parents=True, exist_ok=True)
    funding_path = out_funding / "rates.parquet"
    funding_df.write_parquet(funding_path)
    print(f"  Wrote {funding_path} ({len(funding_df)} hourly funding rows)")

    print(f"\nTotal fixture size on disk:")
    import subprocess
    subprocess.run(["du", "-sh", str(FIXTURES_ROOT)])


if __name__ == "__main__":
    main()
