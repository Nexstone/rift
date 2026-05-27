"""Unit tests for inventory scanner."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import polars as pl
import pytest

from rift_substrate.data.inventory import (
    CoinInventory,
    InventoryReport,
    TimeframeInfo,
    inventory,
)


def _write_test_candles(d: Path, n: int = 100):
    df = pl.DataFrame({
        "timestamp": list(range(n)),
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.5] * n,
        "volume": [1.0] * n,
    })
    d.mkdir(parents=True, exist_ok=True)
    df.write_parquet(d / "candles.parquet")


def _write_test_funding(d: Path, n: int = 100):
    df = pl.DataFrame({
        "timestamp": list(range(n)),
        "funding_rate": [0.0001] * n,
    })
    d.mkdir(parents=True, exist_ok=True)
    df.write_parquet(d / "rates.parquet")


def _write_test_fill_day(d: Path, day: str):
    df = pl.DataFrame({
        "timestamp": list(range(100)),
        "price": [100.0] * 100,
        "size": [1.0] * 100,
        "side": ["B"] * 100,
    })
    d.mkdir(parents=True, exist_ok=True)
    df.write_parquet(d / f"{day}.parquet")


class TestInventoryReport:
    def test_empty_inventory_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        rep = inventory()
        assert rep.total_coins == 0
        summary = rep.summary()
        assert "No cached data" in summary
        assert "rift fetch" in summary

    def test_inventory_with_one_coin_one_tf(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_test_candles(tmp_path / "BTC" / "1h", n=50)

        rep = inventory()
        assert rep.total_coins == 1
        assert rep.has_coin("BTC")
        ci = rep.get("BTC")
        assert ci is not None
        assert ci.has_candles
        assert len(ci.timeframes) == 1
        assert ci.timeframes[0].tf == "1h"
        assert ci.timeframes[0].rows == 50

    def test_inventory_with_funding(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_test_candles(tmp_path / "BTC" / "1h")
        _write_test_funding(tmp_path / "BTC" / "funding")

        rep = inventory()
        ci = rep.get("BTC")
        assert ci.has_funding
        assert ci.funding_rows > 0

    def test_inventory_with_fills(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_test_candles(tmp_path / "BTC" / "1h")
        _write_test_fill_day(tmp_path / "BTC" / "fills", "20250101")
        _write_test_fill_day(tmp_path / "BTC" / "fills", "20250102")

        rep = inventory()
        ci = rep.get("BTC")
        assert ci.has_fills
        assert ci.fill_days == 2
        assert ci.fill_first_day == "2025-01-01"
        assert ci.fill_last_day == "2025-01-02"

    def test_filter_to_specific_coins(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_test_candles(tmp_path / "BTC" / "1h")
        _write_test_candles(tmp_path / "ETH" / "1h")
        _write_test_candles(tmp_path / "SOL" / "1h")

        rep = inventory(coins=["BTC", "ETH"])
        assert rep.total_coins == 2
        assert rep.has_coin("BTC")
        assert rep.has_coin("ETH")
        assert not rep.has_coin("SOL")

    def test_coins_with_candles_at_filter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_test_candles(tmp_path / "BTC" / "1h")
        _write_test_candles(tmp_path / "BTC" / "5m")
        _write_test_candles(tmp_path / "ETH" / "1h")

        rep = inventory()
        coins_1h = rep.coins_with_candles_at("1h")
        assert set(coins_1h) == {"BTC", "ETH"}
        coins_5m = rep.coins_with_candles_at("5m")
        assert set(coins_5m) == {"BTC"}

    def test_summary_includes_capability_info(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_test_candles(tmp_path / "BTC" / "1h", n=200)
        _write_test_funding(tmp_path / "BTC" / "funding", n=100)

        rep = inventory()
        s = rep.summary()
        assert "BTC" in s
        assert "1h(200)" in s
        assert "Statistical tests" in s
        assert "Funding rate" in s

    def test_to_dict_serializable(self, tmp_path, monkeypatch):
        import json
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_test_candles(tmp_path / "BTC" / "1h")
        rep = inventory()
        d = rep.to_dict()
        # Must round-trip through JSON
        assert json.dumps(d)
