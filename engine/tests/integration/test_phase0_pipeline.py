"""Phase 0 composition integration tests — full pipeline end-to-end with mocked HL.

Unit tests verify each module in isolation. These tests verify the modules
COMPOSE correctly: an action that starts at pair_wallet flows through
issue_token → propose_trade → execute_proposal → audit emission, with every
boundary's contracts respected.

Mocked HL via `MockExchangeClient` — no real chain. Real cryptography
(eth_account signing/recovery), real disk I/O (tmp_path), real audit
emission (capsys).

If any of these tests fails, there's a glue bug between the modules even
though each module passes its own unit tests.

Run: `pytest engine/tests/integration/test_phase0_pipeline.py -v`
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
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
from rift_trade.api_wallet import (
    LocalKeySigner,
    RegistrationError,
    generate_api_wallet,
    register_api_wallet,
    save_api_wallet,
)
from rift_trade.auth import (
    IssuanceMode,
    LocalTokenSigner,
    issue_token,
)
from rift_trade.execute import (
    ExecuteConfig,
    ExecutionStatus,
    LegStatus,
    execute_proposal,
)
from rift_trade.gates import DailyActivity, PortfolioSnapshot as GatesPortfolioSnapshot
from rift_trade.propose import propose_trade


# ─── Test infrastructure ──────────────────────────────────────────────

# Deterministic main-wallet test key (NOT a real key — eth_account test pattern)
MAIN_WALLET_KEY = "0x" + "5" * 64


class MockHLExchange:
    """Stands in for both api_wallet's HL POST and execute's order submission.

    Records every call. Returns canned filled responses by default; can be
    configured to reject specific actions or raise exceptions.
    """

    def __init__(self):
        self.approve_agent_calls = []
        self.order_calls = []
        self.next_approve_response = {"status": "ok",
                                       "response": {"type": "default",
                                                    "data": {"hash": "0x" + "a" * 64}}}
        self.next_order_responses = []  # list, consumed in order

    def _filled_response(self, fill_price="2300", fill_size="0.1", fee="0.07"):
        return {"status": "ok", "response": {"type": "order",
                "data": {"statuses": [{"filled": {
                    "avgPx": fill_price, "totalSz": fill_size, "fee": fee
                }}]}}}

    # Patches the HL POST in api_wallet
    def _post_l1_action(self, base_url, action, signature, nonce, timeout=30.0):
        self.approve_agent_calls.append({"action": action, "signature": signature})
        return self.next_approve_response

    # ExchangeClient protocol method for execute.py
    def submit_order(self, *, api_wallet, main_wallet_address, coin, side,
                      size, order_type, limit_price=None, reduce_only=False):
        self.order_calls.append({
            "coin": coin, "side": side, "size": size, "order_type": order_type,
            "limit_price": limit_price, "reduce_only": reduce_only,
            "api_wallet_address": api_wallet.address,
            "main_wallet_address": main_wallet_address,
        })
        if self.next_order_responses:
            return self.next_order_responses.pop(0)
        return self._filled_response()


@pytest.fixture
def isolated_dirs(tmp_path):
    """Isolated storage roots so each test has a clean ~/.rift/."""
    return {
        "credentials": tmp_path / "credentials",
        "tokens": tmp_path / "tokens",
        "proposals": tmp_path / "proposals",
        "kill_flag": tmp_path / "KILL",
    }


@pytest.fixture
def hl(monkeypatch):
    """MockHLExchange patched into api_wallet's HTTP layer."""
    exchange = MockHLExchange()
    monkeypatch.setattr(api_wallet_module, "_post_l1_action", exchange._post_l1_action)
    return exchange


@pytest.fixture
def actor():
    return Actor(kind=ActorKind.AGENT, id="integration-test-agent", session_id="conv-1")


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
def portfolio_state():
    return PortfolioState(
        account_address="0x" + "a" * 40,
        margin_used=Decimal("100"),
        margin_available=Decimal("9000"),
        open_positions=1,
        realized_pnl_today=Decimal("0"),
    )


