"""Unit tests for rift_data.account_mode — collateral reading + mode detection.

No network. All HL Info calls mocked.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from rift_data.account_mode import (
    CollateralBreakdown,
    hl_native_mode,
    query_account_mode,
    read_collateral,
)


# ─── Fixtures ─────────────────────────────────────────────────────────


def _make_info(
    abstraction: str,
    perp_account_value: str = "0",
    perp_margin_used: str = "0",
    spot_usdc: str = "0",
    positions: list | None = None,
) -> MagicMock:
    """Build a mock Info client that returns canned HL responses."""
    info = MagicMock()
    info.query_user_abstraction_state.return_value = abstraction
    info.user_state.return_value = {
        "marginSummary": {
            "accountValue": perp_account_value,
            "totalMarginUsed": perp_margin_used,
        },
        "assetPositions": positions or [],
    }
    info.spot_user_state.return_value = {
        "balances": [
            {"coin": "USDC", "token": 0, "total": spot_usdc, "hold": "0"},
            {"coin": "HYPE", "token": 999, "total": "0", "hold": "0"},
        ],
    }
    return info


# ─── query_account_mode ───────────────────────────────────────────────


class TestQueryAccountMode:
    def test_standard(self):
        info = _make_info("disabled")
        assert query_account_mode(info, "0xabc") == "standard"

    def test_unified(self):
        info = _make_info("unifiedAccount")
        assert query_account_mode(info, "0xabc") == "unified"

    def test_portfolio_margin(self):
        info = _make_info("portfolioMargin")
        assert query_account_mode(info, "0xabc") == "portfolio_margin"

    def test_unknown_string_falls_back_to_unknown(self):
        info = _make_info("someNewModeHLAdded")
        assert query_account_mode(info, "0xabc") == "unknown"

    def test_non_string_response_is_unknown(self):
        info = _make_info(None)  # type: ignore[arg-type]
        info.query_user_abstraction_state.return_value = False
        assert query_account_mode(info, "0xabc") == "unknown"

    def test_address_is_lowercased_for_query(self):
        info = _make_info("disabled")
        query_account_mode(info, "0xABCdef")
        info.query_user_abstraction_state.assert_called_once_with("0xabcdef")


# ─── hl_native_mode ────────────────────────────────────────────────────


class TestHlNativeMode:
    def test_standard_maps_to_disabled(self):
        assert hl_native_mode("standard") == "disabled"

    def test_unified_maps_to_unifiedAccount(self):
        assert hl_native_mode("unified") == "unifiedAccount"

    def test_portfolio_maps_to_portfolioMargin(self):
        assert hl_native_mode("portfolio_margin") == "portfolioMargin"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown account mode"):
            hl_native_mode("bogus")  # type: ignore[arg-type]


# ─── read_collateral — Standard ───────────────────────────────────────


class TestReadCollateralStandard:
    def test_perp_only_does_not_count_spot(self):
        info = _make_info(
            "disabled",
            perp_account_value="500",
            perp_margin_used="100",
            spot_usdc="999",
        )
        c = read_collateral(info, "0xabc")
        assert c.mode == "standard"
        assert c.perp_available == Decimal("400")
        assert c.spot_usdc == Decimal("999")
        # Critical: standard total IGNORES spot
        assert c.total == Decimal("400")
        assert c.perp_only is True

    def test_zero_collateral(self):
        info = _make_info("disabled")
        c = read_collateral(info, "0xabc")
        assert c.total == Decimal("0")
        assert c.spot_usdc == Decimal("0")


# ─── read_collateral — Unified ────────────────────────────────────────


class TestReadCollateralUnified:
    def test_spot_usdc_is_summed_into_total(self):
        info = _make_info(
            "unifiedAccount",
            perp_account_value="0",   # unified often shows $0 perp
            perp_margin_used="0",
            spot_usdc="697.74",
        )
        c = read_collateral(info, "0xabc")
        assert c.mode == "unified"
        assert c.perp_available == Decimal("0")
        assert c.spot_usdc == Decimal("697.74")
        assert c.total == Decimal("697.74")
        assert c.perp_only is False

    def test_perp_plus_spot(self):
        info = _make_info(
            "unifiedAccount",
            perp_account_value="100",
            perp_margin_used="20",
            spot_usdc="500",
        )
        c = read_collateral(info, "0xabc")
        # 100 - 20 (perp_available) + 500 (spot)
        assert c.total == Decimal("580")


# ─── read_collateral — Portfolio Margin ───────────────────────────────


class TestReadCollateralPortfolioMargin:
    def test_same_as_unified_for_usdc(self):
        """Per HL docs (account-abstraction-modes): under PM, the perp
        clearinghouse state is 'not meaningful'. Real trading collateral
        is in spot. Same handling as unified."""
        info = _make_info(
            "portfolioMargin",
            perp_account_value="0",
            perp_margin_used="0",
            spot_usdc="500",
        )
        c = read_collateral(info, "0xabc")
        assert c.mode == "portfolio_margin"
        assert c.total == Decimal("500")
        assert c.spot_usdc == Decimal("500")

    def test_pm_with_open_perp_position(self):
        """If a PM user has an open perp position, margin_used is real
        and reduces available collateral."""
        info = _make_info(
            "portfolioMargin",
            perp_account_value="100",  # rare but possible — small perp leg
            perp_margin_used="20",
            spot_usdc="500",
        )
        c = read_collateral(info, "0xabc")
        # 100 - 20 (perp) + 500 (spot) = 580
        assert c.total == Decimal("580")


# ─── read_collateral — Unknown mode safety ────────────────────────────


class TestReadCollateralUnknown:
    def test_unknown_treats_as_unified(self):
        """If HL adds a new mode we don't recognize, default to summing
        spot+perp (matches unified). Over-counts vs Standard but safer
        than under-counting and rejecting valid trades."""
        info = _make_info(
            "someFutureMode",
            perp_account_value="100",
            spot_usdc="300",
        )
        c = read_collateral(info, "0xabc")
        assert c.mode == "unknown"
        assert c.total == Decimal("400")
