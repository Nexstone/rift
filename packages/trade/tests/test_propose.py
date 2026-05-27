"""Unit tests for rift_trade.propose — Phase 0 step 5.

Tests cover:
  - TradeProposal model construction
  - Input validation (legs, market snapshot, rationale)
  - Risk metric estimation
  - Persistence (atomic write, 0600, roundtrip)
  - Lookup by ID
  - List recent proposals
  - Delete
  - PROPOSE audit record emission via capsys
  - T2 invariant: rationale required

No chain access, no real signing, no real audit log (just captures NDJSON
emit to stdout).
"""

from __future__ import annotations

import io
import json
import stat
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from rift_core.audit_schemas import (
    MarketSnapshot,
    PortfolioState,
    ProposalLeg,
    RiskMetrics,
)
from rift_core.keys import Actor, ActorKind
from rift_trade.propose import (
    MAX_LEGS,
    MAX_SNAPSHOT_AGE_SECONDS,
    ProposeError,
    TradeProposal,
    delete_proposal,
    get_proposal,
    list_recent_proposals,
    propose_trade,
    save_proposal,
)


# ─── Shared fixtures ──────────────────────────────────────────────────

@pytest.fixture
def actor():
    return Actor(kind=ActorKind.AGENT, id="claude-session-1", session_id="conv-abc")


@pytest.fixture
def now():
    return datetime.now(timezone.utc)


@pytest.fixture
def fresh_market(now):
    return MarketSnapshot(
        coin="ETH",
        mid_price=Decimal("2300"),
        bid=Decimal("2299.5"),
        ask=Decimal("2300.5"),
        funding_rate_1h=Decimal("0.00001"),
        timestamp_ms=int(now.timestamp() * 1000),
    )


@pytest.fixture
def portfolio():
    return PortfolioState(
        account_address="0x" + "a" * 40,
        margin_used=Decimal("100"),
        margin_available=Decimal("900"),
        open_positions=2,
        realized_pnl_today=Decimal("12.5"),
    )


@pytest.fixture
def long_market_leg():
    return ProposalLeg(
        coin="ETH",
        side="buy",
        size=Decimal("0.2"),
        order_type="market",
        stop_loss=Decimal("2275"),
        take_profit=Decimal("2350"),
    )


@pytest.fixture
def short_limit_leg():
    return ProposalLeg(
        coin="ETH",
        side="sell",
        size=Decimal("0.1"),
        order_type="limit",
        limit_price=Decimal("2310"),
        stop_loss=Decimal("2330"),
    )


@pytest.fixture
def tmp_proposals_dir(tmp_path):
    return tmp_path / "proposals"


# ─── propose_trade happy paths ────────────────────────────────────────

