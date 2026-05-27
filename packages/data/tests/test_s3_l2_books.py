"""Tests for rift_data.s3.l2_books — parse + aggregate L2 book history.

Pins behavior on synthetic JSONL strings — no S3, no real disk dependency.

Coverage:
  1. parse_l2_jsonl correctly extracts (ts, bids, asks) per line; skips malformed lines
  2. L2Snapshot derived properties (best_bid, mid, spread_bps) compute right
  3. aggregate produces ONE bar per interval window with correct OHLC
  4. Depth features: bid_depth, imbalance, max_wall computed from input
  5. Snapshots that are one-sided / non-positive mid get filtered out
  6. Multi-bar input → multiple sorted bars
  7. Empty / all-malformed input → empty result
  8. interval_to_ms map covers expected names
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys
_repo_root = Path(__file__).resolve().parents[3]
for sub in ("packages/data/src",):
    p = str(_repo_root / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from rift_data.s3.l2_books import (
    L2Snapshot,
    aggregate_l2_to_candles,
    interval_to_ms,
    parse_l2_jsonl,
)


# ─── Helpers ─────────────────────────────────────────────────────────


def _build_snapshot_line(
    ts_ms: int,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
) -> str:
    """Construct one JSONL line in HL's L2 book format."""
    return json.dumps({
        "time": "2024-06-01T12:00:00.000000000",
        "ver_num": 1,
        "raw": {
            "channel": "l2Book",
            "data": {
                "coin": "BTC",
                "time": ts_ms,
                "levels": [
                    [{"px": str(p), "sz": str(s), "n": 1} for p, s in bids],
                    [{"px": str(p), "sz": str(s), "n": 1} for p, s in asks],
                ],
            },
        },
    })


def _write_jsonl(tmp_path: Path, lines: list[str]) -> Path:
    f = tmp_path / "snapshots.jsonl"
    f.write_text("\n".join(lines))
    return f


# ─── L2Snapshot ──────────────────────────────────────────────────────


class TestL2Snapshot:
    def test_best_bid_ask_mid(self):
        s = L2Snapshot(
            timestamp_ms=1000,
            bids=[(99.5, 1.0), (99.0, 2.0)],
            asks=[(100.5, 1.0), (101.0, 2.0)],
        )
        assert s.best_bid == 99.5
        assert s.best_ask == 100.5
        assert s.mid == 100.0

    def test_spread_bps(self):
        # spread = 1.0, mid = 100 → 100 bps
        s = L2Snapshot(
            timestamp_ms=1000,
            bids=[(99.5, 1.0)],
            asks=[(100.5, 1.0)],
        )
        assert s.spread_bps == pytest.approx(100.0)

    def test_one_sided_book_has_no_mid(self):
        s = L2Snapshot(timestamp_ms=1000, bids=[(99.5, 1.0)], asks=[])
        assert s.mid is None
        assert s.spread_bps is None

    def test_empty_book(self):
        s = L2Snapshot(timestamp_ms=1000, bids=[], asks=[])
        assert s.best_bid is None
        assert s.mid is None


# ─── parse_l2_jsonl ──────────────────────────────────────────────────


class TestParseL2Jsonl:
    def test_parses_well_formed_lines(self, tmp_path):
        line = _build_snapshot_line(
            ts_ms=1717243200000,
            bids=[(67730.0, 0.5), (67729.0, 1.0)],
            asks=[(67732.0, 0.3), (67733.0, 0.8)],
        )
        f = _write_jsonl(tmp_path, [line])
        snaps = list(parse_l2_jsonl(f))
        assert len(snaps) == 1
        s = snaps[0]
        assert s.timestamp_ms == 1717243200000
        assert s.bids == [(67730.0, 0.5), (67729.0, 1.0)]
        assert s.asks == [(67732.0, 0.3), (67733.0, 0.8)]

    def test_skips_malformed_lines(self, tmp_path):
        good = _build_snapshot_line(1000, [(99.0, 1.0)], [(101.0, 1.0)])
        f = _write_jsonl(tmp_path, [
            "not valid json",
            "",
            good,
            json.dumps({"missing_raw_key": True}),
            json.dumps({"raw": {"data": "wrong shape"}}),
        ])
        snaps = list(parse_l2_jsonl(f))
        # Only the good line should yield a snapshot
        assert len(snaps) == 1
        assert snaps[0].timestamp_ms == 1000

    def test_empty_file_yields_nothing(self, tmp_path):
        f = _write_jsonl(tmp_path, [])
        assert list(parse_l2_jsonl(f)) == []

    def test_multiple_lines_preserve_order(self, tmp_path):
        lines = [
            _build_snapshot_line(1000, [(99.0, 1.0)], [(101.0, 1.0)]),
            _build_snapshot_line(2000, [(99.5, 1.0)], [(100.5, 1.0)]),
            _build_snapshot_line(3000, [(100.0, 1.0)], [(100.2, 1.0)]),
        ]
        f = _write_jsonl(tmp_path, lines)
        snaps = list(parse_l2_jsonl(f))
        assert [s.timestamp_ms for s in snaps] == [1000, 2000, 3000]


