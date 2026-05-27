"""T2 surface: build, validate, persist trade proposals.

A `TradeProposal` is a structured, immutable plan for one or more trades
that an actor (human or AI agent) has built and intends to submit. It
captures everything needed to:

  1. Re-execute later (so propose() and execute() can be separate operations)
  2. Audit forensically (what did the agent see at decision time?)
  3. Validate at execute time against a fresh market snapshot + auth token

T2 means: no chain access, no auth token required. Anyone can propose.
Execution (T3) is what's gated. propose() does:

  - Input validation (legs non-empty, sizes positive, single-coin, etc.)
  - Risk metric estimation (notional, leverage, max-loss-from-stops)
  - Persist to ~/.rift/proposals/{id}.json (atomic write)
  - Emit a PROPOSE DecisionRecord via the structured-output channel
  - Return the immutable TradeProposal

Storage layout:

  ~/.rift/proposals/
    {uuid}.json        ← one file per proposal, full TradeProposal serialized

Proposals are kept until executed or manually pruned.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from rift_core.audit_schemas import (
    DecisionKind,
    MarketSnapshot,
    PortfolioState,
    ProposalLeg,
    ProposeInputs,
    ProposeOutputs,
    RiskMetrics,
    build_record,
)
from rift_core.keys import Actor
from rift_core.output import emit


# ─── Constants ────────────────────────────────────────────────────────

DEFAULT_PROPOSALS_DIR = Path.home() / ".rift" / "proposals"

# Maximum legs allowed per proposal (matches TokenScope.legs upper bound).
MAX_LEGS = 10

# Reject market snapshots older than this many seconds at propose time.
# Execute will re-check against a fresh snapshot.
MAX_SNAPSHOT_AGE_SECONDS = 60


# ─── TradeProposal model ──────────────────────────────────────────────

class TradeProposal(BaseModel):
    """A fully-built, persisted trade proposal.

    Constructed by `propose_trade()`. Frozen — once built, it's an
    immutable record of what the actor intended. Execute consumes it
    by ID and either fills it (T3) or rejects it (gate failure).
    """
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor: Actor

    legs: list[ProposalLeg] = Field(..., min_length=1, max_length=MAX_LEGS)
    market_snapshot: MarketSnapshot
    portfolio_state: PortfolioState
    signals: dict[str, Decimal] = Field(default_factory=dict)
    risk: RiskMetrics
    rationale: str = Field(..., min_length=1, max_length=4096)
    expected_pnl: Decimal | None = None

    @property
    def coin(self) -> str:
        """Convenience: every leg shares the same coin."""
        return self.legs[0].coin

    @property
    def total_notional_usd(self) -> Decimal:
        """Sum of |size × price| across all legs."""
        total = Decimal("0")
        for leg in self.legs:
            price = leg.limit_price if leg.limit_price is not None else self.market_snapshot.mid_price
            total += leg.size * price
        return total


# ─── Errors ───────────────────────────────────────────────────────────

class ProposeError(ValueError):
    """Raised when a proposal can't be built (validation failure)."""


# ─── Validation helpers ───────────────────────────────────────────────

def _validate_legs(legs: list[ProposalLeg], market: MarketSnapshot) -> None:
    """T2-time leg validation. Pure structural / shape checks only.

    Stricter execution-time checks (margin, slippage, scope) happen in
    execute.py against fresh data + an auth token.
    """
    if not legs:
        raise ProposeError("at least one leg required")
    if len(legs) > MAX_LEGS:
        raise ProposeError(f"max {MAX_LEGS} legs per proposal, got {len(legs)}")

    for i, leg in enumerate(legs):
        if leg.size <= 0:
            raise ProposeError(f"leg[{i}].size must be > 0, got {leg.size}")
        if leg.coin != market.coin:
            raise ProposeError(
                f"leg[{i}].coin={leg.coin} but market snapshot is for {market.coin}. "
                f"Multi-coin proposals are not supported."
            )

        if leg.order_type == "limit" and leg.limit_price is None:
            raise ProposeError(f"leg[{i}] is limit order but limit_price is missing")
        if leg.order_type == "limit" and leg.limit_price is not None and leg.limit_price <= 0:
            raise ProposeError(f"leg[{i}].limit_price must be > 0")

        # Stop loss sanity: stops must be on the loss-side of the entry.
        entry = leg.limit_price if leg.limit_price is not None else market.mid_price
        if leg.stop_loss is not None:
            if leg.side == "buy" and leg.stop_loss >= entry:
                raise ProposeError(
                    f"leg[{i}]: buy stop_loss {leg.stop_loss} must be below entry {entry}"
                )
            if leg.side == "sell" and leg.stop_loss <= entry:
                raise ProposeError(
                    f"leg[{i}]: sell stop_loss {leg.stop_loss} must be above entry {entry}"
                )
        # Take profit sanity: opposite direction from stop_loss.
        if leg.take_profit is not None:
            if leg.side == "buy" and leg.take_profit <= entry:
                raise ProposeError(
                    f"leg[{i}]: buy take_profit {leg.take_profit} must be above entry {entry}"
                )
            if leg.side == "sell" and leg.take_profit >= entry:
                raise ProposeError(
                    f"leg[{i}]: sell take_profit {leg.take_profit} must be below entry {entry}"
                )


