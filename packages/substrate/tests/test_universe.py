"""Unit tests for Universe selection."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from rift_substrate.universe import (
    AssetMetadata,
    Universe,
    UniverseSpec,
)


def _write_candles(d: Path, n: int = 10):
    df = pl.DataFrame({
        "timestamp": list(range(n)),
        "close": [100.0] * n,
    })
    d.mkdir(parents=True, exist_ok=True)
    df.write_parquet(d / "candles.parquet")


class TestFromCache:
    def test_empty_cache_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        uni = Universe.from_cache()
        assert len(uni) == 0

    def test_populated_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr("rift_substrate.data.paths.DATA_DIR", tmp_path)
        _write_candles(tmp_path / "BTC" / "1h")
        _write_candles(tmp_path / "ETH" / "1h")

        uni = Universe.from_cache()
        assert len(uni) == 2
        assert "BTC" in uni
        assert "ETH" in uni
        assert uni.source == "cache"


class TestFromList:
    def test_explicit_list(self):
        uni = Universe.from_list(["BTC", "eth", "Sol"])
        # All uppercase, sorted
        assert uni.coins == ["BTC", "ETH", "SOL"]
        assert uni.source == "list"

    def test_metadata_populated(self):
        uni = Universe.from_list(["BTC"])
        assert "BTC" in uni.metadata
        meta = uni.metadata["BTC"]
        assert isinstance(meta, AssetMetadata)


class TestFromSectors:
    def test_includes_only_tagged_coins(self):
        uni = Universe.from_sectors(["L1"])
        # BTC, ETH, SOL are tagged L1 in vendored sector_tags.json
        assert "BTC" in uni
        assert "ETH" in uni
        # DOGE (Meme) shouldn't be in L1
        assert "DOGE" not in uni

    def test_meme_sector(self):
        uni = Universe.from_sectors(["Meme"])
        assert "DOGE" in uni
        assert "BTC" not in uni

    def test_multiple_sectors_union(self):
        uni = Universe.from_sectors(["L1", "Meme"])
        # Both L1 (BTC) and Meme (DOGE) should be included
        assert "BTC" in uni
        assert "DOGE" in uni

    def test_unknown_sector_returns_empty(self):
        uni = Universe.from_sectors(["NonexistentSector"])
        assert len(uni) == 0


class TestSetOperations:
    def test_intersection(self):
        a = Universe.from_list(["BTC", "ETH", "SOL"])
        b = Universe.from_list(["ETH", "SOL", "AVAX"])
        common = Universe.intersection(a, b)
        assert common.coins == ["ETH", "SOL"]

    def test_difference(self):
        a = Universe.from_list(["BTC", "ETH", "SOL"])
        b = Universe.from_list(["ETH"])
        diff = Universe.difference(a, b)
        assert diff.coins == ["BTC", "SOL"]

    def test_union(self):
        a = Universe.from_list(["BTC", "ETH"])
        b = Universe.from_list(["ETH", "SOL"])
        u = Universe.union(a, b)
        assert u.coins == ["BTC", "ETH", "SOL"]

    def test_intersection_of_empty(self):
        u = Universe.intersection()
        assert len(u) == 0


class TestUniverseSpec:
    def test_iteration(self):
        uni = Universe.from_list(["BTC", "ETH"])
        coins = list(uni)
        assert coins == ["BTC", "ETH"]

    def test_contains_normalizes(self):
        uni = Universe.from_list(["BTC"])
        assert "btc" in uni
        assert "BTC" in uni
        assert "ETH" not in uni

    def test_to_dict_serializable(self):
        import json
        uni = Universe.from_list(["BTC", "ETH"])
        d = uni.to_dict()
        assert json.dumps(d)


# ─── from_hl_data ─────────────────────────────────────────────────────


def _mock_hl_response(coins_with_vol: list[tuple[str, float]]) -> tuple[dict, list]:
    """Build a (meta, asset_ctxs) tuple matching HL's info.meta_and_asset_ctxs() shape."""
    meta = {
        "universe": [
            {"name": coin, "szDecimals": 3, "maxLeverage": 50}
            for coin, _ in coins_with_vol
        ]
    }
    asset_ctxs = [
        {"dayNtlVlm": str(vol), "funding": "0.0001", "openInterest": "1000"}
        for _, vol in coins_with_vol
    ]
    return meta, asset_ctxs