# ─── aggregate_l2_to_candles ─────────────────────────────────────────


class TestAggregate:
    def test_single_bar_ohlc(self):
        """Three snapshots in a 1m bar → one row with correct OHLC."""
        snaps = [
            L2Snapshot(timestamp_ms=60_000, bids=[(99.0, 1.0)], asks=[(101.0, 1.0)]),   # mid 100
            L2Snapshot(timestamp_ms=70_000, bids=[(99.5, 1.0)], asks=[(102.5, 1.0)]),   # mid 101
            L2Snapshot(timestamp_ms=110_000, bids=[(98.0, 1.0)], asks=[(100.0, 1.0)]),  # mid 99
        ]
        bars = aggregate_l2_to_candles(snaps, interval_ms=60_000)
        assert len(bars) == 1
        b = bars[0]
        assert b["timestamp"] == 60_000  # bar START
        assert b["open"] == 100.0
        assert b["high"] == 101.0
        assert b["low"] == 99.0
        assert b["close"] == 99.0
        assert b["volume"] is None
        assert b["num_trades"] is None
        assert b["n_snapshots"] == 3

    def test_multi_bar_sorting(self):
        """Snapshots spanning 3 bars produce 3 sorted bars."""
        snaps = [
            # bar @ 60_000
            L2Snapshot(timestamp_ms=60_000, bids=[(99.0, 1.0)], asks=[(101.0, 1.0)]),
            # bar @ 120_000
            L2Snapshot(timestamp_ms=125_000, bids=[(100.0, 1.0)], asks=[(102.0, 1.0)]),
            # bar @ 180_000
            L2Snapshot(timestamp_ms=180_000, bids=[(101.0, 1.0)], asks=[(103.0, 1.0)]),
        ]
        bars = aggregate_l2_to_candles(snaps, interval_ms=60_000)
        assert [b["timestamp"] for b in bars] == [60_000, 120_000, 180_000]
        assert bars[0]["close"] == 100.0
        assert bars[1]["close"] == 101.0
        assert bars[2]["close"] == 102.0

    def test_depth_features_basic(self):
        """Verify depth columns are computed correctly on a known input."""
        # One snapshot: bids 100×1 (USD 100) + 99×2 (USD 198) = 298 USD
        #               asks 101×1 (USD 101) + 102×3 (USD 306) = 407 USD
        # imbalance = (298 - 407) / (298 + 407) = -109 / 705 ≈ -0.1546
        # max_bid_wall = max(100, 198) = 198 USD
        # max_ask_wall = max(101, 306) = 306 USD
        snaps = [L2Snapshot(
            timestamp_ms=0,
            bids=[(100.0, 1.0), (99.0, 2.0)],
            asks=[(101.0, 1.0), (102.0, 3.0)],
        )]
        bars = aggregate_l2_to_candles(snaps, interval_ms=60_000)
        assert len(bars) == 1
        b = bars[0]
        assert b["bid_depth_top10_usd"] == pytest.approx(298.0)
        assert b["ask_depth_top10_usd"] == pytest.approx(407.0)
        assert b["order_book_imbalance"] == pytest.approx(-109.0 / 705.0, rel=1e-6)
        assert b["max_bid_wall_usd"] == pytest.approx(198.0)
        assert b["max_ask_wall_usd"] == pytest.approx(306.0)
        # spread = 1.0, mid = 100.5 → 99.50 bps
        assert b["mean_spread_bps"] == pytest.approx(1.0 / 100.5 * 10_000, rel=1e-6)

    def test_max_wall_takes_max_across_snapshots(self):
        """max_*_wall_usd should track the LARGEST wall ever seen in the bar."""
        snaps = [
            L2Snapshot(
                timestamp_ms=0,
                bids=[(100.0, 1.0)],   # 100 USD
                asks=[(101.0, 1.0)],   # 101 USD
            ),
            L2Snapshot(
                timestamp_ms=30_000,
                bids=[(100.0, 50.0)],  # 5000 USD — the big wall
                asks=[(101.0, 1.0)],
            ),
            L2Snapshot(
                timestamp_ms=50_000,
                bids=[(100.0, 1.0)],
                asks=[(101.0, 1.0)],
            ),
        ]
        bars = aggregate_l2_to_candles(snaps, interval_ms=60_000)
        assert len(bars) == 1
        assert bars[0]["max_bid_wall_usd"] == pytest.approx(5000.0)

    def test_depth_features_averaged_across_snapshots(self):
        """bid_depth / ask_depth / imbalance / spread are mean-aggregated."""
        snaps = [
            # snap 1: balanced book, depth 100/100, imbalance 0
            L2Snapshot(timestamp_ms=0, bids=[(100.0, 1.0)], asks=[(101.0, 1.0)]),
            # snap 2: heavier bid, depth 200/100, imbalance = 100/300 ≈ 0.333
            L2Snapshot(timestamp_ms=30_000, bids=[(100.0, 2.0)], asks=[(101.0, 1.0)]),
        ]
        bars = aggregate_l2_to_candles(snaps, interval_ms=60_000)
        b = bars[0]
        # bid depth mean = (100 + 200) / 2 = 150
        assert b["bid_depth_top10_usd"] == pytest.approx(150.0)
        # ask depth mean = (101 + 101) / 2 = 101
        assert b["ask_depth_top10_usd"] == pytest.approx(101.0)
        # imbalance mean = (0 + 100/(300+1)) / 2 ≈ depends, just check < 0.2
        # snap 1: (100-101)/(100+101) ≈ -0.00498
        # snap 2: (200-101)/(200+101) ≈ 0.329
        # mean ≈ 0.162
        assert b["order_book_imbalance"] == pytest.approx(0.162, abs=0.01)

    def test_one_sided_snapshots_filtered(self):
        """Snapshots with empty bids/asks shouldn't contribute."""
        snaps = [
            L2Snapshot(timestamp_ms=0, bids=[(100.0, 1.0)], asks=[(101.0, 1.0)]),   # OK
            L2Snapshot(timestamp_ms=10_000, bids=[(100.0, 1.0)], asks=[]),          # one-sided
            L2Snapshot(timestamp_ms=20_000, bids=[], asks=[(101.0, 1.0)]),          # one-sided
            L2Snapshot(timestamp_ms=30_000, bids=[(99.0, 1.0)], asks=[(102.0, 1.0)]),  # OK
        ]
        bars = aggregate_l2_to_candles(snaps, interval_ms=60_000)
        assert len(bars) == 1
        assert bars[0]["n_snapshots"] == 2  # only the two valid ones

    def test_empty_input_yields_empty_output(self):
        assert aggregate_l2_to_candles([], interval_ms=60_000) == []

    def test_all_one_sided_input_yields_empty(self):
        snaps = [
            L2Snapshot(timestamp_ms=0, bids=[(100.0, 1.0)], asks=[]),
            L2Snapshot(timestamp_ms=10_000, bids=[], asks=[(101.0, 1.0)]),
        ]
        assert aggregate_l2_to_candles(snaps, interval_ms=60_000) == []

    def test_invalid_interval_ms_raises(self):
        with pytest.raises(ValueError, match="interval_ms"):
            aggregate_l2_to_candles([], interval_ms=0)
        with pytest.raises(ValueError, match="interval_ms"):
            aggregate_l2_to_candles([], interval_ms=-1)

    def test_invalid_depth_levels_raises(self):
        with pytest.raises(ValueError, match="depth_levels"):
            aggregate_l2_to_candles([], interval_ms=60_000, depth_levels=0)

    def test_depth_levels_caps_at_top_n(self):
        """depth_levels=2 should only count the top 2 levels."""
        snaps = [L2Snapshot(
            timestamp_ms=0,
            # 5 bid levels: 100, 99, 98, 97, 96 — top 2 are 100 + 99
            bids=[(100.0, 1.0), (99.0, 1.0), (98.0, 1.0), (97.0, 1.0), (96.0, 1.0)],
            asks=[(101.0, 1.0)],
        )]
        bars = aggregate_l2_to_candles(snaps, interval_ms=60_000, depth_levels=2)
        # Top-2 bid depth = 100 + 99 = 199
        assert bars[0]["bid_depth_top10_usd"] == pytest.approx(199.0)


