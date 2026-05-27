"""Unit tests for bar resampling."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from rift_substrate.data.resample import (
    parse_time_freq,
    to_dollar_bars,
    to_time_bars,
    to_volume_bars,
)


@pytest.fixture
def synthetic_1m_candles():
    """100 minutes of synthetic OHLCV at 1-min resolution."""
    rng = np.random.default_rng(42)
    n = 100
    base_price = 100.0
    closes = base_price * np.cumprod(1 + rng.normal(0, 0.001, n))
    opens = np.roll(closes, 1)
    opens[0] = base_price
    highs = np.maximum(opens, closes) * (1 + rng.uniform(0, 0.001, n))
    lows = np.minimum(opens, closes) * (1 - rng.uniform(0, 0.001, n))
    volumes = rng.uniform(0.5, 2.0, n)  # 0.5-2.0 BTC per minute
    timestamps = np.arange(n, dtype=np.int64) * 60_000  # 1-min spacing, epoch ms

    return pl.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


class TestParseTimeFreq:
    def test_valid_freqs(self):
        for f in ["1s", "5s", "1m", "5m", "15m", "1h", "4h", "1d", "1w"]:
            assert parse_time_freq(f) == f

    def test_invalid_suffix_rejected(self):
        with pytest.raises(ValueError, match="end in"):
            parse_time_freq("1x")

    def test_non_int_prefix_rejected(self):
        with pytest.raises(ValueError, match="int"):
            parse_time_freq("ah")


class TestTimeBars:
    def test_resample_1m_to_5m(self, synthetic_1m_candles):
        out = to_time_bars(synthetic_1m_candles, freq="5m")
        # 100 1-min bars → 20 5-min bars
        assert len(out) == 20
        # Conservation: total volume preserved
        assert abs(out["volume"].sum() - synthetic_1m_candles["volume"].sum()) < 1e-6

    def test_resample_preserves_high_low(self, synthetic_1m_candles):
        out = to_time_bars(synthetic_1m_candles, freq="5m")
        # Each 5m bar's high >= max of its 5 inputs
        # Just check overall max is preserved
        assert out["high"].max() == synthetic_1m_candles["high"].max()
        assert out["low"].min() == synthetic_1m_candles["low"].min()

    def test_resample_open_close_correct(self, synthetic_1m_candles):
        out = to_time_bars(synthetic_1m_candles, freq="10m")
        # First 10m bar's open == first 1m bar's open
        first_open = float(synthetic_1m_candles[0, "open"])
        assert abs(out[0, "open"] - first_open) < 1e-9
        # First 10m bar's close == 10th 1m bar's close
        tenth_close = float(synthetic_1m_candles[9, "close"])
        assert abs(out[0, "close"] - tenth_close) < 1e-9

    def test_lazy_input_works(self, synthetic_1m_candles):
        lazy = synthetic_1m_candles.lazy()
        out = to_time_bars(lazy, freq="5m")
        assert len(out) == 20


class TestVolumeBars:
    def test_volume_conservation(self, synthetic_1m_candles):
        threshold = 5.0  # 5 BTC per bar
        out = to_volume_bars(synthetic_1m_candles, threshold_units=threshold)
        # Total volume should be conserved
        total_in = float(synthetic_1m_candles["volume"].sum())
        total_out = float(out["volume"].sum())
        assert abs(total_in - total_out) < 1e-6

    def test_bars_meet_or_exceed_threshold(self, synthetic_1m_candles):
        threshold = 5.0
        out = to_volume_bars(synthetic_1m_candles, threshold_units=threshold)
        # All bars except possibly the last must have volume >= threshold
        # (last may be partial)
        if len(out) > 1:
            for v in out["volume"].to_numpy()[:-1]:
                assert v >= threshold

    def test_smaller_threshold_more_bars(self, synthetic_1m_candles):
        out_big = to_volume_bars(synthetic_1m_candles, threshold_units=10.0)
        out_small = to_volume_bars(synthetic_1m_candles, threshold_units=2.0)
        assert len(out_small) > len(out_big)

    def test_rejects_zero_threshold(self):
        df = pl.DataFrame({"timestamp": [0], "volume": [1.0],
                           "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]})
        with pytest.raises(ValueError, match="> 0"):
            to_volume_bars(df, threshold_units=0)


class TestDollarBars:
    def test_dollar_bars_built_from_price_times_volume(self, synthetic_1m_candles):
        # All synthetic prices ~100, all volumes 0.5-2, so dollar ~50-200 per bar
        threshold_usd = 500.0  # ~5 input bars per output
        out = to_dollar_bars(synthetic_1m_candles, threshold_usd=threshold_usd)
        assert len(out) > 0
        # First N-1 bars should meet threshold roughly
        if len(out) > 1:
            for v, close in zip(out["volume"][:-1], out["close"][:-1]):
                # dollar ~ close * volume; should be >= threshold (with overshoot)
                assert close * v >= threshold_usd * 0.5  # at least half threshold

    def test_smaller_threshold_more_bars(self, synthetic_1m_candles):
        big = to_dollar_bars(synthetic_1m_candles, threshold_usd=1000.0)
        small = to_dollar_bars(synthetic_1m_candles, threshold_usd=100.0)
        assert len(small) > len(big)

    def test_rejects_zero_threshold(self, synthetic_1m_candles):
        with pytest.raises(ValueError, match="> 0"):
            to_dollar_bars(synthetic_1m_candles, threshold_usd=0)