@pytest.fixture
def portfolio_snapshot():
    return GatesPortfolioSnapshot(
        margin_used=Decimal("100"),
        margin_available=Decimal("9000"),
        open_positions=1,
        realized_pnl_today=Decimal("0"),
    )


def _execute_config(isolated_dirs) -> ExecuteConfig:
    """Default ExecuteConfig pointing kill flag at the per-test tmp dir."""
    return ExecuteConfig(kill_flag_path=isolated_dirs["kill_flag"])


# ─── Pipeline helpers ─────────────────────────────────────────────────

def _pair_agent(isolated_dirs, network=Network.MAINNET):
    """Run the agent-pair flow: generate + register + save."""
    api_wallet = generate_api_wallet(network=network, name="RIFT-TEST")
    signer = LocalKeySigner(MAIN_WALLET_KEY)
    registered = register_api_wallet(api_wallet, signer)
    save_api_wallet(registered, path=isolated_dirs["credentials"])
    return registered, signer


def _issue_token(isolated_dirs, signer, *, coins=None, max_notional=Decimal("500"),
                  max_daily=Decimal("2000"), mode=IssuanceMode.SESSION, expires_at=None):
    """Run the token-issue flow."""
    if coins is None:
        coins = ["ETH"]
    scope = TokenScope(
        coins=coins,
        max_notional=max_notional,
        max_daily=max_daily,
    )
    return issue_token(
        scope=scope, signer=LocalTokenSigner(MAIN_WALLET_KEY),
        issuance_mode=mode, expires_at=expires_at,
        tokens_dir=isolated_dirs["tokens"], save=True,
    )


def _build_proposal(isolated_dirs, actor, fresh_market, portfolio_state,
                     *, legs=None, rationale="integration test"):
    """Run the propose flow."""
    if legs is None:
        legs = [ProposalLeg(coin="ETH", side="buy", size=Decimal("0.1"),
                             order_type="market", stop_loss=Decimal("2275"))]
    return propose_trade(
        actor=actor, legs=legs,
        market_snapshot=fresh_market, portfolio_state=portfolio_state,
        rationale=rationale,
        proposals_dir=isolated_dirs["proposals"], emit_audit=False,
    )


# ─── PIPELINE TESTS ───────────────────────────────────────────────────

class TestHappyPath:
    """The most important integration test: full end-to-end pipeline succeeds."""

    def test_pair_issue_propose_execute_all_succeed(
        self, hl, isolated_dirs, actor, fresh_market, portfolio_state, portfolio_snapshot,
    ):
        # 1. Pair API wallet — registers on (mocked) HL
        api_wallet, main_signer = _pair_agent(isolated_dirs)
        assert api_wallet.registered_tx == "0x" + "a" * 64
        assert len(hl.approve_agent_calls) == 1
        assert hl.approve_agent_calls[0]["action"]["type"] == "approveAgent"

        # 2. Issue session token
        token = _issue_token(isolated_dirs, main_signer)
        assert token.is_valid()

        # 3. Build proposal
        proposal = _build_proposal(isolated_dirs, actor, fresh_market, portfolio_state)
        assert len(proposal.legs) == 1

        # 4. Execute
        activity = DailyActivity(token_id=str(token.id),
                                  volume_today_usd=Decimal("0"), actions_today=0)
        result = execute_proposal(
            proposal_id=proposal.id, token_id=token.id, actor=actor,
            market_snapshot=fresh_market, portfolio_snapshot=portfolio_snapshot,
            activity=activity, exchange=hl, rationale="integration",
            config=_execute_config(isolated_dirs),
            proposals_dir=isolated_dirs["proposals"],
            tokens_dir=isolated_dirs["tokens"],
            api_wallet_path=isolated_dirs["credentials"],
        )

        # 5. Verify outcome
        assert result.status == ExecutionStatus.FILLED
        assert len(result.legs) == 1
        assert result.legs[0].status == LegStatus.FILLED
        assert result.legs[0].fill_price == Decimal("2300")

        # 6. Exchange was called with the right wallet
        assert len(hl.order_calls) == 1
        assert hl.order_calls[0]["api_wallet_address"] == api_wallet.address