# ─── interval_to_ms ──────────────────────────────────────────────────


class TestIntervalToMs:
    def test_known_intervals(self):
        assert interval_to_ms("1m") == 60_000
        assert interval_to_ms("5m") == 300_000
        assert interval_to_ms("15m") == 900_000
        assert interval_to_ms("1h") == 3_600_000
        assert interval_to_ms("4h") == 14_400_000
        assert interval_to_ms("1d") == 86_400_000

    def test_unknown_interval_raises(self):
        with pytest.raises(ValueError, match="unknown interval"):
            interval_to_ms("3h")  # not in the standard map


# ─── End-to-end on synthetic data ───────────────────────────────────


class TestDownloadL2Hour:
    """Mocked S3 client — no real AWS calls."""

    def _mock_s3_returning(self, payload: bytes):
        """Build a mock S3 client whose get_object returns the given bytes."""
        import lz4.frame
        compressed = lz4.frame.compress(payload)

        class _MockBody:
            def __init__(self, data):
                self._data = data
            def read(self):
                return self._data

        class _MockS3:
            def __init__(self):
                self.last_call = None
            def get_object(self, **kwargs):
                self.last_call = kwargs
                return {"Body": _MockBody(compressed)}
        return _MockS3()

    def _mock_s3_raising(self, exc: Exception):
        class _MockS3:
            def get_object(self, **kwargs):
                raise exc
        return _MockS3()

    def test_download_writes_decompressed_jsonl(self, tmp_path):
        from rift_data.s3.l2_books import download_l2_hour
        payload = b'{"raw":{"data":{"coin":"BTC","time":1,"levels":[[],[]]}}}\n'
        s3 = self._mock_s3_returning(payload)
        path = download_l2_hour(s3, "BTC", "20240601", 12, cache_dir=tmp_path)
        assert path is not None
        assert path.exists()
        # File should be the decompressed payload, not the lz4 blob
        assert path.read_bytes() == payload

    def test_download_uses_correct_s3_key(self, tmp_path):
        from rift_data.s3.l2_books import L2_ARCHIVE_BUCKET, download_l2_hour
        s3 = self._mock_s3_returning(b'{"raw":{"data":{"coin":"BTC","time":1,"levels":[[],[]]}}}\n')
        download_l2_hour(s3, "BTC", "20240601", 12, cache_dir=tmp_path)
        assert s3.last_call["Bucket"] == L2_ARCHIVE_BUCKET
        assert s3.last_call["Key"] == "market_data/20240601/12/l2Book/BTC.lz4"
        assert s3.last_call["RequestPayer"] == "requester"

    def test_cache_hit_skips_download(self, tmp_path):
        from rift_data.s3.l2_books import download_l2_hour
        # Pre-create the cache file
        cache_file = tmp_path / "20240601" / "12" / "BTC.jsonl"
        cache_file.parent.mkdir(parents=True)
        cache_file.write_bytes(b"already cached\n")
        # Mock S3 that would raise if called — proves we didn't call it
        s3 = self._mock_s3_raising(RuntimeError("should not be called"))
        path = download_l2_hour(s3, "BTC", "20240601", 12, cache_dir=tmp_path)
        assert path == cache_file
        assert path.read_bytes() == b"already cached\n"

    def test_download_failure_returns_none(self, tmp_path):
        from rift_data.s3.l2_books import download_l2_hour
        from botocore.exceptions import ClientError
        s3 = self._mock_s3_raising(
            ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "GetObject")
        )
        path = download_l2_hour(s3, "BTC", "20240601", 12, cache_dir=tmp_path)
        assert path is None