class TestFromHlData:
    def test_builds_universe_from_prefetched_data(self):
        meta, ctxs = _mock_hl_response([
            ("BTC", 1_000_000_000),
            ("ETH", 500_000_000),
            ("SOL", 200_000_000),
        ])
        uni = Universe.from_hl_data(meta, ctxs)
        assert set(uni.coins) == {"BTC", "ETH", "SOL"}
        assert uni.source == "hl_data"
        assert uni.metadata["BTC"].avg_volume_24h_usd == 1_000_000_000

    def test_filters_by_min_volume(self):
        meta, ctxs = _mock_hl_response([
            ("BTC", 1_000_000_000),
            ("DEAD", 1_000),
        ])
        uni = Universe.from_hl_data(meta, ctxs, min_volume_24h_usd=100_000)
        assert "BTC" in uni
        assert "DEAD" not in uni

    def test_exclude_drops_coins(self):
        meta, ctxs = _mock_hl_response([
            ("BTC", 1_000_000_000),
            ("ETH", 500_000_000),
        ])
        uni = Universe.from_hl_data(meta, ctxs, exclude=["ETH"])
        assert "BTC" in uni
        assert "ETH" not in uni

    def test_include_only_filters_to_subset(self):
        meta, ctxs = _mock_hl_response([
            ("BTC", 1_000_000_000),
            ("ETH", 500_000_000),
            ("SOL", 200_000_000),
        ])
        uni = Universe.from_hl_data(meta, ctxs, include_only=["BTC", "SOL"])
        assert set(uni.coins) == {"BTC", "SOL"}

    def test_uppercase_normalization(self):
        meta = {"universe": [{"name": "btc"}]}
        ctxs = [{"dayNtlVlm": "1000000"}]
        uni = Universe.from_hl_data(meta, ctxs)
        assert "BTC" in uni.coins

    def test_size_decimals_and_max_leverage_carried(self):
        meta = {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 20}]}
        ctxs = [{"dayNtlVlm": "1000000"}]
        uni = Universe.from_hl_data(meta, ctxs)
        assert uni.metadata["BTC"].size_decimals == 5
        assert uni.metadata["BTC"].max_leverage == 20


# ─── top_by_volume ────────────────────────────────────────────────────


class TestTopByVolume:
    def test_picks_top_n_by_volume(self):
        meta, ctxs = _mock_hl_response([
            ("LOW", 100_000),
            ("HIGH", 1_000_000_000),
            ("MID", 50_000_000),
            ("HIGHER", 5_000_000_000),
        ])
        uni = Universe.from_hl_data(meta, ctxs)
        top2 = uni.top_by_volume(2)
        assert set(top2.coins) == {"HIGHER", "HIGH"}

    def test_n_greater_than_size_returns_all(self):
        meta, ctxs = _mock_hl_response([("BTC", 1e9), ("ETH", 1e8)])
        uni = Universe.from_hl_data(meta, ctxs)
        top10 = uni.top_by_volume(10)
        assert set(top10.coins) == {"BTC", "ETH"}

    def test_zero_returns_empty(self):
        meta, ctxs = _mock_hl_response([("BTC", 1e9)])
        uni = Universe.from_hl_data(meta, ctxs)
        assert uni.top_by_volume(0).coins == []
        assert uni.top_by_volume(-3).coins == []

    def test_preserves_metadata(self):
        meta, ctxs = _mock_hl_response([("BTC", 1e9), ("ETH", 1e8)])
        uni = Universe.from_hl_data(meta, ctxs)
        top = uni.top_by_volume(1)
        assert "BTC" in top.metadata
        assert top.metadata["BTC"].avg_volume_24h_usd == 1e9

    def test_source_lineage(self):
        meta, ctxs = _mock_hl_response([("BTC", 1e9)])
        uni = Universe.from_hl_data(meta, ctxs)
        top = uni.top_by_volume(1)
        assert top.source == "hl_data+top_by_volume"

    def test_handles_missing_volume_metadata(self):
        """Specs from from_list have no avg_volume — should still work, those rank last."""
        uni = Universe.from_list(["BTC", "ETH"])
        top = uni.top_by_volume(1)
        # Just pick the first one alphabetically; behavior shouldn't crash
        assert len(top.coins) == 1
        assert top.coins[0] in {"BTC", "ETH"}