def _validate_market_snapshot(market: MarketSnapshot, now: datetime | None = None) -> None:
    """Market snapshot must have sane prices and be reasonably fresh."""
    if market.mid_price <= 0:
        raise ProposeError(f"market.mid_price must be > 0, got {market.mid_price}")
    if market.bid is not None and market.bid <= 0:
        raise ProposeError(f"market.bid must be > 0 if present")
    if market.ask is not None and market.ask <= 0:
        raise ProposeError(f"market.ask must be > 0 if present")

    # Freshness check
    now = now or datetime.now(timezone.utc)
    snapshot_time = datetime.fromtimestamp(market.timestamp_ms / 1000, tz=timezone.utc)
    age_seconds = (now - snapshot_time).total_seconds()
    if age_seconds > MAX_SNAPSHOT_AGE_SECONDS:
        raise ProposeError(
            f"market snapshot is {age_seconds:.0f}s old, exceeds {MAX_SNAPSHOT_AGE_SECONDS}s freshness limit"
        )
    if age_seconds < -5:  # Allow tiny clock skew
        raise ProposeError(f"market snapshot timestamp is in the future ({-age_seconds:.0f}s)")


def _validate_rationale(rationale: str) -> None:
    rationale = rationale.strip()
    if not rationale:
        raise ProposeError("rationale is required for T2 proposals (trust invariant)")
    if len(rationale) > 4096:
        raise ProposeError(f"rationale too long ({len(rationale)} chars, max 4096)")


# ─── Risk estimation ──────────────────────────────────────────────────

def _estimate_risk(
    legs: list[ProposalLeg],
    market: MarketSnapshot,
    portfolio: PortfolioState,
) -> RiskMetrics:
    """Rough risk estimate at propose time. Execute re-computes against
    fresh data — these are just for the audit record and human visibility."""
    total_notional = Decimal("0")
    total_max_loss = Decimal("0")

    for leg in legs:
        price = leg.limit_price if leg.limit_price is not None else market.mid_price
        notional = leg.size * price
        total_notional += notional

        # Max loss from stop_loss, if any. Otherwise: open-ended (estimate
        # as 20% of notional — conservative placeholder).
        if leg.stop_loss is not None:
            if leg.side == "buy":
                loss_per_unit = price - leg.stop_loss
            else:  # sell
                loss_per_unit = leg.stop_loss - price
            total_max_loss += leg.size * max(loss_per_unit, Decimal("0"))
        else:
            total_max_loss += notional * Decimal("0.20")

    # Leverage approximation: notional / available margin. Doesn't account
    # for HL's specific margin calculation; this is a sanity floor.
    leverage: Decimal
    if portfolio.margin_available > 0:
        leverage = total_notional / portfolio.margin_available
    else:
        leverage = Decimal("999")  # sentinel: no margin available

    return RiskMetrics(
        notional_usd=total_notional,
        leverage=leverage,
        max_loss_estimate=total_max_loss,
        correlation_to_existing=None,  # not yet computed
    )


# ─── Persistence ──────────────────────────────────────────────────────