class TestSyncL2Candles:
    """Orchestrator tested with a fake S3 client + tmp dirs."""

    def _build_lz4_payload_for_hour(
        self, base_ts_ms: int, snapshots: int = 4
    ) -> bytes:
        """Hour-specific payload — snapshots span [base_ts, base_ts + 1h)."""
        import lz4.frame
        lines = []
        for i in range(snapshots):
            ts_ms = base_ts_ms + i * 15 * 60 * 1000  # 15-min spacing within the hour
            lines.append(json.dumps({
                "time": "x",
                "ver_num": 1,
                "raw": {
                    "channel": "l2Book",
                    "data": {
                        "coin": "BTC",
                        "time": ts_ms,
                        "levels": [
                            [{"px": "99.5", "sz": "1.0", "n": 1}],
                            [{"px": "100.5", "sz": "1.0", "n": 1}],
                        ],
                    },
                },
            }))
        raw = "\n".join(lines).encode("utf-8")
        return lz4.frame.compress(raw)

    def _mock_s3_for_one_day(self):
        """Mock that returns date+hour-appropriate payloads keyed off the S3 path."""
        import re
        from datetime import datetime, timezone

        builder = self._build_lz4_payload_for_hour

        class _MockBody:
            def __init__(self, data):
                self._data = data
            def read(self):
                return self._data

        class _MockS3:
            def __init__(self):
                self.call_count = 0
            def get_object(self, **kwargs):
                self.call_count += 1
                # Key format: market_data/{YYYYMMDD}/{H}/l2Book/{COIN}.lz4
                key = kwargs.get("Key", "")
                m = re.match(r"market_data/(\d+)/(\d+)/l2Book/.*", key)
                if m:
                    date_str = m.group(1)
                    hour = int(m.group(2))
                    # Date string → epoch ms
                    dt = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
                    date_base_ms = int(dt.timestamp() * 1000)
                else:
                    date_base_ms = 0
                    hour = 0
                base_ts = date_base_ms + hour * 3_600_000
                return {"Body": _MockBody(builder(base_ts))}

        return _MockS3()

    def test_sync_one_day_one_timeframe_writes_parquet(self, tmp_path, monkeypatch):
        """End-to-end: mocked S3 → l2_books.sync_l2_candles → parquet on disk."""
        import polars as pl
        from rift_data.s3 import l2_books

        # Point the raw-cache somewhere temp so we don't touch ~/.rift/raw_l2
        monkeypatch.setattr(l2_books, "RAW_L2_CACHE_DIR", tmp_path / "raw_l2")

        mock_s3 = self._mock_s3_for_one_day()
        results = l2_books.sync_l2_candles(
            coins=["BTC"],
            timeframes=["1h"],
            start_date="2024-06-01",
            end_date="2024-06-01",
            incremental=False,
            data_dir=tmp_path / "data",
            s3_client=mock_s3,
            on_progress=None,
        )

        # Should have called S3 24 times (one per hour)
        assert mock_s3.call_count == 24

        # Result reports rows added per (coin, interval)
        assert results == {"BTC": {"1h": 24}}  # 24 hours of bars

        # Parquet was written with the right schema
        parquet_path = tmp_path / "data" / "BTC" / "1h" / "candles.parquet"
        assert parquet_path.exists()
        df = pl.read_parquet(parquet_path)
        assert df.height == 24
        # Schema includes both core OHLC + the new depth columns
        expected_cols = {
            "timestamp", "open", "high", "low", "close", "volume", "num_trades",
            "bid_depth_top10_usd", "ask_depth_top10_usd", "order_book_imbalance",
            "mean_spread_bps", "max_bid_wall_usd", "max_ask_wall_usd", "n_snapshots",
        }
        assert expected_cols.issubset(set(df.columns))
        # OHLC values: payload has bids[99.5], asks[100.5] → mid = 100.0 throughout
        assert df["open"].to_list() == [100.0] * 24
        assert df["close"].to_list() == [100.0] * 24
        # volume / num_trades should all be null (L2 doesn't carry them)
        assert df["volume"].null_count() == 24
        assert df["num_trades"].null_count() == 24

    def test_sync_checkpoint_skips_completed_days(self, tmp_path, monkeypatch):
        """Second sync call should be a no-op when incremental + checkpoint present."""
        from rift_data.s3 import l2_books

        monkeypatch.setattr(l2_books, "RAW_L2_CACHE_DIR", tmp_path / "raw_l2")
        mock_s3 = self._mock_s3_for_one_day()

        # First sync
        l2_books.sync_l2_candles(
            coins=["BTC"], timeframes=["1h"],
            start_date="2024-06-01", end_date="2024-06-01",
            incremental=True,
            data_dir=tmp_path / "data",
            s3_client=mock_s3,
            max_parse_workers=1,
        )
        first_calls = mock_s3.call_count

        # Second sync — same range, should skip
        results = l2_books.sync_l2_candles(
            coins=["BTC"], timeframes=["1h"],
            start_date="2024-06-01", end_date="2024-06-01",
            incremental=True,
            data_dir=tmp_path / "data",
            s3_client=mock_s3,
            max_parse_workers=1,
        )
        # No new S3 calls — checkpoint says we already did 2024-06-01
        assert mock_s3.call_count == first_calls
        # And no new rows added
        assert results == {"BTC": {"1h": 0}}

    def test_batch_merge_reduces_parquet_reads(self, tmp_path, monkeypatch):
        """flush_batch_size=10 over 25 days should call parquet read ~3 times
        (days 1-10, 11-20, 21-25 = 3 flushes), not 25 times (one per day)."""
        from rift_data.s3 import l2_books
        import polars as pl

        monkeypatch.setattr(l2_books, "RAW_L2_CACHE_DIR", tmp_path / "raw_l2")

        # Wrap pl.read_parquet to count calls
        original_read = pl.read_parquet
        read_call_count = {"n": 0}

        def _counted_read(*args, **kwargs):
            read_call_count["n"] += 1
            return original_read(*args, **kwargs)

        monkeypatch.setattr(pl, "read_parquet", _counted_read)

        mock_s3 = self._mock_s3_for_one_day()
        l2_books.sync_l2_candles(
            coins=["BTC"],
            timeframes=["1h"],
            start_date="2024-06-01",
            end_date="2024-06-25",  # 25 days
            incremental=False,
            flush_batch_size=10,
            data_dir=tmp_path / "data",
            s3_client=mock_s3,
            max_parse_workers=1,
        )
        # 25 days / batch 10 = 3 flushes (10 + 10 + 5).
        # Each flush calls read_parquet at most ONCE per timeframe (skipped on first).
        # First flush: no existing parquet, 0 reads.
        # Second flush: existing parquet, 1 read for 1h timeframe.
        # Third flush: existing parquet, 1 read for 1h timeframe.
        # Total: ≤ 2 reads. Without batching it'd be 24.
        assert read_call_count["n"] <= 3, (
            f"expected ≤3 parquet reads with batching, got {read_call_count['n']}"
        )

    def test_batch_size_one_matches_per_day(self, tmp_path, monkeypatch):
        """flush_batch_size=1 should reproduce the old per-day behavior:
        checkpoint advances daily, and the result counts are correct."""
        from rift_data.s3 import l2_books
        monkeypatch.setattr(l2_books, "RAW_L2_CACHE_DIR", tmp_path / "raw_l2")

        mock_s3 = self._mock_s3_for_one_day()
        results = l2_books.sync_l2_candles(
            coins=["BTC"],
            timeframes=["1h"],
            start_date="2024-06-01",
            end_date="2024-06-03",  # 3 days
            incremental=False,
            flush_batch_size=1,
            data_dir=tmp_path / "data",
            s3_client=mock_s3,
            max_parse_workers=1,
        )
        # 3 days × 24 bars/day = 72 rows
        assert results["BTC"]["1h"] == 72
        # Checkpoint should reflect the last day
        meta_path = tmp_path / "data" / "BTC" / "_l2_sync_meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["last_completed_date"] == "2024-06-03"

    def test_batch_size_zero_raises(self, tmp_path):
        from rift_data.s3 import l2_books
        with pytest.raises(ValueError, match="flush_batch_size"):
            l2_books.sync_l2_candles(
                coins=["BTC"], timeframes=["1h"],
                start_date="2024-06-01", end_date="2024-06-01",
                flush_batch_size=0,
                data_dir=tmp_path / "data",
                s3_client=self._mock_s3_for_one_day(),
            )

    def test_sync_drops_raw_after_aggregate(self, tmp_path, monkeypatch):
        """drop_raw_after_aggregate=True should leave the raw cache empty."""
        from rift_data.s3 import l2_books

        raw_cache = tmp_path / "raw_l2"
        monkeypatch.setattr(l2_books, "RAW_L2_CACHE_DIR", raw_cache)
        mock_s3 = self._mock_s3_for_one_day()

        l2_books.sync_l2_candles(
            coins=["BTC"], timeframes=["1h"],
            start_date="2024-06-01", end_date="2024-06-01",
            incremental=False,
            data_dir=tmp_path / "data",
            s3_client=mock_s3,
            drop_raw_after_aggregate=True,
        )
        # Raw L2 files should be gone
        if raw_cache.exists():
            remaining = list(raw_cache.rglob("*.jsonl"))
            assert remaining == []