class TestSecurityScenarios:
    """The four classes of attack from the Phase 0 doc — verified at the
    pipeline level (each module already rejects these in unit tests, but
    we verify the composition also rejects)."""

    def test_forged_token_rejected_in_full_pipeline(
        self, hl, isolated_dirs, actor, fresh_market, portfolio_state, portfolio_snapshot,
    ):
        api_wallet, main_signer = _pair_agent(isolated_dirs)
        token = _issue_token(isolated_dirs, main_signer)
        # Attacker tampers with the on-disk token to escalate scope
        evil_scope = TokenScope(coins=["BTC", "ETH"],
                                  max_notional=Decimal("999999"),
                                  max_daily=Decimal("99999999"))
        tampered = token.model_copy(update={"scope": evil_scope})
        auth_module.save_token(tampered, tokens_dir=isolated_dirs["tokens"])
        # Build a legit proposal
        proposal = _build_proposal(isolated_dirs, actor, fresh_market, portfolio_state)
        activity = DailyActivity(token_id=str(token.id),
                                  volume_today_usd=Decimal("0"), actions_today=0)
        result = execute_proposal(
            proposal_id=proposal.id, token_id=token.id, actor=actor,
            market_snapshot=fresh_market, portfolio_snapshot=portfolio_snapshot,
            activity=activity, exchange=hl, rationale="test",
            config=_execute_config(isolated_dirs),
            proposals_dir=isolated_dirs["proposals"],
            tokens_dir=isolated_dirs["tokens"],
            api_wallet_path=isolated_dirs["credentials"],
        )
        assert result.status == ExecutionStatus.REJECTED
        assert "signature" in result.rejection_reason.lower()
        # Critical: exchange NEVER called
        assert len(hl.order_calls) == 0

    def test_revoked_token_rejected_in_full_pipeline(
        self, hl, isolated_dirs, actor, fresh_market, portfolio_state, portfolio_snapshot,
    ):
        api_wallet, main_signer = _pair_agent(isolated_dirs)
        token = _issue_token(isolated_dirs, main_signer)
        # Operator revokes the token before execute
        auth_module.revoke_token(token.id, tokens_dir=isolated_dirs["tokens"])
        proposal = _build_proposal(isolated_dirs, actor, fresh_market, portfolio_state)
        activity = DailyActivity(token_id=str(token.id),
                                  volume_today_usd=Decimal("0"), actions_today=0)
        result = execute_proposal(
            proposal_id=proposal.id, token_id=token.id, actor=actor,
            market_snapshot=fresh_market, portfolio_snapshot=portfolio_snapshot,
            activity=activity, exchange=hl, rationale="test",
            config=_execute_config(isolated_dirs),
            proposals_dir=isolated_dirs["proposals"],
            tokens_dir=isolated_dirs["tokens"],
            api_wallet_path=isolated_dirs["credentials"],
        )
        assert result.status == ExecutionStatus.REJECTED
        assert "revoked" in result.rejection_reason
        assert len(hl.order_calls) == 0

    def test_scope_violation_rejected_in_full_pipeline(
        self, hl, isolated_dirs, actor, fresh_market, portfolio_state, portfolio_snapshot,
    ):
        api_wallet, main_signer = _pair_agent(isolated_dirs)
        # Token only allows BTC
        btc_only_token = _issue_token(isolated_dirs, main_signer, coins=["BTC"])
        # Proposal is for ETH
        proposal = _build_proposal(isolated_dirs, actor, fresh_market, portfolio_state)
        activity = DailyActivity(token_id=str(btc_only_token.id),
                                  volume_today_usd=Decimal("0"), actions_today=0)
        result = execute_proposal(
            proposal_id=proposal.id, token_id=btc_only_token.id, actor=actor,
            market_snapshot=fresh_market, portfolio_snapshot=portfolio_snapshot,
            activity=activity, exchange=hl, rationale="test",
            config=_execute_config(isolated_dirs),
            proposals_dir=isolated_dirs["proposals"],
            tokens_dir=isolated_dirs["tokens"],
            api_wallet_path=isolated_dirs["credentials"],
        )
        assert result.status == ExecutionStatus.REJECTED
        assert result.legs[0].status == LegStatus.REJECTED
        assert len(hl.order_calls) == 0

    def test_kill_switch_blocks_full_pipeline(
        self, hl, isolated_dirs, actor, fresh_market, portfolio_state, portfolio_snapshot,
    ):
        api_wallet, main_signer = _pair_agent(isolated_dirs)
        token = _issue_token(isolated_dirs, main_signer)
        proposal = _build_proposal(isolated_dirs, actor, fresh_market, portfolio_state)
        # Operator hits the kill switch between propose and execute
        isolated_dirs["kill_flag"].touch()
        activity = DailyActivity(token_id=str(token.id),
                                  volume_today_usd=Decimal("0"), actions_today=0)
        result = execute_proposal(
            proposal_id=proposal.id, token_id=token.id, actor=actor,
            market_snapshot=fresh_market, portfolio_snapshot=portfolio_snapshot,
            activity=activity, exchange=hl, rationale="test",
            config=_execute_config(isolated_dirs),
            proposals_dir=isolated_dirs["proposals"],
            tokens_dir=isolated_dirs["tokens"],
            api_wallet_path=isolated_dirs["credentials"],
        )
        assert result.status == ExecutionStatus.REJECTED
        assert "kill" in result.legs[0].rejection_reason.lower()
        assert len(hl.order_calls) == 0


