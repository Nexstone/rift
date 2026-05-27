"""Unit tests for rift_trade.gates — Phase 0 execution-time safety gates.

Every gate is tested for both the passing and failing path, plus the
edge cases that would cause silent failures (zero prices, mismatched
tokens, programming errors).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from rift_core.keys import (
    AuthorizationToken,
    TokenScope,
    TradeAction,
    TradeSide,
)
from rift_trade.gates import (
    CircuitBreakerConfig,
    DailyActivity,
    GateResult,
    MarketSnapshot,
    PortfolioSnapshot,
    ProposalLegSummary,
    check_circuit_breakers,
    check_daily_cap,
    check_kill_switch,
    check_margin,
    check_slippage,
    check_token_scope,
    first_failure,
    run_all_gates,
)


# ─── Shared fixtures ──────────────────────────────────────────────────

VALID_SIG = "0x" + "1" * 130


@pytest.fixture
def token():
    return AuthorizationToken(
        issuer="0x" + "a" * 40,
        scope=TokenScope(
            coins=["ETH", "SUI"],
            sides=[TradeSide.BUY, TradeSide.SELL],
            actions=[TradeAction.OPEN, TradeAction.CLOSE],
            max_notional=Decimal("500"),
            max_daily=Decimal("2000"),
        ),
        signature=VALID_SIG,
    )


@pytest.fixture
def leg():
    return ProposalLegSummary(
        coin="ETH",
        side=TradeSide.BUY,
        size=Decimal("0.2"),
        notional_usd=Decimal("460"),
        action=TradeAction.OPEN,
    )


@pytest.fixture
def portfolio():
    return PortfolioSnapshot(
        margin_used=Decimal("100"),
        margin_available=Decimal("900"),
        open_positions=2,
        realized_pnl_today=Decimal("0"),
    )


@pytest.fixture
def market():
    return MarketSnapshot(
        coin="ETH",
        mid_price=Decimal("2300"),
        bid=Decimal("2299.5"),
        ask=Decimal("2300.5"),
    )


@pytest.fixture
def activity(token):
    return DailyActivity(
        token_id=str(token.id),
        volume_today_usd=Decimal("500"),
        actions_today=3,
    )


# ─── Kill switch ──────────────────────────────────────────────────────

class TestKillSwitch:
    def test_passes_when_no_flag_file(self, tmp_path):
        result = check_kill_switch(flag_path=tmp_path / "NOPE")
        assert result.passed
        assert result.gate_name == "kill_switch"

    def test_fails_when_flag_file_exists(self, tmp_path):
        flag = tmp_path / "KILL"
        flag.write_text("triggered by ops at 12:00")
        result = check_kill_switch(flag_path=flag)
        assert not result.passed
        assert result.gate_name == "kill_switch"
        assert "kill switch" in result.reason.lower()
        assert "12:00" in result.detail["flag_content"]

    def test_empty_flag_file_still_blocks(self, tmp_path):
        flag = tmp_path / "KILL"
        flag.touch()
        result = check_kill_switch(flag_path=flag)
        assert not result.passed


# ─── Token scope ──────────────────────────────────────────────────────

class TestTokenScope:
    def test_in_scope_passes(self, leg, token):
        assert check_token_scope(leg, token).passed

    def test_revoked_token_rejected(self, leg, token):
        revoked = token.model_copy(update={"revoked": True})
        r = check_token_scope(leg, revoked)
        assert not r.passed
        assert "revoked" in r.reason.lower() or "expired" in r.reason.lower()

    def test_disallowed_coin_rejected(self, token):
        bad_leg = ProposalLegSummary(
            coin="BTC", side=TradeSide.BUY, size=Decimal("0.01"),
            notional_usd=Decimal("100"), action=TradeAction.OPEN,
        )
        r = check_token_scope(bad_leg, token)
        assert not r.passed
        assert "BTC" in r.reason

    def test_disallowed_side_rejected(self):
        t = AuthorizationToken(
            issuer="0x" + "a" * 40,
            scope=TokenScope(
                coins=["ETH"], sides=[TradeSide.BUY],  # buy only
                max_notional=Decimal("500"), max_daily=Decimal("2000"),
            ),
            signature=VALID_SIG,
        )
        sell_leg = ProposalLegSummary(
            coin="ETH", side=TradeSide.SELL, size=Decimal("0.1"),
            notional_usd=Decimal("200"), action=TradeAction.OPEN,
        )
        r = check_token_scope(sell_leg, t)
        assert not r.passed
        assert "sell" in r.reason.lower()

    def test_disallowed_action_rejected(self):
        t = AuthorizationToken(
            issuer="0x" + "a" * 40,
            scope=TokenScope(
                coins=["ETH"], actions=[TradeAction.CLOSE],  # closes only
                max_notional=Decimal("500"), max_daily=Decimal("2000"),
            ),
            signature=VALID_SIG,
        )
        open_leg = ProposalLegSummary(
            coin="ETH", side=TradeSide.BUY, size=Decimal("0.1"),
            notional_usd=Decimal("200"), action=TradeAction.OPEN,
        )
        r = check_token_scope(open_leg, t)
        assert not r.passed
        assert "open" in r.reason.lower()

    def test_notional_over_cap_rejected(self, token):
        big = ProposalLegSummary(
            coin="ETH", side=TradeSide.BUY, size=Decimal("1"),
            notional_usd=Decimal("600"),  # token cap is 500
            action=TradeAction.OPEN,
        )
        r = check_token_scope(big, token)
        assert not r.passed
        assert "600" in r.reason

    def test_any_coin_scope_allows_anything(self):
        any_token = AuthorizationToken(
            issuer="0x" + "a" * 40,
            scope=TokenScope(coins="any", max_notional=Decimal("1000"), max_daily=Decimal("5000")),
            signature=VALID_SIG,
        )
        leg = ProposalLegSummary(
            coin="DOGE", side=TradeSide.BUY, size=Decimal("100"),
            notional_usd=Decimal("50"), action=TradeAction.OPEN,
        )
        assert check_token_scope(leg, any_token).passed


# ─── Daily cap ────────────────────────────────────────────────────────

class TestDailyCap:
    def test_under_cap_passes(self, leg, token, activity):
        # 500 already + 460 leg = 960, under 2000 cap
        assert check_daily_cap(leg, token, activity).passed

    def test_at_cap_passes(self, token):
        # exactly at cap: 1540 + 460 = 2000
        activity = DailyActivity(token_id=str(token.id),
                                  volume_today_usd=Decimal("1540"), actions_today=5)
        leg = ProposalLegSummary(coin="ETH", side=TradeSide.BUY, size=Decimal("0.2"),
                                  notional_usd=Decimal("460"), action=TradeAction.OPEN)
        assert check_daily_cap(leg, token, activity).passed

    def test_over_cap_rejected(self, leg, token):
        activity = DailyActivity(token_id=str(token.id),
                                  volume_today_usd=Decimal("1800"), actions_today=10)
        r = check_daily_cap(leg, token, activity)
        assert not r.passed
        # 1800 + 460 = 2260 > 2000
        assert "2260" in r.reason

    def test_mismatched_token_id_rejected_as_programming_error(self, leg, token):
        wrong_activity = DailyActivity(
            token_id="00000000-0000-0000-0000-000000000000",
            volume_today_usd=Decimal("0"), actions_today=0,
        )
        r = check_daily_cap(leg, token, wrong_activity)
        assert not r.passed
        assert "programming error" in r.reason.lower()


# ─── Circuit breakers ─────────────────────────────────────────────────

class TestCircuitBreakers:
    def test_no_config_always_passes(self, leg, portfolio):
        assert check_circuit_breakers(
            leg, portfolio, CircuitBreakerConfig(), Decimal("0")
        ).passed

    def test_volume_cap_blocks(self, leg, portfolio):
        config = CircuitBreakerConfig(max_daily_volume_usd=Decimal("1000"))
        r = check_circuit_breakers(leg, portfolio, config, Decimal("700"))
        assert not r.passed  # 700 + 460 = 1160 > 1000
        assert "1000" in r.reason

    def test_open_position_cap_blocks_opens(self, leg, portfolio):
        config = CircuitBreakerConfig(max_open_positions=2)
        # portfolio.open_positions == 2 (at cap)
        r = check_circuit_breakers(leg, portfolio, config, Decimal("0"))
        assert not r.passed
        assert "open" in r.reason.lower()

    def test_open_position_cap_allows_closes_at_cap(self, portfolio):
        config = CircuitBreakerConfig(max_open_positions=2)
        close_leg = ProposalLegSummary(
            coin="ETH", side=TradeSide.SELL, size=Decimal("0.1"),
            notional_usd=Decimal("230"), action=TradeAction.CLOSE,
        )
        assert check_circuit_breakers(close_leg, portfolio, config, Decimal("0")).passed

    def test_drawdown_trigger_blocks(self, leg):
        # Started at 10000, lost 600 today, cap is 5%
        portfolio = PortfolioSnapshot(
            margin_used=Decimal("100"), margin_available=Decimal("9400"),
            open_positions=2, realized_pnl_today=Decimal("-600"),
        )
        config = CircuitBreakerConfig(
            max_drawdown_pct_today=Decimal("0.05"),
            starting_equity_today=Decimal("10000"),
        )
        r = check_circuit_breakers(leg, portfolio, config, Decimal("0"))
        assert not r.passed  # 6% drawdown > 5% cap
        assert "drawdown" in r.reason.lower()

    def test_drawdown_under_threshold_passes(self, leg):
        portfolio = PortfolioSnapshot(
            margin_used=Decimal("100"), margin_available=Decimal("9700"),
            open_positions=2, realized_pnl_today=Decimal("-300"),  # 3%
        )
        config = CircuitBreakerConfig(
            max_drawdown_pct_today=Decimal("0.05"),
            starting_equity_today=Decimal("10000"),
        )
        assert check_circuit_breakers(leg, portfolio, config, Decimal("0")).passed


# ─── Margin ───────────────────────────────────────────────────────────

class TestMargin:
    def test_sufficient_margin_passes(self, leg, portfolio):
        assert check_margin(leg, portfolio, Decimal("100")).passed

    def test_insufficient_margin_rejected(self, leg, portfolio):
        r = check_margin(leg, portfolio, Decimal("1000"))  # > 900 available
        assert not r.passed
        assert "1000" in r.reason
        assert "900" in r.reason


# ─── Slippage ─────────────────────────────────────────────────────────

class TestSlippage:
    def test_tight_spread_passes(self, leg, market):
        # market: bid=2299.5 ask=2300.5 → half-spread = 0.25 / 2300 ~ 1 bp; cap 50 bps
        assert check_slippage(leg, market, max_slippage_bps=50).passed

    def test_wide_spread_rejected(self, leg):
        wide = MarketSnapshot(
            coin="ETH", mid_price=Decimal("2300"),
            bid=Decimal("2280"), ask=Decimal("2320"),  # 87 bps half-spread
        )
        r = check_slippage(leg, wide, max_slippage_bps=50)
        assert not r.passed
        assert "slippage" in r.reason.lower()

    def test_coin_mismatch_rejected_as_programming_error(self, leg):
        wrong_market = MarketSnapshot(
            coin="BTC", mid_price=Decimal("70000"),
            bid=Decimal("69990"), ask=Decimal("70010"),
        )
        r = check_slippage(leg, wrong_market, max_slippage_bps=50)
        assert not r.passed
        assert "ETH" in r.reason and "BTC" in r.reason

    def test_zero_prices_rejected(self, leg):
        zero_market = MarketSnapshot(
            coin="ETH", mid_price=Decimal("0"),
            bid=Decimal("0"), ask=Decimal("0"),
        )
        r = check_slippage(leg, zero_market, max_slippage_bps=50)
        assert not r.passed
        assert "invalid" in r.reason.lower() or "zero" in r.reason.lower()


# ─── run_all_gates orchestration ──────────────────────────────────────

class TestRunAllGates:
    def test_happy_path_all_pass(self, tmp_path, leg, token, portfolio, market, activity):
        results = run_all_gates(
            leg=leg, token=token, portfolio=portfolio, market=market,
            activity=activity, circuit_config=CircuitBreakerConfig(),
            total_volume_today_usd=Decimal("0"), margin_required_usd=Decimal("100"),
            kill_flag_path=tmp_path / "NOPE",
        )
        assert all(r.passed for r in results)
        assert first_failure(results) is None

    def test_kill_switch_short_circuits_in_results(self, tmp_path, leg, token, portfolio, market, activity):
        kill = tmp_path / "KILL"
        kill.touch()
        results = run_all_gates(
            leg=leg, token=token, portfolio=portfolio, market=market,
            activity=activity, circuit_config=CircuitBreakerConfig(),
            total_volume_today_usd=Decimal("0"), margin_required_usd=Decimal("100"),
            kill_flag_path=kill,
        )
        # Kill switch is FIRST, so first_failure is kill_switch
        ff = first_failure(results)
        assert ff is not None
        assert ff.gate_name == "kill_switch"
        # All gates still run (for audit) but the headline failure is kill_switch
        assert len(results) == 6

    def test_multiple_failures_reported(self, tmp_path, portfolio, market, activity):
        bad_token = AuthorizationToken(
            issuer="0x" + "a" * 40,
            scope=TokenScope(coins=["BTC"], max_notional=Decimal("50"), max_daily=Decimal("100")),
            signature=VALID_SIG,
        )
        bad_leg = ProposalLegSummary(
            coin="ETH", side=TradeSide.BUY, size=Decimal("1"),
            notional_usd=Decimal("2000"), action=TradeAction.OPEN,
        )
        bad_activity = DailyActivity(
            token_id=str(bad_token.id),
            volume_today_usd=Decimal("90"), actions_today=20,
        )
        results = run_all_gates(
            leg=bad_leg, token=bad_token, portfolio=portfolio, market=market,
            activity=bad_activity, circuit_config=CircuitBreakerConfig(),
            total_volume_today_usd=Decimal("0"), margin_required_usd=Decimal("100"),
            kill_flag_path=tmp_path / "NOPE",
        )
        failed = [r for r in results if not r.passed]
        # Scope fails (wrong coin), daily_cap fails (over limit). Multiple failures captured.
        assert len(failed) >= 2


# ─── GateResult helpers ───────────────────────────────────────────────

class TestGateResult:
    def test_ok_factory(self):
        r = GateResult.ok("test")
        assert r.passed and r.gate_name == "test" and r.reason is None

    def test_reject_factory(self):
        r = GateResult.reject("test", "nope", extra=1)
        assert not r.passed and r.reason == "nope" and r.detail == {"extra": 1}

    def test_frozen(self):
        r = GateResult.ok("test")
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            r.passed = False  # type: ignore