class TestUXBundle:
    """Tests for the OSS sync UX upgrade: 90-day default + 24 workers +
    ProcessPool parse + partial-bar merge equivalence."""

    def test_start_date_none_defaults_to_last_90_days(self, tmp_path, monkeypatch):
        """start_date=None → today minus 90 days."""
        from rift_data.s3 import l2_books

        monkeypatch.setattr(l2_books, "RAW_L2_CACHE_DIR", tmp_path / "raw_l2")

        # Build a mock that records all S3 keys requested
        keys_requested = []

        class _RecordingMock:
            def get_object(self, **kwargs):
                keys_requested.append(kwargs.get("Key", ""))
                raise Exception("404 — not interested in actual data here")

        l2_books.sync_l2_candles(
            coins=["BTC"],
            timeframes=["1h"],
            # start_date intentionally omitted → defaults to today - 90 days
            incremental=False,
            data_dir=tmp_path / "data",
            s3_client=_RecordingMock(),
            max_parse_workers=1,
        )
        # Should have requested SOMETHING (otherwise the default isn't doing anything)
        assert len(keys_requested) > 0
        # Extract dates from the keys (format: market_data/YYYYMMDD/H/l2Book/BTC.lz4)
        import re
        dates = sorted({
            re.search(r"market_data/(\d{8})/", k).group(1)
            for k in keys_requested if re.search(r"market_data/(\d{8})/", k)
        })
        # First date should be ~90 days ago
        from datetime import datetime, timedelta
        earliest = datetime.strptime(dates[0], "%Y%m%d")
        latest = datetime.strptime(dates[-1], "%Y%m%d")
        expected_first = datetime.utcnow().date() - timedelta(days=90)
        # ± 1 day tolerance for UTC clock-edge
        assert abs((earliest.date() - expected_first).days) <= 1
        # End should be today (± 1 day)
        assert abs((latest.date() - datetime.utcnow().date()).days) <= 1

    def test_explicit_start_date_overrides_default(self, tmp_path, monkeypatch):
        """Passing a real start_date string should NOT use the 90-day default."""
        from rift_data.s3 import l2_books

        monkeypatch.setattr(l2_books, "RAW_L2_CACHE_DIR", tmp_path / "raw_l2")
        keys_requested = []

        class _RecordingMock:
            def get_object(self, **kwargs):
                keys_requested.append(kwargs.get("Key", ""))
                raise Exception("404")

        l2_books.sync_l2_candles(
            coins=["BTC"], timeframes=["1h"],
            start_date="2024-01-01",
            end_date="2024-01-03",
            incremental=False,
            data_dir=tmp_path / "data",
            s3_client=_RecordingMock(),
            max_parse_workers=1,
        )
        import re
        dates = sorted({
            re.search(r"market_data/(\d{8})/", k).group(1)
            for k in keys_requested if re.search(r"market_data/(\d{8})/", k)
        })
        assert dates == ["20240101", "20240102", "20240103"]

    def test_default_workers_is_24(self):
        """The bundled UX upgrade raised max_download_workers default to 24."""
        from rift_data.s3 import l2_books
        import inspect
        sig = inspect.signature(l2_books.sync_l2_candles)
        assert sig.parameters["max_download_workers"].default == 24

    def test_default_parse_workers_is_4(self):
        from rift_data.s3 import l2_books
        import inspect
        sig = inspect.signature(l2_books.sync_l2_candles)
        assert sig.parameters["max_parse_workers"].default == 4

    def test_parse_workers_zero_raises(self, tmp_path):
        from rift_data.s3 import l2_books
        with pytest.raises(ValueError, match="max_parse_workers"):
            l2_books.sync_l2_candles(
                coins=["BTC"], timeframes=["1h"],
                start_date="2024-01-01", end_date="2024-01-01",
                max_parse_workers=0,
                data_dir=tmp_path / "data",
                s3_client=TestSyncL2Candles()._mock_s3_for_one_day(),
            )