class TestAuditTrail:
    """The audit trail is what makes T3 trustworthy. Verify a successful
    execution emits EXECUTE record with everything needed to reproduce."""

    def test_execute_audit_record_captures_full_context(
        self, capsys, hl, isolated_dirs, actor, fresh_market, portfolio_state, portfolio_snapshot,
    ):
        api_wallet, main_signer = _pair_agent(isolated_dirs)
        token = _issue_token(isolated_dirs, main_signer)
        proposal = _build_proposal(isolated_dirs, actor, fresh_market, portfolio_state,
                                    rationale="RSI=28 long ETH")
        activity = DailyActivity(token_id=str(token.id),
                                  volume_today_usd=Decimal("0"), actions_today=0)
        execute_proposal(
            proposal_id=proposal.id, token_id=token.id, actor=actor,
            market_snapshot=fresh_market, portfolio_snapshot=portfolio_snapshot,
            activity=activity, exchange=hl, rationale="filling proposal",
            config=_execute_config(isolated_dirs),
            proposals_dir=isolated_dirs["proposals"],
            tokens_dir=isolated_dirs["tokens"],
            api_wallet_path=isolated_dirs["credentials"],
        )

        out = capsys.readouterr().out
        records = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        execute_records = [r for r in records if r.get("record", {}).get("kind") == "execute"]
        assert len(execute_records) >= 1
        rec = execute_records[0]["record"]

        # Verify forensic-replay essentials
        assert rec["rationale"] == "filling proposal"
        assert rec["actor"]["id"] == actor.id
        assert rec["inputs"]["proposal_id"] == str(proposal.id)
        assert rec["inputs"]["auth_token_id"] == str(token.id)
        assert rec["inputs"]["api_wallet_address"] == api_wallet.address
        assert rec["outputs"]["status"] == "filled"
        assert rec["outputs"]["fill_price"] == "2300"


class TestMultiLeg:
    def test_three_leg_proposal_all_succeed(
        self, hl, isolated_dirs, actor, fresh_market, portfolio_state, portfolio_snapshot,
    ):
        api_wallet, main_signer = _pair_agent(isolated_dirs)
        # Token must allow 3 legs
        token = _issue_token(isolated_dirs, main_signer,
                              max_notional=Decimal("500"), max_daily=Decimal("5000"))
        legs = [
            ProposalLeg(coin="ETH", side="buy", size=Decimal("0.1"), order_type="market"),
            ProposalLeg(coin="ETH", side="sell", size=Decimal("0.05"),
                        order_type="market", reduce_only=True),
            ProposalLeg(coin="ETH", side="buy", size=Decimal("0.05"), order_type="market"),
        ]
        proposal = _build_proposal(isolated_dirs, actor, fresh_market, portfolio_state, legs=legs)
        activity = DailyActivity(token_id=str(token.id),
                                  volume_today_usd=Decimal("0"), actions_today=0)
        # Queue 3 filled responses
        hl.next_order_responses = [hl._filled_response() for _ in range(3)]
        result = execute_proposal(
            proposal_id=proposal.id, token_id=token.id, actor=actor,
            market_snapshot=fresh_market, portfolio_snapshot=portfolio_snapshot,
            activity=activity, exchange=hl, rationale="3-leg",
            config=_execute_config(isolated_dirs),
            proposals_dir=isolated_dirs["proposals"],
            tokens_dir=isolated_dirs["tokens"],
            api_wallet_path=isolated_dirs["credentials"],
        )
        assert result.status == ExecutionStatus.FILLED
        assert len(result.legs) == 3
        assert all(r.status == LegStatus.FILLED for r in result.legs)
        assert len(hl.order_calls) == 3


