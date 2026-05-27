"""Tests for the polars-native indicator wrappers in rift_substrate.indicators.

Each test verifies: (a) accepts polars/numpy/pandas/list inputs equivalently,
(b) returns a polars Series or DataFrame with sensible bounds, (c) the
output length matches input length (warm-up filled with nulls).
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest


@pytest.fixture(scope="module")
def ohlcv():
    """Synthetic OHLCV data — 200 bars with random walk + clipped volume."""
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 0.5, 200))
    high = close + np.abs(rng.normal(0, 0.3, 200))
    low = close - np.abs(rng.normal(0, 0.3, 200))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    volume = np.clip(rng.normal(1000, 100, 200), 100, None)
    return {
        "open": open_, "high": high, "low": low, "close": close, "volume": volume,
    }


# ─── Moving averages ───────────────────────────────────────────────────


class TestMovingAverages:
    def test_sma_length_matches_input(self, ohlcv):
        from rift_substrate.indicators import sma
        out = sma(ohlcv["close"], length=20)
        assert isinstance(out, pl.Series)
        assert len(out) == len(ohlcv["close"])
        assert out.name == "sma_20"

    def test_ema_accepts_polars_series(self, ohlcv):
        from rift_substrate.indicators import ema
        s = pl.Series("close", ohlcv["close"])
        out = ema(s, length=10)
        assert len(out) == len(s)
        # Last value should be near recent closes (smoothing)
        assert abs(out[-1] - ohlcv["close"][-1]) < 5.0

    def test_wma_accepts_python_list(self, ohlcv):
        from rift_substrate.indicators import wma
        out = wma(list(ohlcv["close"]), length=5)
        assert len(out) == len(ohlcv["close"])


# ─── Momentum ──────────────────────────────────────────────────────────


class TestMomentum:
    def test_rsi_in_0_100_range(self, ohlcv):
        from rift_substrate.indicators import rsi
        out = rsi(ohlcv["close"], length=14)
        valid = out.drop_nulls()
        assert (valid >= 0).all()
        assert (valid <= 100).all()

    def test_macd_returns_three_columns(self, ohlcv):
        from rift_substrate.indicators import macd
        out = macd(ohlcv["close"])
        assert isinstance(out, pl.DataFrame)
        assert set(out.columns) == {"macd", "macd_hist", "macd_signal"}
        assert len(out) == len(ohlcv["close"])

    def test_stoch_returns_k_and_d(self, ohlcv):
        from rift_substrate.indicators import stoch
        out = stoch(ohlcv["high"], ohlcv["low"], ohlcv["close"])
        assert isinstance(out, pl.DataFrame)
        assert set(out.columns) == {"stoch_k", "stoch_d"}


# ─── Volatility ────────────────────────────────────────────────────────


class TestVolatility:
    def test_atr_is_positive(self, ohlcv):
        from rift_substrate.indicators import atr
        out = atr(ohlcv["high"], ohlcv["low"], ohlcv["close"], length=14)
        valid = out.drop_nulls()
        assert (valid > 0).all()

    def test_bbands_upper_above_lower(self, ohlcv):
        from rift_substrate.indicators import bbands
        out = bbands(ohlcv["close"], length=20, std=2.0)
        assert {"bb_lower", "bb_mid", "bb_upper"}.issubset(set(out.columns))
        valid = out.drop_nulls()
        assert (valid["bb_upper"] >= valid["bb_mid"]).all()
        assert (valid["bb_mid"] >= valid["bb_lower"]).all()


# ─── Volume ────────────────────────────────────────────────────────────


class TestVolume:
    def test_obv_runs(self, ohlcv):
        from rift_substrate.indicators import obv
        out = obv(ohlcv["close"], ohlcv["volume"])
        assert len(out) == len(ohlcv["close"])
        assert out.name == "obv"

    def test_vwap_within_high_low_envelope(self, ohlcv):
        from rift_substrate.indicators import vwap
        out = vwap(ohlcv["high"], ohlcv["low"], ohlcv["close"], ohlcv["volume"])
        # VWAP should be within a reasonable envelope of the candles
        valid = out.drop_nulls()
        assert (valid >= ohlcv["low"].min() * 0.9).all()
        assert (valid <= ohlcv["high"].max() * 1.1).all()

    def test_vwap_matches_manual_calculation(self):
        """VWAP must match Σ(typical*v) / Σ(v) exactly — no session reset,
        no funky index dependency. Crypto-correct continuous VWAP."""
        from rift_substrate.indicators import vwap
        h = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
        l = np.array([8.0, 9.0, 10.0, 11.0, 12.0])
        c = np.array([9.0, 10.0, 11.0, 12.0, 13.0])
        v = np.array([100.0, 200.0, 150.0, 50.0, 100.0])
        typical = (h + l + c) / 3
        expected = (typical * v).cumsum() / v.cumsum()
        actual = vwap(h, l, c, v).to_numpy()
        np.testing.assert_allclose(actual, expected, rtol=1e-9)

    def test_vwap_zero_volume_returns_null_not_nan(self):
        """If cumulative volume is zero (degenerate input), output is null."""
        from rift_substrate.indicators import vwap
        out = vwap([10.0], [8.0], [9.0], [0.0])
        assert out.null_count() == 1


# ─── Trend strength ────────────────────────────────────────────────────


class TestTrend:
    def test_adx_in_0_100_range(self, ohlcv):
        from rift_substrate.indicators import adx
        out = adx(ohlcv["high"], ohlcv["low"], ohlcv["close"], length=14)
        valid = out["adx"].drop_nulls()
        assert (valid >= 0).all()
        assert (valid <= 100).all()

    def test_supertrend_direction_is_signed(self, ohlcv):
        from rift_substrate.indicators import supertrend
        out = supertrend(ohlcv["high"], ohlcv["low"], ohlcv["close"])
        # direction column is +1 or -1
        valid = out["supertrend_direction"].drop_nulls()
        unique = set(valid.to_list())
        assert unique.issubset({1, -1})


# ─── Statistics ────────────────────────────────────────────────────────


class TestStatistics:
    def test_zscore_roughly_centered(self, ohlcv):
        from rift_substrate.indicators import zscore
        out = zscore(ohlcv["close"], length=30)
        valid = out.drop_nulls()
        # Rolling zscore should be roughly centered around 0
        assert abs(valid.mean()) < 1.0


# ─── Power-user escape hatch ───────────────────────────────────────────


class TestEscapeHatch:
    def test_ta_namespace_reachable(self):
        from rift_substrate.indicators import ta
        # pandas-ta-classic surface should be present
        assert hasattr(ta, "rsi")
        assert hasattr(ta, "kama")
        assert hasattr(ta, "obv")


# ─── Prefix-rename robustness ──────────────────────────────────────────


class TestPrefixRename:
    """Lock the invariant that wrappers translate pandas-ta-classic columns
    by PREFIX, not by exact param-suffixed name. If pandas-ta-classic ever
    changes its suffix format (e.g. '20_2.0' → '20_2.00'), prefix matching
    still works and these tests still pass.
    """

    def test_smart_rename_picks_longest_prefix(self):
        import pandas as pd

        from rift_substrate.indicators.core import _smart_rename

        df = pd.DataFrame({
            "MACD_12_26_9": [1.0],
            "MACDh_12_26_9": [2.0],
            "MACDs_12_26_9": [3.0],
        })
        prefix_map = {"MACD_": "macd", "MACDh_": "macd_hist", "MACDs_": "macd_signal"}
        out = _smart_rename(df, prefix_map)
        assert set(out.columns) == {"macd", "macd_hist", "macd_signal"}
        # MACD_ must NOT have eaten the MACDh_/MACDs_ columns.
        assert out["macd"].iloc[0] == 1.0
        assert out["macd_hist"].iloc[0] == 2.0
        assert out["macd_signal"].iloc[0] == 3.0

    def test_smart_rename_survives_suffix_drift(self):
        """If pandas-ta-classic changes '20_2.0' → '20_2.00' or drops the
        suffix entirely, prefix matching still picks the right target."""
        import pandas as pd

        from rift_substrate.indicators.core import _smart_rename

        # Simulate three different suffix styles for the same indicator.
        for cols in (
            ["BBL_20_2.0", "BBM_20_2.0", "BBU_20_2.0"],
            ["BBL_20_2.00", "BBM_20_2.00", "BBU_20_2.00"],
            ["BBL_xyz", "BBM_xyz", "BBU_xyz"],
        ):
            df = pd.DataFrame({c: [1.0] for c in cols})
            out = _smart_rename(df, {"BBL_": "bb_lower", "BBM_": "bb_mid", "BBU_": "bb_upper"})
            assert {"bb_lower", "bb_mid", "bb_upper"}.issubset(set(out.columns)), (
                f"prefix rename failed for {cols}"
            )

    def test_unmatched_columns_pass_through(self):
        import pandas as pd

        from rift_substrate.indicators.core import _smart_rename

        df = pd.DataFrame({"ABC_xyz": [1.0], "BBL_20_2.0": [2.0]})
        out = _smart_rename(df, {"BBL_": "bb_lower"})
        # ABC_xyz didn't match → left alone; BBL_… renamed.
        assert "bb_lower" in out.columns
        assert "ABC_xyz" in out.columns


# ─── Edge cases ────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_input_returns_empty_series(self):
        from rift_substrate.indicators import rsi
        out = rsi([], length=14)
        assert isinstance(out, pl.Series)
        assert len(out) == 0

    def test_empty_input_multi_output_returns_empty_frame(self):
        from rift_substrate.indicators import macd
        out = macd([])
        assert isinstance(out, pl.DataFrame)
        assert len(out) == 0

    def test_too_short_input_is_padded_to_input_length(self):
        from rift_substrate.indicators import rsi
        # 3 rows can't compute a 14-period RSI; output must still be 3-long
        out = rsi([100.0, 101.0, 99.0], length=14)
        assert len(out) == 3
        assert out.null_count() == 3

    def test_too_short_multi_output_padded(self):
        from rift_substrate.indicators import bbands
        out = bbands([100.0, 101.0, 99.0], length=20)
        assert len(out) == 3
        # All target columns present and full of nulls
        for col in ("bb_lower", "bb_mid", "bb_upper"):
            assert col in out.columns
            assert out[col].null_count() == 3

    def test_constant_input_does_not_crash(self):
        from rift_substrate.indicators import rsi, atr
        out = rsi([100.0] * 50, length=14)
        assert len(out) == 50  # all nulls or all 50 (Wilder smoothing); just must not raise
        out2 = atr([100.0] * 50, [100.0] * 50, [100.0] * 50, length=14)
        assert len(out2) == 50
