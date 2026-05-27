"""Unit tests for rift_core.audit_schemas — DecisionRecord + 8 per-kind schemas.

Goal: every DecisionKind round-trips through JSON without data loss, and
the typed_inputs()/typed_outputs() accessors return the right schemas.

The trust-critical paths (T2 rationale required, T3 rationale required)
are explicitly tested.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from rift_core.audit_schemas import (
    KIND_SCHEMAS,
    AuthorInputs,
    AuthorOutputs,
    AuthorizeInputs,
    AuthorizeOutputs,
    DecisionKind,
    DecisionRecord,
    ExecuteInputs,
    ExecuteOutputs,
    GateRejectInputs,
    GateRejectOutputs,
    KillToggleInputs,
    KillToggleOutputs,
    MarketSnapshot,
    ObserveInputs,
    ObserveOutputs,
    PortfolioState,
    ProposalLeg,
    ProposeInputs,
    ProposeOutputs,
    RiskMetrics,
    SimulateInputs,
    SimulateOutputs,
    build_record,
)
from rift_core.keys import Actor, ActorKind


# ─── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def actor():
    return Actor(kind=ActorKind.AGENT, id="claude-session-1", session_id="conv-abc")


@pytest.fixture
def market_snapshot():
    return MarketSnapshot(
        coin="ETH",
        mid_price=Decimal("2300.50"),
        bid=Decimal("2300.40"),
        ask=Decimal("2300.60"),
        funding_rate_1h=Decimal("0.00001"),
        timestamp_ms=1715890234567,
    )


@pytest.fixture
def portfolio_state():
    return PortfolioState(
        account_address="0xabc1230000000000000000000000000000000000",
        margin_used=Decimal("100"),
        margin_available=Decimal("900"),
        open_positions=2,
        realized_pnl_today=Decimal("12.50"),
    )


@pytest.fixture
def risk_metrics():
    return RiskMetrics(
        notional_usd=Decimal("500"),
        leverage=Decimal("3"),
        max_loss_estimate=Decimal("15"),
    )


# ─── Each DecisionKind constructs cleanly ─────────────────────────────

class TestEachKindBuilds:
    def test_observe(self, actor):
        rec = build_record(
            actor=actor,
            kind=DecisionKind.OBSERVE,
            inputs=ObserveInputs(resource="candles", filters={"coin": "ETH", "tf": "1h"}),
            outputs=ObserveOutputs(rows_returned=5000),
            package="rift_data",
            version="0.1.0",
        )
        assert rec.kind == DecisionKind.OBSERVE
        assert rec.typed_inputs().resource == "candles"
        assert rec.typed_outputs().rows_returned == 5000

    def test_simulate(self, actor):
        rec = build_record(
            actor=actor,
            kind=DecisionKind.SIMULATE,
            inputs=SimulateInputs(kind="backtest", strategy_ref="trend_follow", coin="BTC", interval="4h"),
            outputs=SimulateOutputs(return_pct=Decimal("25.0"), sharpe=Decimal("0.71"), num_trades=33),
            package="rift_engine",
            version="0.1.0",
        )
        assert rec.typed_outputs().num_trades == 33

    def test_author(self, actor):
        rec = build_record(
            actor=actor,
            kind=DecisionKind.AUTHOR,
            inputs=AuthorInputs(operation="create", template="momentum"),
            outputs=AuthorOutputs(path="strategies/my_strat.py", code_hash="a" * 64),
            package="rift_strategies_sdk",
            version="0.1.0",
        )
        assert rec.typed_outputs().path == "strategies/my_strat.py"

    def test_propose(self, actor, market_snapshot, portfolio_state, risk_metrics):
        rec = build_record(
            actor=actor,
            kind=DecisionKind.PROPOSE,
            inputs=ProposeInputs(
                market_snapshot=market_snapshot,
                portfolio_state=portfolio_state,
                signals={"rsi": Decimal("28"), "atr": Decimal("12.5")},
            ),
            outputs=ProposeOutputs(
                proposal_id=uuid4(),
                legs=[ProposalLeg(
                    coin="ETH", side="buy", size=Decimal("0.2"),
                    order_type="market", stop_loss=Decimal("2275"),
                )],
                expected_pnl=Decimal("8.50"),
                risk=risk_metrics,
            ),
            rationale="RSI=28 below oversold threshold; long ETH with 1% stop",
            package="rift_trade",
            version="0.1.0",
        )
        assert len(rec.typed_outputs().legs) == 1
        assert rec.typed_outputs().legs[0].coin == "ETH"

    def test_authorize(self, actor):
        rec = build_record(
            actor=actor,
            kind=DecisionKind.AUTHORIZE,
            inputs=AuthorizeInputs(
                issuance_mode="session",
                issuer_address="0xabc" + "0" * 37,
            ),
            outputs=AuthorizeOutputs(token_id=uuid4(), expires_at_ms=1715900000000),
            package="rift_trade",
            version="0.1.0",
        )
        assert rec.typed_inputs().issuance_mode == "session"

    def test_execute(self, actor, market_snapshot):
        rec = build_record(
            actor=actor,
            kind=DecisionKind.EXECUTE,
            inputs=ExecuteInputs(
                proposal_id=uuid4(),
                auth_token_id=uuid4(),
                snapshot_at_attempt=market_snapshot,
                api_wallet_address="0xdef" + "0" * 37,
            ),
            outputs=ExecuteOutputs(
                status="filled",
                chain_tx_hash="0x" + "f" * 64,
                fill_price=Decimal("2300.55"),
                fill_size=Decimal("0.2"),
                fee_paid=Decimal("0.14"),
            ),
            rationale="Auto-execute from session token within scope",
            package="rift_trade",
            version="0.1.0",
        )
        assert rec.typed_outputs().status == "filled"

    def test_gate_reject(self, actor):
        rec = build_record(
            actor=actor,
            kind=DecisionKind.GATE_REJECT,
            inputs=GateRejectInputs(proposal_id=uuid4(), gate_name="slippage"),
            outputs=GateRejectOutputs(reason="Expected slippage 1.2% > limit 0.5%",
                                       detail={"expected_pct": 1.2, "limit_pct": 0.5}),
            package="rift_trade",
            version="0.1.0",
        )
        assert rec.typed_inputs().gate_name == "slippage"

    def test_kill_toggle(self, actor):
        rec = build_record(
            actor=actor,
            kind=DecisionKind.KILL_TOGGLE,
            inputs=KillToggleInputs(new_state="on", triggered_by="operator"),
            outputs=KillToggleOutputs(file_flag_present=True, timestamp_ms=1715890234567),
            package="rift_trade",
            version="0.1.0",
        )
        assert rec.typed_outputs().file_flag_present is True


# ─── JSON roundtrip per kind ──────────────────────────────────────────

class TestJSONRoundtrip:
    """Critical: every DecisionRecord must survive a round-trip through
    JSON without data loss. This is what makes the audit log replayable."""

    def test_observe_roundtrip(self, actor):
        rec = build_record(
            actor=actor, kind=DecisionKind.OBSERVE,
            inputs=ObserveInputs(resource="fills", filters={"coin": "ETH"}),
            outputs=ObserveOutputs(rows_returned=928424, bytes_returned=12345678),
            package="rift_data", version="0.1.0",
        )
        j = rec.model_dump_json()
        back = DecisionRecord.model_validate_json(j)
        assert back == rec
        assert back.typed_outputs().rows_returned == 928424

    def test_propose_roundtrip_preserves_decimal(self, actor, market_snapshot, portfolio_state, risk_metrics):
        rec = build_record(
            actor=actor, kind=DecisionKind.PROPOSE,
            inputs=ProposeInputs(market_snapshot=market_snapshot, portfolio_state=portfolio_state,
                                  signals={"rsi": Decimal("28.5")}),
            outputs=ProposeOutputs(
                proposal_id=uuid4(),
                legs=[ProposalLeg(coin="ETH", side="buy", size=Decimal("0.123456"),
                                  order_type="limit", limit_price=Decimal("2300.99"))],
                risk=risk_metrics,
            ),
            rationale="test",
            package="rift_trade", version="0.1.0",
        )
        back = DecisionRecord.model_validate_json(rec.model_dump_json())
        assert back.typed_outputs().legs[0].size == Decimal("0.123456")
        assert back.typed_outputs().legs[0].limit_price == Decimal("2300.99")

    def test_execute_roundtrip_preserves_tx_hash(self, actor, market_snapshot):
        rec = build_record(
            actor=actor, kind=DecisionKind.EXECUTE,
            inputs=ExecuteInputs(proposal_id=uuid4(), auth_token_id=uuid4(),
                                  snapshot_at_attempt=market_snapshot,
                                  api_wallet_address="0xabc" + "0" * 37),
            outputs=ExecuteOutputs(status="filled", chain_tx_hash="0x" + "1" * 64,
                                    fill_price=Decimal("100"), fill_size=Decimal("1"),
                                    fee_paid=Decimal("0.03")),
            rationale="r",
            package="rift_trade", version="0.1.0",
        )
        back = DecisionRecord.model_validate_json(rec.model_dump_json())
        assert back.typed_outputs().chain_tx_hash == "0x" + "1" * 64


# ─── build_record enforces type contracts ─────────────────────────────

class TestBuildRecordValidation:
    def test_rejects_wrong_inputs_type_for_kind(self, actor):
        with pytest.raises(TypeError, match="observe.*ObserveInputs"):
            build_record(
                actor=actor, kind=DecisionKind.OBSERVE,
                inputs=SimulateInputs(kind="backtest", strategy_ref="x", coin="BTC", interval="1h"),
                outputs=ObserveOutputs(rows_returned=10),
                package="x", version="0.1.0",
            )

    def test_rejects_wrong_outputs_type_for_kind(self, actor):
        with pytest.raises(TypeError, match="observe.*ObserveOutputs"):
            build_record(
                actor=actor, kind=DecisionKind.OBSERVE,
                inputs=ObserveInputs(resource="candles"),
                outputs=SimulateOutputs(),
                package="x", version="0.1.0",
            )

    def test_propose_requires_rationale(self, actor, market_snapshot, portfolio_state, risk_metrics):
        """T2 audit records must include rationale per Phase 0 doc."""
        with pytest.raises(ValueError, match="rationale"):
            build_record(
                actor=actor, kind=DecisionKind.PROPOSE,
                inputs=ProposeInputs(market_snapshot=market_snapshot,
                                      portfolio_state=portfolio_state, signals={}),
                outputs=ProposeOutputs(
                    proposal_id=uuid4(),
                    legs=[ProposalLeg(coin="ETH", side="buy", size=Decimal("1"), order_type="market")],
                    risk=risk_metrics,
                ),
                # rationale missing
                package="rift_trade", version="0.1.0",
            )

    def test_execute_requires_rationale(self, actor, market_snapshot):
        """T3 audit records must include rationale per Phase 0 doc."""
        with pytest.raises(ValueError, match="rationale"):
            build_record(
                actor=actor, kind=DecisionKind.EXECUTE,
                inputs=ExecuteInputs(proposal_id=uuid4(), auth_token_id=uuid4(),
                                      snapshot_at_attempt=market_snapshot,
                                      api_wallet_address="0xa" * 40),
                outputs=ExecuteOutputs(status="filled", chain_tx_hash="0x" + "1" * 64,
                                        fill_price=Decimal("100"), fill_size=Decimal("1"),
                                        fee_paid=Decimal("0.03")),
                # rationale missing
                package="rift_trade", version="0.1.0",
            )

    def test_observe_does_not_require_rationale(self, actor):
        """T0 records: rationale is optional."""
        rec = build_record(
            actor=actor, kind=DecisionKind.OBSERVE,
            inputs=ObserveInputs(resource="x"), outputs=ObserveOutputs(rows_returned=1),
            package="rift_data", version="0.1.0",
        )
        assert rec.rationale is None


# ─── Schema map completeness ──────────────────────────────────────────

class TestKindSchemaCompleteness:
    def test_every_kind_has_a_schema_pair(self):
        """If we add a new DecisionKind, KIND_SCHEMAS must be updated too."""
        for kind in DecisionKind:
            assert kind in KIND_SCHEMAS, f"DecisionKind.{kind.name} missing from KIND_SCHEMAS"

    def test_no_phantom_schemas_for_undefined_kinds(self):
        for k in KIND_SCHEMAS:
            assert isinstance(k, DecisionKind)


# ─── DecisionRecord envelope ──────────────────────────────────────────

class TestDecisionRecord:
    def test_default_id_and_timestamp(self, actor):
        rec = build_record(
            actor=actor, kind=DecisionKind.OBSERVE,
            inputs=ObserveInputs(resource="x"), outputs=ObserveOutputs(rows_returned=1),
            package="x", version="0.1.0",
        )
        assert rec.id is not None
        assert rec.timestamp_ms > 0

    def test_rationale_max_length(self, actor):
        with pytest.raises(ValidationError):
            DecisionRecord(
                actor=actor, kind=DecisionKind.PROPOSE,
                inputs={}, outputs={},
                rationale="x" * 5000,
                package="x", version="0.1.0",
            )

    def test_parent_id_linking(self, actor, market_snapshot):
        propose_id = uuid4()
        execute_rec = DecisionRecord(
            actor=actor, kind=DecisionKind.EXECUTE,
            inputs={}, outputs={},
            parent_id=propose_id,
            package="rift_trade", version="0.1.0",
        )
        assert execute_rec.parent_id == propose_id