class TestRotation:
    def test_rotate_agent_invalidates_old_wallet(
        self, hl, isolated_dirs, actor,
    ):
        # Pair, then rotate (revoke + register new)
        old_wallet, signer = _pair_agent(isolated_dirs)
        from rift_trade.api_wallet import revoke_api_wallet
        revoke_api_wallet(old_wallet, signer)
        new_wallet = generate_api_wallet(network=Network.MAINNET, name="RIFT-TEST")
        registered = register_api_wallet(new_wallet, signer)
        save_api_wallet(registered, path=isolated_dirs["credentials"])
        # Verify two approve_agent calls + the addresses changed
        assert len(hl.approve_agent_calls) == 3  # original + revoke + new
        assert old_wallet.address != registered.address
        # Loading from disk gets the new wallet
        loaded = api_wallet_module.load_api_wallet(path=isolated_dirs["credentials"])
        assert loaded.address == registered.address


class TestPersistenceRoundtrip:
    """Verify that state written by one phase is correctly readable by the next."""

    def test_token_signature_survives_disk_roundtrip(
        self, hl, isolated_dirs,
    ):
        api_wallet, main_signer = _pair_agent(isolated_dirs)
        token = _issue_token(isolated_dirs, main_signer)
        # Reload from disk
        loaded = auth_module.load_token(token.id, tokens_dir=isolated_dirs["tokens"])
        assert loaded == token
        # Signature still verifies after roundtrip
        assert auth_module.verify_token_signature(loaded)

    def test_proposal_state_survives_disk_roundtrip(
        self, isolated_dirs, actor, fresh_market, portfolio_state,
    ):
        original = _build_proposal(isolated_dirs, actor, fresh_market, portfolio_state)
        from rift_trade.propose import get_proposal
        loaded = get_proposal(original.id, proposals_dir=isolated_dirs["proposals"])
        assert loaded == original


class TestRationale:
    def test_propose_rationale_required(self, isolated_dirs, actor, fresh_market, portfolio_state):
        from rift_trade.propose import ProposeError
        with pytest.raises(ProposeError):
            _build_proposal(isolated_dirs, actor, fresh_market, portfolio_state, rationale="   ")

    def test_execute_rationale_required(
        self, hl, isolated_dirs, actor, fresh_market, portfolio_state, portfolio_snapshot,
    ):
        api_wallet, main_signer = _pair_agent(isolated_dirs)
        token = _issue_token(isolated_dirs, main_signer)
        proposal = _build_proposal(isolated_dirs, actor, fresh_market, portfolio_state)
        activity = DailyActivity(token_id=str(token.id),
                                  volume_today_usd=Decimal("0"), actions_today=0)
        result = execute_proposal(
            proposal_id=proposal.id, token_id=token.id, actor=actor,
            market_snapshot=fresh_market, portfolio_snapshot=portfolio_snapshot,
            activity=activity, exchange=hl, rationale="",
            config=_execute_config(isolated_dirs),
            proposals_dir=isolated_dirs["proposals"],
            tokens_dir=isolated_dirs["tokens"],
            api_wallet_path=isolated_dirs["credentials"],
        )
        assert result.status == ExecutionStatus.REJECTED
        assert "rationale" in result.rejection_reason.lower()
