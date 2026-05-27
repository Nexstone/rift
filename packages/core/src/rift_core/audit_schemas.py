"""Phase 0 audit substrate — typed DecisionRecord envelope + 8 per-kind schemas.

Every action an AI agent or human operator takes inside RIFT emits a
DecisionRecord. The records are written synchronously to:

  1. NDJSON to stdout (for the TS CLI / MCP server to consume)
  2. Append-only Parquet at ~/.rift/audit/{YYYYMMDD}.parquet

T3 actions (execute_proposal) treat audit-write failure as a HARD ERROR
and refuse to submit the trade. Every cent that moves on-chain has a
typed audit trail that can be replayed via the audit.replay MCP tool.

Schema evolution: add new fields with sensible defaults; never remove
or rename. Bump `version` on the DecisionRecord envelope when breaking
changes ship. Old records remain parseable indefinitely.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from rift_core.keys import Actor, AuthorizationToken, Network


# ─── Decision kinds ───────────────────────────────────────────────────

class DecisionKind(str, Enum):
    """Discriminator for the inputs/outputs schema of a DecisionRecord."""
    OBSERVE = "observe"           # T0 — pure data read
    SIMULATE = "simulate"         # T1 — backtest / MC / walk-forward
    AUTHOR = "author"             # strategy file written or modified
    PROPOSE = "propose"           # T2 — trade or rebalance proposal
    AUTHORIZE = "authorize"       # operator issued an authorization token
    EXECUTE = "execute"           # T3 — chain submission attempt
    GATE_REJECT = "gate_reject"   # safety primitive blocked an action
    KILL_TOGGLE = "kill_toggle"   # global kill switch state changed


# ─── Per-kind input/output schemas ────────────────────────────────────

# Shared building blocks used across multiple kinds

class MarketSnapshot(BaseModel):
    """What the market looked like at decision time. Embedded in PROPOSE
    and EXECUTE records so the agent's view is reproducible."""
    model_config = ConfigDict(frozen=True)

    coin: str
    mid_price: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    funding_rate_1h: Decimal | None = None
    timestamp_ms: int


class PortfolioState(BaseModel):
    """Account context at decision time."""
    model_config = ConfigDict(frozen=True)

    account_address: str
    margin_used: Decimal
    margin_available: Decimal
    open_positions: int
    realized_pnl_today: Decimal


class RiskMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    notional_usd: Decimal
    leverage: Decimal
    max_loss_estimate: Decimal
    correlation_to_existing: Decimal | None = None


# OBSERVE — pure read

class ObserveInputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    resource: str = Field(..., description="What was read: 'candles', 'fills', 'orderbook', 'portfolio', etc.")
    filters: dict = Field(default_factory=dict, description="Query filters (coin, interval, date range, …)")


class ObserveOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    rows_returned: int
    bytes_returned: int | None = None


# SIMULATE — T1

class SimulateInputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["backtest", "walkforward", "montecarlo", "sweep"]
    strategy_ref: str = Field(..., description="Strategy name or inline-code hash")
    coin: str
    interval: str
    params: dict = Field(default_factory=dict)


class SimulateOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    return_pct: Decimal | None = None
    sharpe: Decimal | None = None
    num_trades: int | None = None
    summary: dict = Field(default_factory=dict, description="Free-form per-kind extras")


# AUTHOR — strategy file written

class AuthorInputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation: Literal["create", "modify", "delete"]
    template: str | None = None


class AuthorOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    code_hash: str = Field(..., description="SHA256 of the strategy file (so we don't embed code in audit)")


# PROPOSE — T2

class ProposalLeg(BaseModel):
    """A single leg of a (potentially multi-leg) trade proposal."""
    model_config = ConfigDict(frozen=True)

    coin: str
    side: Literal["buy", "sell"]
    size: Decimal
    order_type: Literal["market", "limit"]
    limit_price: Decimal | None = None
    reduce_only: bool = False
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None


class ProposeInputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    market_snapshot: MarketSnapshot
    portfolio_state: PortfolioState
    signals: dict[str, Decimal] = Field(default_factory=dict, description="Signal values used in the decision")


class ProposeOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_id: UUID
    legs: list[ProposalLeg] = Field(..., min_length=1, max_length=10)
    expected_pnl: Decimal | None = None
    risk: RiskMetrics


# AUTHORIZE — operator issued a token

class AuthorizeInputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    issuance_mode: Literal["per-trade", "session", "long-lived"]
    issuer_address: str


class AuthorizeOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    token_id: UUID
    expires_at_ms: int | None = Field(None, description="None for long-lived tokens")


# EXECUTE — T3 (the trust-critical kind)

class ExecuteInputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_id: UUID
    auth_token_id: UUID
    snapshot_at_attempt: MarketSnapshot
    api_wallet_address: str


class ExecuteOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["filled", "partial", "rejected", "submitted"]
    chain_tx_hash: str | None = None
    fill_price: Decimal | None = None
    fill_size: Decimal | None = None
    fee_paid: Decimal | None = None
    rejection_reason: str | None = None


# GATE_REJECT — safety primitive blocked something

class GateRejectInputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_id: UUID | None = None
    auth_token_id: UUID | None = None
    gate_name: str = Field(..., description="Which gate fired: 'kill_switch', 'circuit_breaker', 'slippage', 'margin', 'correlation', 'scope'")


class GateRejectOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason: str
    detail: dict = Field(default_factory=dict)


# KILL_TOGGLE — global kill switch state changed

class KillToggleInputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    new_state: Literal["on", "off"]
    triggered_by: Literal["operator", "circuit_breaker", "system"]


class KillToggleOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    file_flag_present: bool
    timestamp_ms: int


# ─── DecisionRecord envelope ──────────────────────────────────────────

# Mapping from kind to (inputs, outputs) pair — used by validators.
KIND_SCHEMAS: dict[DecisionKind, tuple[type[BaseModel], type[BaseModel]]] = {
    DecisionKind.OBSERVE: (ObserveInputs, ObserveOutputs),
    DecisionKind.SIMULATE: (SimulateInputs, SimulateOutputs),
    DecisionKind.AUTHOR: (AuthorInputs, AuthorOutputs),
    DecisionKind.PROPOSE: (ProposeInputs, ProposeOutputs),
    DecisionKind.AUTHORIZE: (AuthorizeInputs, AuthorizeOutputs),
    DecisionKind.EXECUTE: (ExecuteInputs, ExecuteOutputs),
    DecisionKind.GATE_REJECT: (GateRejectInputs, GateRejectOutputs),
    DecisionKind.KILL_TOGGLE: (KillToggleInputs, KillToggleOutputs),
}


class DecisionRecord(BaseModel):
    """One typed entry in RIFT's audit log.

    Inputs/outputs are stored as dicts but should always match the
    KIND_SCHEMAS for the corresponding kind. Use `typed_inputs()` and
    `typed_outputs()` to get back the validated Pydantic instance.
    """
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    timestamp_ms: int = Field(
        default_factory=lambda: int(datetime.now(timezone.utc).timestamp() * 1000),
        description="UTC ms since epoch",
    )
    actor: Actor
    kind: DecisionKind

    inputs: dict = Field(..., description="Typed per kind — see KIND_SCHEMAS")
    outputs: dict = Field(..., description="Typed per kind — see KIND_SCHEMAS")
    rationale: str | None = Field(
        default=None,
        description="Required for PROPOSE and EXECUTE; optional for everything else",
        max_length=4096,
    )

    parent_id: UUID | None = Field(
        default=None,
        description="Links into a decision chain (e.g. EXECUTE record points back at the PROPOSE that built it)",
    )
    package: str = Field(..., description="Which rift package emitted this record")
    version: str = Field(..., description="rift version at emit time")

    bundle_id: str | None = Field(
        default=None,
        description=(
            "Optional pointer to the rift_substrate.provenance SealedBundle that "
            "produced this decision. Lets audit traces resolve back to the exact "
            "code commit + data + config + seed that fed the run. NULL on legacy "
            "records and on records emitted before bundle generation is wired in."
        ),
        max_length=64,
    )

    def typed_inputs(self) -> BaseModel:
        """Return the inputs dict re-parsed as the kind's typed schema."""
        in_cls, _ = KIND_SCHEMAS[self.kind]
        return in_cls.model_validate(self.inputs)

    def typed_outputs(self) -> BaseModel:
        """Return the outputs dict re-parsed as the kind's typed schema."""
        _, out_cls = KIND_SCHEMAS[self.kind]
        return out_cls.model_validate(self.outputs)


def build_record(
    actor: Actor,
    kind: DecisionKind,
    inputs: BaseModel,
    outputs: BaseModel,
    *,
    package: str,
    version: str,
    rationale: str | None = None,
    parent_id: UUID | None = None,
    bundle_id: str | None = None,
) -> DecisionRecord:
    """Construct a DecisionRecord from typed input/output models.

    Validates that the provided inputs/outputs match the schemas registered
    for the kind. Use this in preference to constructing DecisionRecord
    directly — it guarantees typed-vs-stored consistency.
    """
    expected_in, expected_out = KIND_SCHEMAS[kind]
    if not isinstance(inputs, expected_in):
        raise TypeError(
            f"kind={kind.value} requires inputs of type {expected_in.__name__}, "
            f"got {type(inputs).__name__}"
        )
    if not isinstance(outputs, expected_out):
        raise TypeError(
            f"kind={kind.value} requires outputs of type {expected_out.__name__}, "
            f"got {type(outputs).__name__}"
        )

    # T2/T3 must have rationale per Phase 0 doc
    if kind in (DecisionKind.PROPOSE, DecisionKind.EXECUTE) and not rationale:
        raise ValueError(f"kind={kind.value} requires a non-empty rationale")

    return DecisionRecord(
        actor=actor,
        kind=kind,
        inputs=inputs.model_dump(mode="json"),
        outputs=outputs.model_dump(mode="json"),
        rationale=rationale,
        parent_id=parent_id,
        package=package,
        version=version,
        bundle_id=bundle_id,
    )
