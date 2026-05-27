"""Unit tests for rift_data.s3.aggregate.fills_to_candles.

This is the most logic-heavy function in the data layer — it filters to
taker fills (no double-counting), bucketizes by timestamp, computes 18
candle columns including order-flow features.
"""

from __future__ import annotations

import polars as pl
import pytest

from rift_data.s3.aggregate import fills_to_candles


class TestFillsToCandles:
    def test_empty_fills_returns_empty_df_with_minimal_schema(self):
        empty = pl.DataFrame(schema={"timestamp": pl.Int64, "price": pl.Float64,
                                     "size": pl.Float64, "side": pl.Utf8, "dir": pl.Utf8,
                                     "is_open": pl.Boolean, "is_long": pl.Boolean,
                                     "crossed": pl.Boolean, "closed_pnl": pl.Float64,
                                     "fee": pl.Float64, "start_position": pl.Float64})
        out = fills_to_candles(empty, "5m")
        assert len(out) == 0
        # Minimal schema for empty case
        assert {"timestamp", "open", "high", "low", "close", "volume", "num_trades"}.issubset(set(out.columns))

    def test_unknown_interval_raises(self, sample_fills_df):
        with pytest.raises(ValueError, match="Unknown interval"):
            fills_to_candles(sample_fills_df, "13m")

    def test_taker_filter_avoids_double_count(self, sample_fills_df):
        """S3 records both sides of every trade. fills_to_candles must filter
        to crossed=True (takers only) so volume isn't doubled."""
        # sample fixture has 6 fills total, 3 taker (crossed=True), 3 maker
        candles = fills_to_candles(sample_fills_df, "5m")
        # Total volume should equal sum of taker fills only
        taker_volume = sample_fills_df.filter(pl.col("crossed"))["size"].sum()
        assert candles["volume"].sum() == pytest.approx(taker_volume)

    def test_bucketization_by_interval(self, sample_fills_df):
        """Fixture has fills in two 5-min buckets (t0 and t1=t0+300000ms).
        Expect exactly 2 candles at 5m, 1 candle at 1h (both buckets within same hour)."""
        c5 = fills_to_candles(sample_fills_df, "5m")
        c1h = fills_to_candles(sample_fills_df, "1h")
        assert len(c5) == 2
        assert len(c1h) == 1

    def test_ohlc_correctness(self, sample_fills_df):
        """For the test fixture, manually verify OHLC of bucket 0:
        - Bucket 0 taker fills: (50000.0, B/Open Long), (50100.0, B/Close Short)
        - open=50000, high=50100, low=50000, close=50100
        """
        c5 = fills_to_candles(sample_fills_df, "5m").sort("timestamp")
        b0 = c5.row(0, named=True)
        assert b0["open"] == 50000.0
        assert b0["high"] == 50100.0
        assert b0["low"] == 50000.0
        assert b0["close"] == 50100.0

    def test_num_trades_counts_taker_fills(self, sample_fills_df):
        c5 = fills_to_candles(sample_fills_df, "5m").sort("timestamp")
        # Bucket 0: 2 taker fills; Bucket 1: 1 taker fill
        assert c5["num_trades"].sum() == 3

    def test_order_flow_columns_present_when_flow_data_available(self, sample_fills_df):
        candles = fills_to_candles(sample_fills_df, "5m")
        for col in ("buy_volume", "sell_volume", "volume_delta", "opens_long",
                    "closes_long", "opens_short", "closes_short", "net_flow"):
            assert col in candles.columns, f"missing order-flow column: {col}"

    def test_buy_vs_sell_aggression_classification(self, sample_fills_df):
        """Taker buying = (open long) OR (close short). Taker selling = opposite.

        Bucket 0 taker fills:
          - (50000, B, Open Long, size=1)   → buy aggression
          - (50100, B, Close Short, size=0.5) → buy aggression
        Bucket 1 taker fills:
          - (49900, A, Open Short, size=2) → sell aggression

        So bucket 0: buy_volume=1.5, sell_volume=0
           bucket 1: buy_volume=0, sell_volume=2
        """
        c5 = fills_to_candles(sample_fills_df, "5m").sort("timestamp")
        b0, b1 = c5.row(0, named=True), c5.row(1, named=True)
        assert b0["buy_volume"] == pytest.approx(1.5)
        assert b0["sell_volume"] == pytest.approx(0.0)
        assert b1["buy_volume"] == pytest.approx(0.0)
        assert b1["sell_volume"] == pytest.approx(2.0)

    def test_volume_delta_is_buy_minus_sell(self, sample_fills_df):
        c5 = fills_to_candles(sample_fills_df, "5m")
        for row in c5.iter_rows(named=True):
            assert row["volume_delta"] == pytest.approx(row["buy_volume"] - row["sell_volume"])

    def test_total_fees_summed(self, sample_fills_df):
        """Bucket 0 taker fees: 1.7 + 0.85 = 2.55; bucket 1: 1.7"""
        c5 = fills_to_candles(sample_fills_df, "5m").sort("timestamp")
        assert c5.row(0, named=True)["total_fees"] == pytest.approx(2.55)
        assert c5.row(1, named=True)["total_fees"] == pytest.approx(1.7)

    def test_timestamps_aligned_to_interval(self, sample_fills_df):
        """Each candle's timestamp must be a multiple of the interval in ms."""
        for tf, ms in [("5m", 300_000), ("1h", 3_600_000), ("1m", 60_000)]:
            candles = fills_to_candles(sample_fills_df, tf)
            if len(candles) == 0:
                continue
            for ts in candles["timestamp"]:
                assert ts % ms == 0, f"timestamp {ts} not aligned to {tf}"
