"""Unit tests for rift_trade.execute — Phase 0 step 7, the T3 trust-critical path.

Coverage targets (per Phase 0 doc):
  - 100% on critical paths
  - Token scope mismatch → reject
  - Gate failure → reject + GATE_REJECT audit emitted
  - Audit-write failure → reject (fail-closed invariant)
  - Successful path → EXECUTE audit emitted with all fields

No real chain calls. MockExchangeClient stands in for HL.
No real WC. LocalTokenSigner stands in for the WC bridge.
No real audit log writer (yet) — verifying audit emission via capsys.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from rift_core.audit_schemas import (
    MarketSnapshot,
    PortfolioState,
    ProposalLeg,
)
from rift_core.keys import (
    APIWalletKey,
    Actor,
    ActorKind,
    Network,
    TokenScope,
    TradeAction,
    TradeSide,
)
from rift_trade import api_wallet as api_wallet_module
from rift_trade import auth as auth_module
from rift_trade.auth import LocalTokenSigner, IssuanceMode
from rift_trade.execute import (
    ExchangeClient,
    ExecuteConfig,
    ExecutionResult,
    ExecutionStatus,
    LegResult,
    LegStatus,
    _parse_exchange_response,
    execute_proposal,
)
from rift_trade.gates import (
    CircuitBreakerConfig,
    DailyActivity,
    DEFAULT_KILL_FLAG_PATH,
    PortfolioSnapshot as GatesPortfolioSnapshot,
)
from rift_trade.propose import propose_trade


# ─── Mock ExchangeClient ──────────────────────────────────────────────

class MockExchangeClient:
    """Records every submit_order call. Returns canned responses per leg."""

    def __init__(self, responses: list[dict] | None = None,
                 raise_on_submit: Exception | None = None):
        self._responses = responses or [self._default_filled_response()]
        self._index = 0
        self._raise = raise_on_submit
        self.calls: list[dict] = []

    @staticmethod
    def _default_filled_response() -> dict:
        return {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [
                    {"filled": {"avgPx": "2300", "totalSz": "0.1", "fee": "0.07"}}
                ]},
            },
        }

    def submit_order(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        if self._index >= len(self._responses):
            return self._default_filled_response()
        resp = self._responses[self._index]
        self._index += 1
        return resp


# ─── Shared fixtures ──────────────────────────────────────────────────

LOCAL_KEY = "0x" + "5" * 64


@pytest.fixture
def signer():
    return LocalTokenSigner(LOCAL_KEY)


@pytest.fixture
def actor():
    return Actor(kind=ActorKind.AGENT, id="claude-1", session_id="s1")


@pytest.fixture
def tmp_dirs(tmp_path):
    """Per-test isolated dirs for proposals, tokens, api wallet."""
    return {
        "proposals": tmp_path / "proposals",
        "tokens": tmp_path / "tokens",
        "api_wallet": tmp_path / "credentials",
        "kill_flag": tmp_path / "KILL",
    }


@pytest.fixture
def api_wallet_on_disk(tmp_dirs):
    """A locally-generated API wallet, persisted to a tmp path."""
    w = api_wallet_module.generate_api_wallet(Network.MAINNET, name="test-agent")
    api_wallet_module.save_api_wallet(w, tmp_dirs["api_wallet"])
    return w


@pytest.fixture
def scope():
    return TokenScope(
        coins=["ETH"],
        sides=[TradeSide.BUY, TradeSide.SELL],
        actions=[TradeAction.OPEN, TradeAction.CLOSE],
        max_notional=Decimal("500"),
        max_daily=Decimal("2000"),
    )


@pytest.fixture
def token_on_disk(signer, scope, tmp_dirs):
    return auth_module.issue_token(
        scope=scope, signer=signer, issuance_mode=IssuanceMode.SESSION,
        tokens_dir=tmp_dirs["tokens"], save=True,
    )


@pytest.fixture
def fresh_market():
    return MarketSnapshot(
        coin="ETH",
        mid_price=Decimal("2300"),
        bid=Decimal("2299.5"),
        ask=Decimal("2300.5"),
        timestamp_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
    )


@pytest.fixture
def portfolio():
    return GatesPortfolioSnapshot(
        margin_used=Decimal("100"),
        margin_available=Decimal("9000"),
        open_positions=2,
        realized_pnl_today=Decimal("0"),
    )


@pytest.fixture
def activity(token_on_disk):
    return DailyActivity(
        token_id=str(token_on_disk.id),
        volume_today_usd=Decimal("0"),
        actions_today=0,
    )


@pytest.fixture
def proposal_on_disk(actor, fresh_market, tmp_dirs):
    portfolio = PortfolioState(
        account_address="0x" + "a" * 40,
        margin_used=Decimal("100"),
        margin_available=Decimal("9000"),
        open_positions=2,
        realized_pnl_today=Decimal("0"),
    )
    leg = ProposalLeg(
        coin="ETH", side="buy", size=Decimal("0.1"),
        order_type="market", stop_loss=Decimal("2275"),
    )
    return propose_trade(
        actor=actor, legs=[leg],
        market_snapshot=fresh_market, portfolio_state=portfolio,
        rationale="test execute",
        proposals_dir=tmp_dirs["proposals"], emit_audit=False,
    )


@pytest.fixture
def cfg(tmp_dirs):
    """Default ExecuteConfig with kill flag pointing at the tmp (nonexistent) path."""
    return ExecuteConfig(kill_flag_path=tmp_dirs["kill_flag"])


def _call(*, proposal_id, token_id, actor, fresh_market, portfolio, activity,
          exchange, rationale, cfg, tmp_dirs):
    """Common invocation helper to reduce test boilerplate."""
    return execute_proposal(
        proposal_id=proposal_id, token_id=token_id, actor=actor,
        market_snapshot=fresh_market, portfolio_snapshot=portfolio,
        activity=activity, exchange=exchange,
        rationale=rationale, config=cfg,
        proposals_dir=tmp_dirs["proposals"], tokens_dir=tmp_dirs["tokens"],
        api_wallet_path=tmp_dirs["api_wallet"],
    )


# ─── Happy path ───────────────────────────────────────────────────────

class TestHappyPath:
    def test_filled_order_returns_filled_status(
        self, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, proposal_on_disk, token_on_disk, cfg, tmp_dirs,
    ):
        exchange = MockExchangeClient()
        result = _call(
            proposal_id=proposal_on_disk.id, token_id=token_on_disk.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=exchange, rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.status == ExecutionStatus.FILLED
        assert len(result.legs) == 1
        assert result.legs[0].status == LegStatus.FILLED
        assert result.legs[0].fill_price == Decimal("2300")
        assert result.legs[0].fill_size == Decimal("0.1")
        assert len(exchange.calls) == 1

    def test_exchange_called_with_correct_args(
        self, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, proposal_on_disk, token_on_disk, cfg, tmp_dirs,
    ):
        exchange = MockExchangeClient()
        _call(
            proposal_id=proposal_on_disk.id, token_id=token_on_disk.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=exchange, rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        call = exchange.calls[0]
        assert call["coin"] == "ETH"
        assert call["side"] == "buy"
        assert call["size"] == Decimal("0.1")
        assert call["order_type"] == "market"
        assert call["api_wallet"].address == api_wallet_on_disk.address


# ─── Rejection paths ──────────────────────────────────────────────────

class TestRejections:
    def test_missing_proposal_rejected(
        self, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, token_on_disk, cfg, tmp_dirs,
    ):
        result = _call(
            proposal_id=uuid4(),  # never proposed
            token_id=token_on_disk.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=MockExchangeClient(), rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.status == ExecutionStatus.REJECTED
        assert "not found" in result.rejection_reason

    def test_missing_token_rejected(
        self, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, proposal_on_disk, cfg, tmp_dirs,
    ):
        bogus_token_id = uuid4()
        activity = DailyActivity(token_id=str(bogus_token_id),
                                  volume_today_usd=Decimal("0"), actions_today=0)
        result = _call(
            proposal_id=proposal_on_disk.id, token_id=bogus_token_id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=MockExchangeClient(), rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.status == ExecutionStatus.REJECTED
        assert "auth token" in result.rejection_reason

    def test_forged_token_signature_rejected(
        self, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, proposal_on_disk, token_on_disk, cfg, tmp_dirs,
    ):
        # Tamper with the on-disk token's scope
        bad = token_on_disk.model_copy(update={
            "scope": TokenScope(coins=["BTC"], max_notional=Decimal("999999"), max_daily=Decimal("999999"))
        })
        auth_module.save_token(bad, tokens_dir=tmp_dirs["tokens"])
        result = _call(
            proposal_id=proposal_on_disk.id, token_id=bad.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=MockExchangeClient(), rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.status == ExecutionStatus.REJECTED
        assert "signature" in result.rejection_reason.lower()

    def test_revoked_token_rejected(
        self, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, proposal_on_disk, token_on_disk, cfg, tmp_dirs,
    ):
        auth_module.revoke_token(token_on_disk.id, tokens_dir=tmp_dirs["tokens"])
        result = _call(
            proposal_id=proposal_on_disk.id, token_id=token_on_disk.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=MockExchangeClient(), rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.status == ExecutionStatus.REJECTED
        assert "revoked" in result.rejection_reason

    def test_expired_token_rejected(
        self, signer, actor, fresh_market, portfolio,
        api_wallet_on_disk, proposal_on_disk, cfg, tmp_dirs,
    ):
        from rift_core.keys import TokenScope
        # Issue a token that already expired
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        scope = TokenScope(coins=["ETH"], max_notional=Decimal("500"),
                            max_daily=Decimal("2000"))
        expired = auth_module.issue_token(
            scope=scope, signer=signer, expires_at=past,
            tokens_dir=tmp_dirs["tokens"], save=True,
        )
        activity = DailyActivity(token_id=str(expired.id), volume_today_usd=Decimal("0"), actions_today=0)
        result = _call(
            proposal_id=proposal_on_disk.id, token_id=expired.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=MockExchangeClient(), rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.status == ExecutionStatus.REJECTED
        assert "expired" in result.rejection_reason

    def test_empty_rationale_rejected(
        self, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, proposal_on_disk, token_on_disk, cfg, tmp_dirs,
    ):
        result = _call(
            proposal_id=proposal_on_disk.id, token_id=token_on_disk.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=MockExchangeClient(),
            rationale="   ",  # whitespace only
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.status == ExecutionStatus.REJECTED
        assert "rationale" in result.rejection_reason.lower()

    def test_missing_api_wallet_rejected(
        self, actor, fresh_market, portfolio, activity,
        proposal_on_disk, token_on_disk, cfg, tmp_dirs,
    ):
        # api_wallet_on_disk fixture NOT used here — wallet file missing
        result = _call(
            proposal_id=proposal_on_disk.id, token_id=token_on_disk.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=MockExchangeClient(), rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.status == ExecutionStatus.REJECTED
        assert "API wallet" in result.rejection_reason


# ─── Gate-failure paths ───────────────────────────────────────────────

class TestGateFailures:
    def test_kill_switch_blocks_execution(
        self, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, proposal_on_disk, token_on_disk, tmp_dirs,
    ):
        # Create the kill flag file
        tmp_dirs["kill_flag"].touch()
        cfg = ExecuteConfig(kill_flag_path=tmp_dirs["kill_flag"])
        exchange = MockExchangeClient()
        result = _call(
            proposal_id=proposal_on_disk.id, token_id=token_on_disk.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=exchange, rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.status == ExecutionStatus.REJECTED
        # First leg rejected with kill_switch reason
        assert result.legs[0].status == LegStatus.REJECTED
        assert "kill switch" in result.legs[0].rejection_reason.lower()
        # Exchange never called
        assert len(exchange.calls) == 0

    def test_scope_mismatch_blocks(
        self, signer, actor, fresh_market, portfolio,
        api_wallet_on_disk, proposal_on_disk, cfg, tmp_dirs,
    ):
        # Token that only allows BTC; proposal is for ETH
        btc_scope = TokenScope(coins=["BTC"], max_notional=Decimal("500"),
                                max_daily=Decimal("2000"))
        btc_token = auth_module.issue_token(
            scope=btc_scope, signer=signer,
            tokens_dir=tmp_dirs["tokens"], save=True,
        )
        activity = DailyActivity(token_id=str(btc_token.id), volume_today_usd=Decimal("0"), actions_today=0)
        exchange = MockExchangeClient()
        result = _call(
            proposal_id=proposal_on_disk.id, token_id=btc_token.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=exchange, rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.status == ExecutionStatus.REJECTED
        assert "ETH" in result.legs[0].rejection_reason
        assert len(exchange.calls) == 0

    def test_gate_reject_emits_audit_record(
        self, capsys, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, proposal_on_disk, token_on_disk, tmp_dirs,
    ):
        tmp_dirs["kill_flag"].touch()
        cfg = ExecuteConfig(kill_flag_path=tmp_dirs["kill_flag"])
        _call(
            proposal_id=proposal_on_disk.id, token_id=token_on_disk.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=MockExchangeClient(), rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        out = capsys.readouterr().out
        # At least one GATE_REJECT record emitted
        records = [json.loads(line) for line in out.strip().split("\n") if line]
        gate_rejects = [r for r in records if r.get("record", {}).get("kind") == "gate_reject"]
        assert len(gate_rejects) == 1
        assert gate_rejects[0]["record"]["inputs"]["gate_name"] == "kill_switch"


# ─── Audit emission ───────────────────────────────────────────────────

class TestAuditEmission:
    def test_filled_leg_emits_execute_record(
        self, capsys, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, proposal_on_disk, token_on_disk, cfg, tmp_dirs,
    ):
        _call(
            proposal_id=proposal_on_disk.id, token_id=token_on_disk.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=MockExchangeClient(), rationale="rsi reversion",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        out = capsys.readouterr().out
        records = [json.loads(line) for line in out.strip().split("\n") if line]
        execute_records = [r for r in records if r.get("record", {}).get("kind") == "execute"]
        assert len(execute_records) == 1
        r = execute_records[0]["record"]
        assert r["rationale"] == "rsi reversion"
        assert r["outputs"]["status"] == "filled"
        assert r["outputs"]["fill_price"] == "2300"

    def test_audit_record_includes_actor(
        self, capsys, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, proposal_on_disk, token_on_disk, cfg, tmp_dirs,
    ):
        _call(
            proposal_id=proposal_on_disk.id, token_id=token_on_disk.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=MockExchangeClient(), rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        out = capsys.readouterr().out
        records = [json.loads(line) for line in out.strip().split("\n") if line]
        execute_records = [r for r in records if r.get("record", {}).get("kind") == "execute"]
        actor_record = execute_records[0]["record"]["actor"]
        assert actor_record["kind"] == "agent"
        assert actor_record["id"] == "claude-1"


# ─── Audit-write fail-closed invariant ────────────────────────────────

class TestAuditFailClosed:
    def test_audit_write_failure_halts_multi_leg_execution(
        self, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, signer, scope, cfg, tmp_dirs, monkeypatch,
    ):
        """If emit() fails after leg 1 fills, leg 2 must NOT be submitted.
        Phase 0 fail-closed invariant."""
        # Build a 2-leg proposal
        from rift_core.audit_schemas import PortfolioState
        portfolio_state = PortfolioState(
            account_address="0x" + "a" * 40,
            margin_used=Decimal("100"), margin_available=Decimal("9000"),
            open_positions=2, realized_pnl_today=Decimal("0"),
        )
        legs = [
            ProposalLeg(coin="ETH", side="buy", size=Decimal("0.1"), order_type="market"),
            ProposalLeg(coin="ETH", side="sell", size=Decimal("0.05"), order_type="market"),
        ]
        proposal = propose_trade(
            actor=actor, legs=legs,
            market_snapshot=fresh_market, portfolio_state=portfolio_state,
            rationale="2-leg test", proposals_dir=tmp_dirs["proposals"], emit_audit=False,
        )
        token = auth_module.issue_token(
            scope=scope, signer=signer, tokens_dir=tmp_dirs["tokens"], save=True,
        )
        activity = DailyActivity(token_id=str(token.id), volume_today_usd=Decimal("0"), actions_today=0)

        # Make emit() raise on the EXECUTE record (after leg 1 fills)
        call_count = [0]
        from rift_trade import execute as execute_mod

        original_emit = execute_mod.emit
        def failing_emit(data):
            # Allow the first leg's audit but fail on it
            call_count[0] += 1
            if call_count[0] == 1:  # fail on the very first emit
                raise IOError("disk full")
            return original_emit(data)
        monkeypatch.setattr(execute_mod, "emit", failing_emit)

        exchange = MockExchangeClient(responses=[
            MockExchangeClient._default_filled_response(),  # leg 1 filled
            MockExchangeClient._default_filled_response(),  # leg 2 would fill, but...
        ])

        result = _call(
            proposal_id=proposal.id, token_id=token.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=exchange, rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )

        # Leg 1 filled, leg 2 NOT submitted (fail-closed kicked in)
        assert result.status == ExecutionStatus.PARTIAL
        assert "audit-write failed" in result.rejection_reason.lower()
        assert len(exchange.calls) == 1  # only leg 1 submitted


# ─── Multi-leg semantics ──────────────────────────────────────────────

class TestMultiLeg:
    def test_two_legs_both_filled(
        self, actor, fresh_market, portfolio, signer, scope, cfg, tmp_dirs,
        api_wallet_on_disk,
    ):
        from rift_core.audit_schemas import PortfolioState
        portfolio_state = PortfolioState(
            account_address="0x" + "a" * 40,
            margin_used=Decimal("100"), margin_available=Decimal("9000"),
            open_positions=2, realized_pnl_today=Decimal("0"),
        )
        legs = [
            ProposalLeg(coin="ETH", side="buy", size=Decimal("0.1"), order_type="market"),
            ProposalLeg(coin="ETH", side="sell", size=Decimal("0.05"),
                        order_type="market", reduce_only=True),
        ]
        proposal = propose_trade(
            actor=actor, legs=legs,
            market_snapshot=fresh_market, portfolio_state=portfolio_state,
            rationale="2-leg", proposals_dir=tmp_dirs["proposals"], emit_audit=False,
        )
        token = auth_module.issue_token(
            scope=scope, signer=signer, tokens_dir=tmp_dirs["tokens"], save=True,
        )
        activity = DailyActivity(token_id=str(token.id), volume_today_usd=Decimal("0"), actions_today=0)
        exchange = MockExchangeClient(responses=[
            MockExchangeClient._default_filled_response(),
            MockExchangeClient._default_filled_response(),
        ])
        result = _call(
            proposal_id=proposal.id, token_id=token.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=exchange, rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.status == ExecutionStatus.FILLED
        assert len(result.legs) == 2
        assert all(r.status == LegStatus.FILLED for r in result.legs)

    def test_second_leg_rejected_marks_partial_and_stops(
        self, actor, fresh_market, portfolio, signer, scope, cfg, tmp_dirs,
        api_wallet_on_disk,
    ):
        from rift_core.audit_schemas import PortfolioState
        portfolio_state = PortfolioState(
            account_address="0x" + "a" * 40,
            margin_used=Decimal("100"), margin_available=Decimal("9000"),
            open_positions=2, realized_pnl_today=Decimal("0"),
        )
        legs = [
            ProposalLeg(coin="ETH", side="buy", size=Decimal("0.1"), order_type="market"),
            ProposalLeg(coin="ETH", side="sell", size=Decimal("0.05"),
                        order_type="market", reduce_only=True),
            ProposalLeg(coin="ETH", side="buy", size=Decimal("0.05"), order_type="market"),
        ]
        proposal = propose_trade(
            actor=actor, legs=legs,
            market_snapshot=fresh_market, portfolio_state=portfolio_state,
            rationale="3-leg", proposals_dir=tmp_dirs["proposals"], emit_audit=False,
        )
        token = auth_module.issue_token(
            scope=scope, signer=signer, tokens_dir=tmp_dirs["tokens"], save=True,
        )
        activity = DailyActivity(token_id=str(token.id), volume_today_usd=Decimal("0"), actions_today=0)
        # Leg 1 fills, leg 2 errors
        bad_response = {"status": "ok", "response": {"data": {"statuses": [{"error": "insufficient margin"}]}}}
        exchange = MockExchangeClient(responses=[
            MockExchangeClient._default_filled_response(),
            bad_response,
        ])
        result = _call(
            proposal_id=proposal.id, token_id=token.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=exchange, rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.status == ExecutionStatus.PARTIAL
        assert result.legs[0].status == LegStatus.FILLED
        assert result.legs[1].status == LegStatus.REJECTED
        assert result.legs[2].status == LegStatus.NOT_ATTEMPTED
        # Only 2 calls (third never attempted)
        assert len(exchange.calls) == 2


# ─── Chain submission failures ────────────────────────────────────────

class TestChainSubmissionErrors:
    def test_exchange_exception_marks_leg_rejected(
        self, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, proposal_on_disk, token_on_disk, cfg, tmp_dirs,
    ):
        exchange = MockExchangeClient(raise_on_submit=ConnectionError("timeout"))
        result = _call(
            proposal_id=proposal_on_disk.id, token_id=token_on_disk.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=exchange, rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.status == ExecutionStatus.REJECTED
        assert result.legs[0].status == LegStatus.REJECTED
        assert "timeout" in result.legs[0].rejection_reason


# ─── _parse_exchange_response ─────────────────────────────────────────

class TestParseExchangeResponse:
    def test_filled_response(self):
        resp = {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"avgPx": "2300", "totalSz": "0.5", "fee": "0.34"}}
        ]}}}
        r = _parse_exchange_response(resp, leg_index=0)
        assert r.status == LegStatus.FILLED
        assert r.fill_price == Decimal("2300")
        assert r.fill_size == Decimal("0.5")
        assert r.fee_paid == Decimal("0.34")

    def test_error_response(self):
        resp = {"status": "ok", "response": {"data": {"statuses": [{"error": "size too small"}]}}}
        r = _parse_exchange_response(resp, leg_index=0)
        assert r.status == LegStatus.REJECTED
        assert "size too small" in r.rejection_reason

    def test_resting_response(self):
        resp = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 12345}}]}}}
        r = _parse_exchange_response(resp, leg_index=0)
        assert r.status == LegStatus.SUBMITTED

    def test_non_ok_status(self):
        resp = {"status": "err", "response": "bad signature"}
        r = _parse_exchange_response(resp, leg_index=0)
        assert r.status == LegStatus.REJECTED

    def test_non_dict_response(self):
        r = _parse_exchange_response("not a dict", leg_index=0)  # type: ignore
        assert r.status == LegStatus.REJECTED


# ─── Result invariants ────────────────────────────────────────────────

class TestExecutionResult:
    def test_timestamps_populated(
        self, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, proposal_on_disk, token_on_disk, cfg, tmp_dirs,
    ):
        result = _call(
            proposal_id=proposal_on_disk.id, token_id=token_on_disk.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=MockExchangeClient(), rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.started_at_ms > 0
        assert result.completed_at_ms >= result.started_at_ms

    def test_rejected_result_includes_reason(
        self, actor, fresh_market, portfolio, activity,
        api_wallet_on_disk, token_on_disk, cfg, tmp_dirs,
    ):
        result = _call(
            proposal_id=uuid4(),  # nonexistent
            token_id=token_on_disk.id,
            actor=actor, fresh_market=fresh_market, portfolio=portfolio,
            activity=activity, exchange=MockExchangeClient(), rationale="go",
            cfg=cfg, tmp_dirs=tmp_dirs,
        )
        assert result.status == ExecutionStatus.REJECTED
        assert result.rejection_reason is not None
        assert len(result.rejection_reason) > 0
