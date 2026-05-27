"""Unit tests for rift_trade.gates.build_portfolio_snapshot — mode-aware
gate snapshot construction. No network."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from rift_trade.gates import PortfolioSnapshot, build_portfolio_snapshot


def _info(abstraction: str, perp_value: str, margin_used: str, spot_usdc: str,
          n_positions: int = 0) -> MagicMock:
    info = MagicMock()
    info.query_user_abstraction_state.return_value = abstraction
    info.user_state.return_value = {
        "marginSummary": {"accountValue": perp_value, "totalMarginUsed": margin_used},
        "assetPositions": [{"position": {"coin": f"X{i}", "szi": "0.1"}} for i in range(n_positions)],
    }
    info.spot_user_state.return_value = {
        "balances": [{"coin": "USDC", "token": 0, "total": spot_usdc, "hold": "0"}],
    }
    return info


def test_standard_snapshot_ignores_spot():
    info = _info("disabled", "500", "100", "999", n_positions=2)
    snap = build_portfolio_snapshot(info, "0xabc", realized_pnl_today=Decimal("12.5"))
    assert isinstance(snap, PortfolioSnapshot)
    assert snap.margin_used == Decimal("100")
    assert snap.margin_available == Decimal("400")  # spot ignored
    assert snap.open_positions == 2
    assert snap.realized_pnl_today == Decimal("12.5")


def test_unified_snapshot_includes_spot():
    info = _info("unifiedAccount", "0", "0", "697.74", n_positions=0)
    snap = build_portfolio_snapshot(info, "0xabc")
    # The whole point of the helper: gate sees real available margin
    assert snap.margin_available == Decimal("697.74")
    assert snap.margin_used == Decimal("0")
    assert snap.open_positions == 0


def test_portfolio_margin_snapshot_sums_spot():
    """Per HL docs, perp state is 'not meaningful' under PM. Real
    collateral is in spot. Same treatment as unified."""
    info = _info("portfolioMargin", "200", "50", "1000")
    snap = build_portfolio_snapshot(info, "0xabc")
    # 200 - 50 + 1000 = 1150 (perp slice + spot USDC)
    assert snap.margin_available == Decimal("1150")


def test_realized_pnl_defaults_to_zero():
    info = _info("disabled", "100", "0", "0")
    snap = build_portfolio_snapshot(info, "0xabc")
    assert snap.realized_pnl_today == Decimal("0")
