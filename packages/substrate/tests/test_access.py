"""Unit tests for Data.load() and access semantics."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from rift_substrate.data.access import Data, DataNotAvailable, _parse_freq


def _write_candles(d: Path, n: int = 100, start_ts: int = 0, step: int = 60_000):
    df = pl.DataFrame({
        "timestamp": [start_ts + i * step for i in range(n)],
        "open": [100.0 + i * 0.01 for i in range(n)],
        "high": [100.5 + i * 0.01 for i in range(n)],
        "low": [99.5 + i * 0.01 for i in range(n)],
        "close": [100.2 + i * 0.01 for i in range(n)],
        "volume": [1.0 + (i % 10) * 0.1 for i in range(n)],
        "funding_rate": [0.0001 for _ in range(n)],
    })
    d.mkdir(parents=True, exist_ok=True)
    df.write_parquet(d / "candles.parquet")


class TestParseFreq:
    def test_time_freq(self):
        kind, threshold = _parse_freq("1h")
        assert kind == "time"
        assert threshold is None

    def test_volume_freq(self):
        kind, threshold = _parse_freq("volume:100")
        assert kind == "volume"
        assert threshold == 100.0

    def test_dollar_freq(self):
        kind, threshold = _parse_freq("dollar:1000000")
        assert kind == "dollar"
        assert threshold == 1_000_000.0

    def test_unknown_bar_type_rejected(self):
        with pytest.raises(ValueError, match="Unknown bar type"):
            _parse_freq("renko:50")

    def test_invalid_threshold_rejected(self):
        with pytest.raises(ValueError):
            _parse_freq("volume:abc")

    def test_negative_threshold_rejected(self):
        with pytest.raises(ValueError):
            _parse_freq("volume:-1")


class TestDataLoad:
    def test_load_btc_close(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_candles(tmp_path / "BTC" / "1h", n=50)

        lf = Data.load(coins=["BTC"], fields=["close"], freq="1h")
        df = lf.collect()
        assert len(df) == 50
        assert "close" in df.columns
        assert "coin" in df.columns
        assert df["coin"][0] == "BTC"

    def test_load_multi_coin(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_candles(tmp_path / "BTC" / "1h", n=30)
        _write_candles(tmp_path / "ETH" / "1h", n=30)

        lf = Data.load(coins=["BTC", "ETH"], fields=["close"], freq="1h")
        df = lf.collect()
        assert len(df) == 60
        coins = set(df["coin"].to_list())
        assert coins == {"BTC", "ETH"}

    def test_load_string_coin_normalized(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_candles(tmp_path / "BTC" / "1h", n=20)

        # Single coin as string (not list)
        lf = Data.load(coins="btc", fields=["close"], freq="1h")
        df = lf.collect()
        assert len(df) == 20

    def test_load_unknown_field_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_candles(tmp_path / "BTC" / "1h")

        with pytest.raises(ValueError, match="Unknown field"):
            Data.load(coins=["BTC"], fields=["nonexistent_field"], freq="1h")

    def test_load_missing_data_raises_DataNotAvailable(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        # BTC has 1h but not 5m
        _write_candles(tmp_path / "BTC" / "1h")

        with pytest.raises(DataNotAvailable) as ei:
            Data.load(coins=["BTC"], fields=["close"], freq="5m")
        assert "rift fetch" in str(ei.value)

    def test_load_field_requiring_fills_raises_with_sync_hint(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_candles(tmp_path / "BTC" / "1h")
        # taker_ratio requires fills, which aren't there

        with pytest.raises(DataNotAvailable) as ei:
            Data.load(coins=["BTC"], fields=["close", "taker_ratio"], freq="1h")
        assert "sync" in str(ei.value).lower()

    def test_load_volume_bars(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_candles(tmp_path / "BTC" / "1m", n=200)

        lf = Data.load(coins=["BTC"], fields=["close", "volume"], freq="volume:10")
        df = lf.collect()
        # 200 1-min bars at ~1-2 BTC each = ~200-400 BTC total
        # threshold 10 → ~20-40 volume bars
        assert 10 <= len(df) <= 50

    def test_load_dollar_bars(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_candles(tmp_path / "BTC" / "1m", n=200)

        lf = Data.load(coins=["BTC"], fields=["close", "volume"], freq="dollar:500")
        df = lf.collect()
        assert len(df) > 0

    def test_inventory_via_Data(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_candles(tmp_path / "BTC" / "1h")

        rep = Data.inventory()
        assert rep.has_coin("BTC")

    def test_available_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_candles(tmp_path / "BTC" / "1h")

        af = Data.available_fields()
        assert "BTC" in af
        assert "close" in af["BTC"]
        assert "taker_ratio" not in af["BTC"]  # no fills cached
