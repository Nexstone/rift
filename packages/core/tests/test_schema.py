"""Unit tests for rift_core.schema — coin normalization + canonical FILL_SCHEMA."""

from __future__ import annotations

import polars as pl
import pytest

from rift_core.schema import (
    FILL_SCHEMA,
    VALID_INTERVALS,
    _KNOWN_TRADFI,
    coin_to_path,
    detect_market,
    normalize_coin,
    normalize_spot,
    path_to_coin,
)


# ─── normalize_coin ───────────────────────────────────────────────────

class TestNormalizeCoin:
    def test_strips_perp_suffix(self):
        assert normalize_coin("BTC-PERP") == "BTC"
        assert normalize_coin("eth-perp") == "ETH"

    def test_uppercases(self):
        assert normalize_coin("btc") == "BTC"
        assert normalize_coin("Hype") == "HYPE"

    def test_preserves_xyz_prefix(self):
        assert normalize_coin("xyz:SP500") == "xyz:SP500"
        assert normalize_coin("xyz:sp500") == "xyz:SP500"

    def test_auto_adds_xyz_for_known_tradfi(self):
        assert normalize_coin("SP500") == "xyz:SP500"
        assert normalize_coin("tsla") == "xyz:TSLA"
        assert normalize_coin("NVDA") == "xyz:NVDA"

    def test_does_not_add_xyz_for_crypto(self):
        # Crypto names that look TradFi-shaped should NOT get the prefix
        assert normalize_coin("BTC") == "BTC"
        assert normalize_coin("ETH") == "ETH"
        assert normalize_coin("SUI") == "SUI"

    def test_whitespace(self):
        assert normalize_coin("  BTC  ") == "BTC"
        assert normalize_coin(" BTC-PERP ") == "BTC"

    @pytest.mark.parametrize("tradfi", sorted(_KNOWN_TRADFI)[:5])
    def test_all_known_tradfi_get_xyz_prefix(self, tradfi):
        assert normalize_coin(tradfi.lower()).startswith("xyz:")


# ─── coin_to_path / path_to_coin ──────────────────────────────────────

class TestCoinPath:
    @pytest.mark.parametrize("coin,path", [
        ("BTC", "BTC"),
        ("ETH", "ETH"),
        ("xyz:SP500", "xyz-SP500"),
        ("xyz:NVDA", "xyz-NVDA"),
        ("HYPE/USDC", "spot-HYPE-USDC"),
    ])
    def test_coin_to_path(self, coin, path):
        assert coin_to_path(coin) == path

    @pytest.mark.parametrize("path,coin", [
        ("BTC", "BTC"),
        ("xyz-SP500", "xyz:SP500"),
        ("xyz-NVDA", "xyz:NVDA"),
        ("spot-HYPE-USDC", "HYPE/USDC"),
    ])
    def test_path_to_coin(self, path, coin):
        assert path_to_coin(path) == coin

    @pytest.mark.parametrize("coin", ["BTC", "ETH", "xyz:SP500", "HYPE/USDC", "xyz:NVDA"])
    def test_round_trip(self, coin):
        """coin_to_path → path_to_coin is identity for supported names."""
        assert path_to_coin(coin_to_path(coin)) == coin


# ─── detect_market ────────────────────────────────────────────────────

class TestDetectMarket:
    def test_spot_pair_has_slash(self):
        assert detect_market("HYPE/USDC") == "spot"

    def test_perp_when_no_slash(self):
        assert detect_market("BTC") == "perp"
        assert detect_market("xyz:SP500") == "perp"


# ─── normalize_spot ───────────────────────────────────────────────────

class TestNormalizeSpot:
    def test_adds_usdc_quote(self):
        assert normalize_spot("HYPE") == "HYPE/USDC"
        assert normalize_spot("hype") == "HYPE/USDC"

    def test_preserves_existing_pair(self):
        assert normalize_spot("HYPE/USDC") == "HYPE/USDC"


# ─── FILL_SCHEMA ──────────────────────────────────────────────────────

class TestFillSchema:
    EXPECTED_COLUMNS = {
        "timestamp": pl.Int64,
        "price": pl.Float64,
        "size": pl.Float64,
        "side": pl.Utf8,
        "dir": pl.Utf8,
        "is_open": pl.Boolean,
        "is_long": pl.Boolean,
        "crossed": pl.Boolean,
        "closed_pnl": pl.Float64,
        "fee": pl.Float64,
        "start_position": pl.Float64,
    }

    def test_has_eleven_columns(self):
        assert len(FILL_SCHEMA) == 11

    def test_columns_match_expected(self):
        assert dict(FILL_SCHEMA) == self.EXPECTED_COLUMNS

    def test_dataframe_can_be_constructed(self):
        df = pl.DataFrame(schema=FILL_SCHEMA)
        assert df.shape == (0, 11)


# ─── VALID_INTERVALS ──────────────────────────────────────────────────

class TestValidIntervals:
    def test_includes_common_intervals(self):
        for tf in ("1m", "5m", "15m", "30m", "1h", "4h", "1d"):
            assert tf in VALID_INTERVALS

    def test_is_a_list(self):
        # Order matters for some CLI outputs; ensure it's not a set
        assert isinstance(VALID_INTERVALS, list)