class TestPartialBarMerge:
    """Equivalence: serial aggregation must produce IDENTICAL bars to
    per-hour aggregate + cross-hour merge."""

    def _build_snapshots_spanning_hours(self) -> list[L2Snapshot]:
        """Snapshots across 4 hours — exercises 4h bar partial merge."""
        snaps = []
        # 4 snapshots per hour for 4 hours (16 total)
        for h in range(4):
            for q in range(4):
                ts_ms = (h * 3600 + q * 900) * 1000  # 0, 900s, 1800s, 2700s per hour
                # Mid-price varies systematically so OHLC is non-trivial
                mid = 100.0 + h * 1.0 + q * 0.25
                # Depth varies too
                bid_sz = 1.0 + h * 0.5
                ask_sz = 1.0 + q * 0.5
                snaps.append(L2Snapshot(
                    timestamp_ms=ts_ms,
                    bids=[(mid - 0.5, bid_sz)],
                    asks=[(mid + 0.5, ask_sz)],
                ))
        return snaps

    def test_serial_vs_per_hour_then_merge_for_4h_bar(self):
        """4 hours of snapshots → 1 single 4h bar. Per-hour aggregate + merge
        must produce the SAME bar as single-pass aggregate."""
        from rift_data.s3.l2_books import _merge_partial_bars

        snaps = self._build_snapshots_spanning_hours()
        interval_ms = 14_400_000  # 4h

        # Serial baseline: aggregate the full stream
        serial_bars = aggregate_l2_to_candles(snaps, interval_ms=interval_ms)
        assert len(serial_bars) == 1
        serial = serial_bars[0]

        # Parallel path: aggregate per hour, then merge
        per_hour = []
        for h in range(4):
            hour_snaps = [s for s in snaps if h * 3_600_000 <= s.timestamp_ms < (h + 1) * 3_600_000]
            per_hour.append(aggregate_l2_to_candles(hour_snaps, interval_ms=interval_ms))
        merged_bars = _merge_partial_bars(per_hour)
        assert len(merged_bars) == 1
        merged = merged_bars[0]

        # Identical for OHLC + min/max walls + total n
        assert serial["timestamp"] == merged["timestamp"]
        assert serial["open"] == pytest.approx(merged["open"])
        assert serial["high"] == pytest.approx(merged["high"])
        assert serial["low"] == pytest.approx(merged["low"])
        assert serial["close"] == pytest.approx(merged["close"])
        assert serial["max_bid_wall_usd"] == pytest.approx(merged["max_bid_wall_usd"])
        assert serial["max_ask_wall_usd"] == pytest.approx(merged["max_ask_wall_usd"])
        assert serial["n_snapshots"] == merged["n_snapshots"]
        # Weighted-mean depth features — should match
        assert serial["bid_depth_top10_usd"] == pytest.approx(merged["bid_depth_top10_usd"], rel=1e-9)
        assert serial["ask_depth_top10_usd"] == pytest.approx(merged["ask_depth_top10_usd"], rel=1e-9)
        assert serial["order_book_imbalance"] == pytest.approx(merged["order_book_imbalance"], rel=1e-9)
        assert serial["mean_spread_bps"] == pytest.approx(merged["mean_spread_bps"], rel=1e-9)

    def test_serial_vs_per_hour_for_1h_bar_no_merge_needed(self):
        """1h bars are hour-aligned so no merging happens — but the code path
        still produces identical output."""
        from rift_data.s3.l2_books import _merge_partial_bars

        snaps = self._build_snapshots_spanning_hours()
        interval_ms = 3_600_000  # 1h

        serial_bars = aggregate_l2_to_candles(snaps, interval_ms=interval_ms)
        per_hour = []
        for h in range(4):
            hour_snaps = [s for s in snaps if h * 3_600_000 <= s.timestamp_ms < (h + 1) * 3_600_000]
            per_hour.append(aggregate_l2_to_candles(hour_snaps, interval_ms=interval_ms))
        merged_bars = _merge_partial_bars(per_hour)

        assert len(serial_bars) == len(merged_bars) == 4
        for s, m in zip(serial_bars, merged_bars):
            assert s["timestamp"] == m["timestamp"]
            assert s["open"] == pytest.approx(m["open"])
            assert s["close"] == pytest.approx(m["close"])
            assert s["high"] == pytest.approx(m["high"])
            assert s["low"] == pytest.approx(m["low"])
            assert s["n_snapshots"] == m["n_snapshots"]


