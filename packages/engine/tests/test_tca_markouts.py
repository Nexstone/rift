"""Tests for the markouts extension to rift_engine.tca."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import polars as pl
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
for sub in ("packages/engine/src", "packages/data/src", "packages/substrate/src", "packages/core/src"):
    p = str(_REPO_ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)


def _write_candles(coin: str, interval: str, candles: list[tuple[int, float]], tmp_data_dir: Path):
    """Write a synthetic candle parquet under tmp_data_dir/COIN/INTERVAL/candles.parquet."""
    out = tmp_data_dir / coin.upper() / interval
    out.mkdir(parents=True, exist_ok=True)
    rows = [
        {"timestamp": ts, "open": px, "high": px, "low": px, "close": px, "volume": 1.0, "num_trades": 1}
        for ts, px in candles
    ]
    df = pl.DataFrame(rows)
    df.write_parquet(out / "candles.parquet")


# ─── compute_session_markouts ───────────────────────────────────────


class TestComputeSessionMarkouts:
    def test_empty_trades_returns_empty(self, tmp_path):
        from rift_engine.tca import compute_session_markouts
        result = compute_session_markouts(trades=[], pair="BTC", interval="1m", data_dir=tmp_path / "data")
        assert result["n_fills"] == 0
        assert result["markouts_bps"] == {"t+1s": 0.0, "t+10s": 0.0, "t+60s": 0.0, "t+300s": 0.0}

    def test_long_with_price_rise_positive_markout(self, tmp_path):
        from rift_engine.tca import compute_session_markouts
        data_dir = tmp_path / "data"

        # Use a realistic epoch base (entry_ts = 0 is filtered as invalid)
        BASE = 1_700_000_000_000
        candles = [
            (BASE,            100.00),
            (BASE + 1_000,    100.10),
            (BASE + 10_000,   100.50),
            (BASE + 60_000,   101.00),
            (BASE + 300_000,  103.00),
        ]
        _write_candles("BTC", "1m", candles, data_dir)

        result = compute_session_markouts(
            trades=[{"entry_ts": BASE, "entry_price": 100.0, "side": "long"}],
            pair="BTC", interval="1m", data_dir=data_dir,
        )
        assert result["n_fills"] >= 1
        m = result["markouts_bps"]
        # Long + price up = positive markout (trader edge)
        assert m["t+1s"] > 0
        assert m["t+10s"] > m["t+1s"]
        assert m["t+300s"] > m["t+60s"]

    def test_short_with_price_rise_negative_markout(self, tmp_path):
        from rift_engine.tca import compute_session_markouts
        data_dir = tmp_path / "data"
        BASE = 1_700_000_000_000
        candles = [
            (BASE,            100.00),
            (BASE + 1_000,    100.10),
            (BASE + 10_000,   100.50),
            (BASE + 60_000,   101.00),
            (BASE + 300_000,  103.00),
        ]
        _write_candles("BTC", "1m", candles, data_dir)

        result = compute_session_markouts(
            trades=[{"entry_ts": BASE, "entry_price": 100.0, "side": "short"}],
            pair="BTC", interval="1m", data_dir=data_dir,
        )
        m = result["markouts_bps"]
        # Short + price up = adverse = NEGATIVE markout
        assert m["t+1s"] < 0
        assert m["t+300s"] < m["t+60s"]  # gets worse over time

    def test_no_post_fill_candles_returns_zero(self, tmp_path):
        from rift_engine.tca import compute_session_markouts
        data_dir = tmp_path / "data"
        BASE = 1_700_000_000_000
        _write_candles("BTC", "1m", [(BASE, 100.0), (BASE + 1_000, 100.1)], data_dir)

        result = compute_session_markouts(
            trades=[{"entry_ts": BASE + 9_999_999_999, "entry_price": 100.0, "side": "long"}],
            pair="BTC", interval="1m", data_dir=data_dir,
        )
        assert result["n_fills"] == 0

    def test_missing_candle_cache_returns_zeros(self, tmp_path):
        from rift_engine.tca import compute_session_markouts
        result = compute_session_markouts(
            trades=[{"entry_ts": 1_700_000_000_000, "entry_price": 100.0, "side": "long"}],
            pair="NONEXISTENT", interval="1m", data_dir=tmp_path / "data",
        )
        assert result["n_fills"] == 0


# ─── analyze_session_log embeds markouts ────────────────────────────


class TestAnalyzeSessionLogWithMarkouts:
    def test_session_log_with_pair_metadata_includes_markouts(self, tmp_path):
        from rift_engine.tca import analyze_session_log
        data_dir = tmp_path / "data"
        BASE = 1_700_000_000_000
        _write_candles("BTC", "1m", [(BASE, 100.0), (BASE + 1_000, 100.5), (BASE + 60_000, 101.0)], data_dir)

        session = {
            "pair": "BTC",
            "interval": "1m",
            "trades": [
                {"entry_ts": BASE, "entry_price": 100.0, "exit_price": 101.0,
                 "size": 1.0, "side": "long",
                 "entry_mid_price": 100.0, "exit_mid_price": 101.0,
                 "execution_method": "ioc"},
            ],
        }
        log_path = tmp_path / "session.json"
        log_path.write_text(json.dumps(session))

        report = analyze_session_log(str(log_path), data_dir=data_dir)
        assert report.markout_n_fills > 0
        assert "t+1s" in report.markouts_bps
        assert report.markout_horizons_seconds == [1, 10, 60, 300]

    def test_session_log_without_pair_skips_markouts(self, tmp_path):
        from rift_engine.tca import analyze_session_log
        session = {
            "trades": [
                {"entry_ts": 0, "entry_price": 100.0, "exit_price": 101.0,
                 "size": 1.0, "side": "long",
                 "entry_mid_price": 100.0, "exit_mid_price": 101.0,
                 "execution_method": "ioc"},
            ],
        }
        log_path = tmp_path / "session.json"
        log_path.write_text(json.dumps(session))

        report = analyze_session_log(str(log_path))
        # No pair → no markouts but TCA still works
        assert report.markout_n_fills == 0
        assert report.markouts_bps == {}
        assert len(report.trades) == 1