def _proposals_dir(override: Path | None = None) -> Path:
    d = override or DEFAULT_PROPOSALS_DIR
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def save_proposal(proposal: TradeProposal, proposals_dir: Path | None = None) -> Path:
    """Atomically write a proposal to disk. Returns the file path.

    File: {proposals_dir}/{id}.json (perms 0600).
    """
    d = _proposals_dir(proposals_dir)
    path = d / f"{proposal.id}.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(proposal.model_dump_json())
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def get_proposal(proposal_id: UUID, proposals_dir: Path | None = None) -> TradeProposal | None:
    """Look up a proposal by ID. Returns None if not found or corrupt.

    Used by execute_proposal() to fetch the proposal that's about to be executed.
    """
    d = _proposals_dir(proposals_dir)
    path = d / f"{proposal_id}.json"
    if not path.exists():
        return None
    try:
        return TradeProposal.model_validate_json(path.read_text())
    except Exception:
        return None


def list_recent_proposals(
    limit: int = 20,
    proposals_dir: Path | None = None,
) -> list[TradeProposal]:
    """List the N most recent proposals (newest first), sorted by file mtime.

    For debugging, audit queries, and the rift auth list-proposals command
    (if we ship one). Skips files that fail to parse.
    """
    d = _proposals_dir(proposals_dir)
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[TradeProposal] = []
    for f in files[:limit]:
        try:
            out.append(TradeProposal.model_validate_json(f.read_text()))
        except Exception:
            continue
    return out


def delete_proposal(proposal_id: UUID, proposals_dir: Path | None = None) -> bool:
    """Remove a proposal file. Returns True if removed, False if it didn't exist."""
    d = _proposals_dir(proposals_dir)
    path = d / f"{proposal_id}.json"
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


# ─── The T2 surface — propose_trade() ─────────────────────────────────

def propose_trade(
    *,
    actor: Actor,
    legs: list[ProposalLeg],
    market_snapshot: MarketSnapshot,
    portfolio_state: PortfolioState,
    rationale: str,
    signals: dict[str, Decimal] | None = None,
    expected_pnl: Decimal | None = None,
    proposals_dir: Path | None = None,
    emit_audit: bool = True,
    _now: datetime | None = None,  # injectable for tests
) -> TradeProposal:
    """Build, validate, persist, and audit-emit a trade proposal.

    This is the T2 entry point — anyone (human, AI agent, system) can call
    it. No auth token required. No chain access. The returned TradeProposal
    is what execute_proposal() consumes (with an auth token) to actually
    place the trade.

    Args:
      actor:           who is proposing (Actor type — human / agent / system)
      legs:            one or more ProposalLeg objects (all same coin)
      market_snapshot: current book + funding at decision time
      portfolio_state: account context at decision time
      rationale:       REQUIRED — human-readable why-this-trade explanation
                       (trust invariant — every T2 record must justify itself)
      signals:         optional dict of signal values used (rsi, atr, etc.)
      expected_pnl:    optional projected pnl (for audit & sanity)
      proposals_dir:   override storage location (tests)
      emit_audit:      if True (default), emit a PROPOSE DecisionRecord via emit()
      _now:            injected current time for tests (snapshot freshness check)

    Returns:
      The persisted, immutable TradeProposal.

    Raises:
      ProposeError if any shape/sanity validation fails.
    """
    # Validate every layer
    _validate_market_snapshot(market_snapshot, now=_now)
    _validate_legs(legs, market_snapshot)
    _validate_rationale(rationale)

    risk = _estimate_risk(legs, market_snapshot, portfolio_state)

    proposal = TradeProposal(
        actor=actor,
        legs=legs,
        market_snapshot=market_snapshot,
        portfolio_state=portfolio_state,
        signals=signals or {},
        risk=risk,
        rationale=rationale,
        expected_pnl=expected_pnl,
    )

    save_proposal(proposal, proposals_dir=proposals_dir)

    if emit_audit:
        record = build_record(
            actor=actor,
            kind=DecisionKind.PROPOSE,
            inputs=ProposeInputs(
                market_snapshot=market_snapshot,
                portfolio_state=portfolio_state,
                signals=signals or {},
            ),
            outputs=ProposeOutputs(
                proposal_id=proposal.id,
                legs=legs,
                expected_pnl=expected_pnl,
                risk=risk,
            ),
            rationale=rationale,
            package="rift_trade",
            version="0.1.0",
        )
        emit({"type": "audit_record", "record": record.model_dump(mode="json")})

    return proposal