class TestEndToEnd:
    def test_parse_then_aggregate(self, tmp_path):
        """One-shot pipeline: build jsonl → parse → aggregate → verify."""
        lines = [
            _build_snapshot_line(60_000, [(99.0, 1.0)], [(101.0, 1.0)]),
            _build_snapshot_line(90_000, [(100.0, 1.0)], [(102.0, 1.0)]),
            _build_snapshot_line(120_000, [(101.0, 1.0)], [(103.0, 1.0)]),
            _build_snapshot_line(150_000, [(102.0, 1.0)], [(104.0, 1.0)]),
        ]
        f = _write_jsonl(tmp_path, lines)
        bars = aggregate_l2_to_candles(parse_l2_jsonl(f), interval_ms=60_000)
        # Two bars: 60_000 (snaps 60k, 90k) and 120_000 (snaps 120k, 150k)
        assert len(bars) == 2
        assert bars[0]["timestamp"] == 60_000
        assert bars[1]["timestamp"] == 120_000
        # First bar: opens at mid(99, 101) = 100; closes at mid(100, 102) = 101
        assert bars[0]["open"] == 100.0
        assert bars[0]["close"] == 101.0
        # Second bar: opens at mid(101, 103) = 102; closes at mid(102, 104) = 103
        assert bars[1]["open"] == 102.0
        assert bars[1]["close"] == 103.0