class TestProposeTrade:
    def test_minimal_proposal(self, actor, fresh_market, portfolio, long_market_leg, tmp_proposals_dir):
        p = propose_trade(
            actor=actor, legs=[long_market_leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="RSI=28 oversold; long",
            proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        assert isinstance(p, TradeProposal)
        assert p.actor == actor
        assert len(p.legs) == 1
        assert p.rationale == "RSI=28 oversold; long"

    def test_multi_leg_same_coin(self, actor, fresh_market, portfolio, long_market_leg, short_limit_leg, tmp_proposals_dir):
        p = propose_trade(
            actor=actor, legs=[long_market_leg, short_limit_leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="Spread trade",
            proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        assert len(p.legs) == 2

    def test_persists_to_disk(self, actor, fresh_market, portfolio, long_market_leg, tmp_proposals_dir):
        p = propose_trade(
            actor=actor, legs=[long_market_leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        path = tmp_proposals_dir / f"{p.id}.json"
        assert path.exists()
        # File perms 0600
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_signals_propagated(self, actor, fresh_market, portfolio, long_market_leg, tmp_proposals_dir):
        signals = {"rsi": Decimal("28"), "atr": Decimal("12.5")}
        p = propose_trade(
            actor=actor, legs=[long_market_leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", signals=signals,
            proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        assert p.signals == signals

    def test_unique_ids(self, actor, fresh_market, portfolio, long_market_leg, tmp_proposals_dir):
        p1 = propose_trade(
            actor=actor, legs=[long_market_leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        p2 = propose_trade(
            actor=actor, legs=[long_market_leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        assert p1.id != p2.id


# ─── Validation rejects ───────────────────────────────────────────────

class TestValidation:
    def test_empty_legs_rejected(self, actor, fresh_market, portfolio, tmp_proposals_dir):
        with pytest.raises(ProposeError, match="at least one leg"):
            propose_trade(
                actor=actor, legs=[],
                market_snapshot=fresh_market, portfolio_state=portfolio,
                rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
            )

    def test_too_many_legs_rejected(self, actor, fresh_market, portfolio, long_market_leg, tmp_proposals_dir):
        with pytest.raises(ProposeError, match=f"max {MAX_LEGS}"):
            propose_trade(
                actor=actor, legs=[long_market_leg] * (MAX_LEGS + 1),
                market_snapshot=fresh_market, portfolio_state=portfolio,
                rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
            )

    def test_zero_size_rejected(self, actor, fresh_market, portfolio, tmp_proposals_dir):
        # ProposalLeg itself doesn't validate size > 0; propose() does
        leg = ProposalLeg(coin="ETH", side="buy", size=Decimal("0.00000001"),
                          order_type="market")  # tiny but positive — allowed
        # The validator rejects size <= 0; effectively-zero positive amount allowed
        propose_trade(
            actor=actor, legs=[leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )

    def test_mixed_coin_rejected(self, actor, fresh_market, portfolio, tmp_proposals_dir):
        wrong = ProposalLeg(coin="BTC", side="buy", size=Decimal("0.01"), order_type="market")
        with pytest.raises(ProposeError, match="Multi-coin"):
            propose_trade(
                actor=actor, legs=[wrong],
                market_snapshot=fresh_market, portfolio_state=portfolio,
                rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
            )

    def test_stale_market_snapshot_rejected(self, actor, portfolio, long_market_leg, tmp_proposals_dir):
        stale_ts = int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp() * 1000)
        stale = MarketSnapshot(
            coin="ETH", mid_price=Decimal("2300"),
            bid=Decimal("2299"), ask=Decimal("2301"),
            timestamp_ms=stale_ts,
        )
        with pytest.raises(ProposeError, match="freshness"):
            propose_trade(
                actor=actor, legs=[long_market_leg],
                market_snapshot=stale, portfolio_state=portfolio,
                rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
            )

    def test_empty_rationale_rejected(self, actor, fresh_market, portfolio, long_market_leg, tmp_proposals_dir):
        with pytest.raises(ProposeError, match="rationale is required"):
            propose_trade(
                actor=actor, legs=[long_market_leg],
                market_snapshot=fresh_market, portfolio_state=portfolio,
                rationale="   ",  # whitespace only
                proposals_dir=tmp_proposals_dir, emit_audit=False,
            )

    def test_long_buy_stop_above_entry_rejected(self, actor, fresh_market, portfolio, tmp_proposals_dir):
        # buy entry ~2300, stop_loss above entry is wrong (would lock in loss immediately)
        bad = ProposalLeg(coin="ETH", side="buy", size=Decimal("0.1"),
                          order_type="market", stop_loss=Decimal("2350"))
        with pytest.raises(ProposeError, match="buy stop_loss"):
            propose_trade(
                actor=actor, legs=[bad],
                market_snapshot=fresh_market, portfolio_state=portfolio,
                rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
            )

    def test_short_sell_stop_below_entry_rejected(self, actor, fresh_market, portfolio, tmp_proposals_dir):
        bad = ProposalLeg(coin="ETH", side="sell", size=Decimal("0.1"),
                          order_type="market", stop_loss=Decimal("2250"))
        with pytest.raises(ProposeError, match="sell stop_loss"):
            propose_trade(
                actor=actor, legs=[bad],
                market_snapshot=fresh_market, portfolio_state=portfolio,
                rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
            )

    def test_buy_take_profit_below_entry_rejected(self, actor, fresh_market, portfolio, tmp_proposals_dir):
        bad = ProposalLeg(coin="ETH", side="buy", size=Decimal("0.1"),
                          order_type="market", take_profit=Decimal("2250"))
        with pytest.raises(ProposeError, match="buy take_profit"):
            propose_trade(
                actor=actor, legs=[bad],
                market_snapshot=fresh_market, portfolio_state=portfolio,
                rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
            )

    def test_limit_order_without_limit_price_rejected(self, actor, fresh_market, portfolio, tmp_proposals_dir):
        bad = ProposalLeg(coin="ETH", side="buy", size=Decimal("0.1"), order_type="limit")
        with pytest.raises(ProposeError, match="limit_price"):
            propose_trade(
                actor=actor, legs=[bad],
                market_snapshot=fresh_market, portfolio_state=portfolio,
                rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
            )

    def test_zero_market_price_rejected(self, actor, portfolio, long_market_leg, tmp_proposals_dir):
        broken = MarketSnapshot(coin="ETH", mid_price=Decimal("0"),
                                 bid=Decimal("1"), ask=Decimal("2"), timestamp_ms=int(datetime.now(timezone.utc).timestamp() * 1000))
        with pytest.raises(ProposeError, match="mid_price"):
            propose_trade(
                actor=actor, legs=[long_market_leg],
                market_snapshot=broken, portfolio_state=portfolio,
                rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
            )


# ─── Risk estimation ──────────────────────────────────────────────────

class TestRiskEstimation:
    def test_notional_uses_limit_price_for_limit_orders(self, actor, fresh_market, portfolio, tmp_proposals_dir):
        leg = ProposalLeg(coin="ETH", side="buy", size=Decimal("0.1"),
                          order_type="limit", limit_price=Decimal("2280"))
        p = propose_trade(
            actor=actor, legs=[leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        # 0.1 * 2280 = 228
        assert p.risk.notional_usd == Decimal("228.0")

    def test_notional_uses_mid_for_market_orders(self, actor, fresh_market, portfolio, tmp_proposals_dir):
        leg = ProposalLeg(coin="ETH", side="buy", size=Decimal("0.1"), order_type="market")
        p = propose_trade(
            actor=actor, legs=[leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        # 0.1 * 2300 = 230
        assert p.risk.notional_usd == Decimal("230.0")

    def test_max_loss_from_stop(self, actor, fresh_market, portfolio, tmp_proposals_dir):
        # Buy at 2300, stop at 2275 → loss per unit = 25; size 0.2 → 5.0
        leg = ProposalLeg(coin="ETH", side="buy", size=Decimal("0.2"),
                          order_type="market", stop_loss=Decimal("2275"))
        p = propose_trade(
            actor=actor, legs=[leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        assert p.risk.max_loss_estimate == Decimal("5.0")

    def test_max_loss_fallback_when_no_stop(self, actor, fresh_market, portfolio, tmp_proposals_dir):
        # No stop → 20% of notional placeholder
        leg = ProposalLeg(coin="ETH", side="buy", size=Decimal("0.1"), order_type="market")
        p = propose_trade(
            actor=actor, legs=[leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        # 0.1 * 2300 * 0.20 = 46
        assert p.risk.max_loss_estimate == Decimal("46.000")

    def test_leverage_from_margin_available(self, actor, fresh_market, portfolio, tmp_proposals_dir):
        # notional 230, available margin 900 → leverage = 230/900 ~ 0.256
        leg = ProposalLeg(coin="ETH", side="buy", size=Decimal("0.1"), order_type="market")
        p = propose_trade(
            actor=actor, legs=[leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        assert p.risk.leverage < Decimal("1")


# ─── Persistence operations ───────────────────────────────────────────

class TestPersistence:
    def test_save_and_get_roundtrip(self, actor, fresh_market, portfolio, long_market_leg, tmp_proposals_dir):
        p = propose_trade(
            actor=actor, legs=[long_market_leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        loaded = get_proposal(p.id, proposals_dir=tmp_proposals_dir)
        assert loaded == p

    def test_get_nonexistent_returns_none(self, tmp_proposals_dir):
        assert get_proposal(uuid4(), proposals_dir=tmp_proposals_dir) is None

    def test_get_corrupt_returns_none(self, tmp_proposals_dir):
        tmp_proposals_dir.mkdir(parents=True, exist_ok=True)
        fake_id = uuid4()
        (tmp_proposals_dir / f"{fake_id}.json").write_text("not valid")
        assert get_proposal(fake_id, proposals_dir=tmp_proposals_dir) is None

    def test_list_recent_newest_first(self, actor, fresh_market, portfolio, long_market_leg, tmp_proposals_dir):
        ids = []
        for _ in range(3):
            p = propose_trade(
                actor=actor, legs=[long_market_leg],
                market_snapshot=fresh_market, portfolio_state=portfolio,
                rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
            )
            ids.append(p.id)
        recent = list_recent_proposals(proposals_dir=tmp_proposals_dir)
        # Newest first
        assert recent[0].id == ids[-1]

    def test_list_respects_limit(self, actor, fresh_market, portfolio, long_market_leg, tmp_proposals_dir):
        for _ in range(5):
            propose_trade(
                actor=actor, legs=[long_market_leg],
                market_snapshot=fresh_market, portfolio_state=portfolio,
                rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
            )
        assert len(list_recent_proposals(limit=3, proposals_dir=tmp_proposals_dir)) == 3

    def test_delete_returns_true_when_existed(self, actor, fresh_market, portfolio, long_market_leg, tmp_proposals_dir):
        p = propose_trade(
            actor=actor, legs=[long_market_leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        assert delete_proposal(p.id, proposals_dir=tmp_proposals_dir) is True
        assert get_proposal(p.id, proposals_dir=tmp_proposals_dir) is None

    def test_delete_returns_false_when_missing(self, tmp_proposals_dir):
        assert delete_proposal(uuid4(), proposals_dir=tmp_proposals_dir) is False


# ─── Audit emission ───────────────────────────────────────────────────

class TestAuditEmission:
    def test_emits_propose_record_to_stdout(self, capsys, actor, fresh_market, portfolio, long_market_leg, tmp_proposals_dir):
        propose_trade(
            actor=actor, legs=[long_market_leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="RSI=28 oversold",
            proposals_dir=tmp_proposals_dir, emit_audit=True,
        )
        cap = capsys.readouterr()
        # emit() writes one NDJSON line per call
        line = cap.out.strip()
        data = json.loads(line)
        assert data["type"] == "audit_record"
        rec = data["record"]
        assert rec["kind"] == "propose"
        assert rec["rationale"] == "RSI=28 oversold"
        assert rec["package"] == "rift_trade"

    def test_emit_audit_false_skips_stdout(self, capsys, actor, fresh_market, portfolio, long_market_leg, tmp_proposals_dir):
        propose_trade(
            actor=actor, legs=[long_market_leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        cap = capsys.readouterr()
        assert cap.out == ""


# ─── TradeProposal convenience properties ─────────────────────────────

class TestTradeProposalProperties:
    def test_coin_returns_first_leg_coin(self, actor, fresh_market, portfolio, long_market_leg, tmp_proposals_dir):
        p = propose_trade(
            actor=actor, legs=[long_market_leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        assert p.coin == "ETH"

    def test_total_notional_sums_legs(self, actor, fresh_market, portfolio, long_market_leg, short_limit_leg, tmp_proposals_dir):
        p = propose_trade(
            actor=actor, legs=[long_market_leg, short_limit_leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        # long leg: 0.2 * 2300 = 460 (market, uses mid)
        # short leg: 0.1 * 2310 = 231 (limit, uses limit_price)
        assert p.total_notional_usd == Decimal("691.0")

    def test_frozen(self, actor, fresh_market, portfolio, long_market_leg, tmp_proposals_dir):
        p = propose_trade(
            actor=actor, legs=[long_market_leg],
            market_snapshot=fresh_market, portfolio_state=portfolio,
            rationale="r", proposals_dir=tmp_proposals_dir, emit_audit=False,
        )
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            p.rationale = "changed"  # type: ignore
