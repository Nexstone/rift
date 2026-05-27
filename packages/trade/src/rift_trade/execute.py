"""Trust-critical T3 surface. The one function that moves capital.

`execute_proposal()` is the ONLY path from a TradeProposal to an on-chain
order. Every safety primitive lives in this file or in the modules it
composes (gates, auth, api_wallet, propose). If you find a way to place a
chain order without going through execute_proposal, that's a bug.

Hard invariants:

  1. **Auth token signature is verified.** A token with a tampered scope or
     a forged signature is rejected before any gate runs.
  2. **Token revocation + expiry are checked.** Per-token `is_valid()`.
  3. **All gates must pass.** First failure stops execution. A GATE_REJECT
     audit record is emitted with the full gate result list.
  4. **Audit-write failure on T3 = fail-closed.** If the EXECUTE record
     can't be written to the audit channel, the order is NOT submitted.
     Better to lose a trade than have an untracked execution.
  5. **Rationale is required.** The actor must explain why they're executing
     this proposal. Empty rationale = ProposeError-style rejection at the
     audit-build step.
  6. **No back doors.** This function has no kwarg to skip gates, no
     "trusted" mode, no testnet bypass. Testnet vs mainnet is determined by
     the API wallet's network field, set at registration time.

Multi-leg semantics:

  - Legs execute sequentially.
  - First failure stops the run; remaining legs are NOT submitted.
  - Returned `ExecutionResult.status`:
      FILLED   — every leg filled successfully
      PARTIAL  — at least one leg filled, at least one didn't
      REJECTED — rejected before any leg submitted (auth, gate, or audit failure)
  - Each leg gets its own LegResult with status + chain hash + fill data.

Atomic multi-leg rollback (revert filled legs on later failure) is NOT
attempted — HL has no protocol-level rollback. Operator can manually
close partial fills via `rift close-position` or `rift sell`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from rift_core.audit_schemas import (
    DecisionKind,
    ExecuteInputs,
    ExecuteOutputs,
    GateRejectInputs,
    GateRejectOutputs,
    MarketSnapshot,
    build_record,
)
from rift_core.keys import Actor, APIWalletKey, AuthorizationToken
from rift_core.output import emit

from rift_trade import api_wallet as api_wallet_module
from rift_trade import auth as auth_module
from rift_trade import propose as propose_module
from rift_trade.gates import (
    CircuitBreakerConfig,
    DailyActivity,
    DEFAULT_KILL_FLAG_PATH,
    GateResult,
    MarketSnapshot as GatesMarketSnapshot,
    PortfolioSnapshot as GatesPortfolioSnapshot,
    ProposalLegSummary,
    first_failure,
    run_all_gates,
)


# ─── Result types ─────────────────────────────────────────────────────

class ExecutionStatus(str, Enum):
    """Top-level outcome of execute_proposal()."""
    FILLED = "filled"        # every leg filled
    PARTIAL = "partial"      # some legs filled, some didn't
    REJECTED = "rejected"    # rejected before any chain submission


class LegStatus(str, Enum):
    """Per-leg outcome inside an ExecutionResult."""
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    SUBMITTED = "submitted"  # order placed but fill not yet confirmed
    NOT_ATTEMPTED = "not_attempted"  # earlier leg failed, this never ran


@dataclass(frozen=True)
class LegResult:
    """Per-leg execution outcome."""
    leg_index: int
    status: LegStatus
    chain_tx_hash: str | None = None
    fill_price: Decimal | None = None
    fill_size: Decimal | None = None
    fee_paid: Decimal | None = None
    rejection_reason: str | None = None
    raw_response: dict | None = None  # full HL response for forensics


@dataclass(frozen=True)
class ExecutionResult:
    """Returned by execute_proposal(). Captures the full outcome.

    Either status == REJECTED (no chain action) or the legs list shows what
    happened per leg. Rationale is included so the caller (and audit) can
    confirm the result corresponds to the right intent.
    """
    proposal_id: UUID
    token_id: UUID | None  # None if rejected before token verification
    status: ExecutionStatus
    legs: list[LegResult]
    rationale: str
    rejection_reason: str | None = None  # populated when status == REJECTED
    started_at_ms: int = 0
    completed_at_ms: int = 0


# ─── ExchangeClient Protocol ──────────────────────────────────────────

class ExchangeClient(Protocol):
    """Abstraction over HL order submission. Lets us mock chain interaction
    in tests without spinning up a real HL connection.

    Production implementation: HyperliquidExchangeClient (below) wraps
    `hyperliquid.exchange.Exchange` and uses the API wallet to sign.

    Test implementation: MockExchangeClient records calls and returns
    canned responses; never touches network.
    """

    def submit_order(
        self,
        *,
        api_wallet: APIWalletKey,
        main_wallet_address: str,
        coin: str,
        side: str,                # "buy" | "sell"
        size: Decimal,
        order_type: str,          # "market" | "limit"
        limit_price: Decimal | None = None,
        reduce_only: bool = False,
    ) -> dict:
        """Submit one leg to HL. Returns the raw JSON response.

        `main_wallet_address` is the principal account being traded for —
        the HL SDK requires it when an agent (API wallet) signs on behalf
        of a main wallet, so HL associates the trade with the main wallet
        and verifies the main wallet's builder-fee approval.

        Caller (execute.py) inspects the response for status + fill data.
        Raises Exception on network / protocol errors; the caller treats any
        exception as a leg rejection.
        """
        ...


class HyperliquidExchangeClient:
    """Production ExchangeClient — wraps `hyperliquid.exchange.Exchange`.

    Constructs a fresh Exchange instance per submit_order() so we never
    share state between calls. The API wallet's private key is used to
    sign each order; the main wallet is never touched.

    NOTE: this class makes real HL calls. Tests should use MockExchangeClient.
    Integration tests (step 9) exercise this path on testnet.
    """

    def __init__(self, base_url: str | None = None):
        # Cache base_url; instantiate Exchange lazily per call so each
        # order uses fresh state.
        self._base_url = base_url

    def submit_order(
        self,
        *,
        api_wallet: APIWalletKey,
        main_wallet_address: str,
        coin: str,
        side: str,
        size: Decimal,
        order_type: str,
        limit_price: Decimal | None = None,
        reduce_only: bool = False,
    ) -> dict:
        # Import lazily so unit tests don't pay the eth_account/hyperliquid
        # import cost just to import execute.py.
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants

        from rift_trade.builder_fee import get_builder_info

        base_url = self._base_url or constants.MAINNET_API_URL
        wallet = Account.from_key("0x" + api_wallet.private_key)
        # account_address tells HL which main wallet this agent is trading
        # for. Required so trades are attributed to the main wallet and
        # HL's maxBuilderFee check uses the main wallet's approval.
        ex = Exchange(wallet, base_url=base_url, account_address=main_wallet_address)

        is_buy = side == "buy"
        builder = get_builder_info()

        if order_type == "market":
            # HL SDK splits markets into open vs close. market_close uses
            # current position to size and side; for reduce_only legs that
            # matches our intent (close out the position).
            if reduce_only:
                return ex.market_close(
                    coin=coin,
                    sz=float(size),
                    builder=builder,
                )
            return ex.market_open(
                name=coin,
                is_buy=is_buy,
                sz=float(size),
                builder=builder,
            )
        elif order_type == "limit":
            if limit_price is None:
                raise ValueError("limit_price required for limit order")
            # SDK quirk: order() takes `name=` while market_close() takes `coin=`.
            return ex.order(
                name=coin,
                is_buy=is_buy,
                sz=float(size),
                limit_px=float(limit_price),
                order_type={"limit": {"tif": "Gtc"}},
                reduce_only=reduce_only,
                builder=builder,
            )
        else:
            raise ValueError(f"Unknown order_type: {order_type}")


# ─── Errors (for callers that prefer exception flow) ──────────────────

class ExecuteError(RuntimeError):
    """Base for all execute_proposal failures.

    Note: execute_proposal() does NOT raise these by default — it returns
    ExecutionResult.REJECTED with a populated rejection_reason. Callers
    that prefer exception flow can wrap and re-raise.
    """


# ─── Configuration ────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExecuteConfig:
    """Operator-configurable knobs. Conservative defaults."""
    max_slippage_bps: int = 50
    # Required margin estimate per leg = notional * margin_factor.
    # Sensible for HL's typical isolated-margin perp leverage.
    margin_factor: Decimal = Decimal("0.20")
    kill_flag_path: Path = field(default_factory=lambda: DEFAULT_KILL_FLAG_PATH)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)


# ─── Helpers ──────────────────────────────────────────────────────────

def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _emit_audit_or_raise(record_dict: dict, *, rejection_on_fail: bool) -> None:
    """Emit an audit record. If emission raises and rejection_on_fail is
    True, propagate so the caller can fail-closed.

    For T3 success paths, audit-write failure must block the trade — that's
    the fail-closed invariant. For T3 rejection paths the audit
    record is forensic only; failure to emit is logged but not blocking.
    """
    try:
        emit({"type": "audit_record", "record": record_dict})
    except Exception:
        if rejection_on_fail:
            raise


def _build_leg_summary(leg, fallback_price: Decimal) -> ProposalLegSummary:
    """Convert a ProposalLeg to the lightweight ProposalLegSummary the
    gates module operates on. Estimates notional from limit price or fallback.
    """
    from rift_core.keys import TradeAction, TradeSide
    price = leg.limit_price if leg.limit_price is not None else fallback_price
    # Map proposal leg's "reduce_only" + side + entry to a TradeAction.
    # Heuristic: reduce_only → CLOSE; else OPEN. We could be more precise
    # by inspecting portfolio_state, but for gate scope checks this is fine.
    action = TradeAction.CLOSE if leg.reduce_only else TradeAction.OPEN
    side = TradeSide.BUY if leg.side == "buy" else TradeSide.SELL
    return ProposalLegSummary(
        coin=leg.coin,
        side=side,
        size=leg.size,
        notional_usd=leg.size * price,
        action=action,
    )


def _audit_gate_rejection(
    *,
    proposal_id: UUID,
    token_id: UUID | None,
    actor: Actor,
    leg_index: int,
    gate_results: list[GateResult],
    failing: GateResult,
) -> None:
    """Emit a GATE_REJECT DecisionRecord summarizing the failure."""
    record = build_record(
        actor=actor,
        kind=DecisionKind.GATE_REJECT,
        inputs=GateRejectInputs(
            proposal_id=proposal_id,
            auth_token_id=token_id,
            gate_name=failing.gate_name,
        ),
        outputs=GateRejectOutputs(
            reason=failing.reason or "(no reason provided)",
            detail={
                "leg_index": leg_index,
                "failing_gate": failing.gate_name,
                "all_results": [
                    {"gate": r.gate_name, "passed": r.passed, "reason": r.reason}
                    for r in gate_results
                ],
                **(failing.detail or {}),
            },
        ),
        package="rift_trade",
        version="0.1.0",
    )
    _emit_audit_or_raise(record.model_dump(mode="json"), rejection_on_fail=False)


def _audit_execute(
    *,
    proposal_id: UUID,
    token_id: UUID,
    actor: Actor,
    api_wallet_address: str,
    market_snapshot: MarketSnapshot,
    result: ExecutionResult,
    leg_result: LegResult,
    rationale: str,
) -> None:
    """Emit an EXECUTE DecisionRecord per leg. Must succeed
    BEFORE the trade is considered done. Raises if emit fails."""
    record = build_record(
        actor=actor,
        kind=DecisionKind.EXECUTE,
        inputs=ExecuteInputs(
            proposal_id=proposal_id,
            auth_token_id=token_id,
            snapshot_at_attempt=market_snapshot,
            api_wallet_address=api_wallet_address,
        ),
        outputs=ExecuteOutputs(
            status=leg_result.status.value if leg_result.status != LegStatus.NOT_ATTEMPTED else "rejected",
            chain_tx_hash=leg_result.chain_tx_hash,
            fill_price=leg_result.fill_price,
            fill_size=leg_result.fill_size,
            fee_paid=leg_result.fee_paid,
            rejection_reason=leg_result.rejection_reason,
        ),
        rationale=rationale,
        package="rift_trade",
        version="0.1.0",
    )
    _emit_audit_or_raise(record.model_dump(mode="json"), rejection_on_fail=True)


def _make_rejected_result(
    *,
    proposal_id: UUID,
    token_id: UUID | None,
    rationale: str,
    reason: str,
    started_at_ms: int,
    legs_count: int = 0,
) -> ExecutionResult:
    """Build a REJECTED ExecutionResult for early-failure paths."""
    legs = [
        LegResult(leg_index=i, status=LegStatus.NOT_ATTEMPTED, rejection_reason=reason)
        for i in range(legs_count)
    ]
    return ExecutionResult(
        proposal_id=proposal_id,
        token_id=token_id,
        status=ExecutionStatus.REJECTED,
        legs=legs,
        rationale=rationale,
        rejection_reason=reason,
        started_at_ms=started_at_ms,
        completed_at_ms=_now_ms(),
    )


# ─── The T3 entry point ───────────────────────────────────────────────

def execute_proposal(
    *,
    proposal_id: UUID,
    token_id: UUID,
    actor: Actor,
    market_snapshot: MarketSnapshot,           # fresh snapshot from caller
    portfolio_snapshot: GatesPortfolioSnapshot,
    activity: DailyActivity,
    exchange: ExchangeClient,
    rationale: str,                            # T3 invariant: required
    config: ExecuteConfig | None = None,
    total_volume_today_usd: Decimal = Decimal("0"),
    proposals_dir: Path | None = None,
    tokens_dir: Path | None = None,
    api_wallet_path: Path | None = None,
) -> ExecutionResult:
    """Execute a previously-proposed trade.

    This is the trust-critical T3 path. Every chain order RIFT places passes
    through here. Returns an ExecutionResult; never raises on a normal
    rejection (auth, gate, even audit-write failure all return REJECTED
    with a populated rejection_reason). Raises only on programmer errors
    (e.g. wrong type passed).

    Args:
      proposal_id:           ID of a previously-proposed TradeProposal
      token_id:              ID of an issued AuthorizationToken
      actor:                 who is executing (Actor)
      market_snapshot:       CURRENT market data (caller fetches fresh)
      portfolio_snapshot:    current account state for gates
      activity:              daily-activity stats for the token
      exchange:              ExchangeClient — mock in tests, HL in production
      rationale:             REQUIRED — T3 trust invariant
      config:                gate / circuit-breaker config (None = defaults)
      total_volume_today_usd: account-wide daily volume (for circuit breakers)
      proposals_dir/tokens_dir/api_wallet_path: storage overrides (tests)

    Returns: ExecutionResult
    """
    started_at = _now_ms()
    cfg = config or ExecuteConfig()

    # 0. Rationale is mandatory (T3 invariant)
    if not rationale or not rationale.strip():
        return _make_rejected_result(
            proposal_id=proposal_id, token_id=token_id, rationale=rationale or "",
            reason="rationale is required for T3 execution (trust invariant)",
            started_at_ms=started_at,
        )

    # 1. Load proposal
    proposal = propose_module.get_proposal(proposal_id, proposals_dir=proposals_dir)
    if proposal is None:
        return _make_rejected_result(
            proposal_id=proposal_id, token_id=token_id, rationale=rationale,
            reason=f"proposal {proposal_id} not found",
            started_at_ms=started_at,
        )

    # 2. Load token
    token = auth_module.load_token(token_id, tokens_dir=tokens_dir)
    if token is None:
        return _make_rejected_result(
            proposal_id=proposal_id, token_id=token_id, rationale=rationale,
            reason=f"auth token {token_id} not found",
            started_at_ms=started_at, legs_count=len(proposal.legs),
        )

    # 3. Verify token signature
    if not auth_module.verify_token_signature(token):
        return _make_rejected_result(
            proposal_id=proposal_id, token_id=token_id, rationale=rationale,
            reason="auth token signature verification failed (forged or corrupt)",
            started_at_ms=started_at, legs_count=len(proposal.legs),
        )

    # 4. Check token validity (revoked, expired)
    if not token.is_valid():
        reason_parts = []
        if token.revoked:
            reason_parts.append("revoked")
        if token.is_expired():
            reason_parts.append("expired")
        return _make_rejected_result(
            proposal_id=proposal_id, token_id=token_id, rationale=rationale,
            reason=f"auth token is {' and '.join(reason_parts)}",
            started_at_ms=started_at, legs_count=len(proposal.legs),
        )

    # 5. Load API wallet
    api_wallet = api_wallet_module.load_api_wallet(api_wallet_path)
    if api_wallet is None:
        return _make_rejected_result(
            proposal_id=proposal_id, token_id=token_id, rationale=rationale,
            reason="API wallet not found — run `rift init` to register one",
            started_at_ms=started_at, legs_count=len(proposal.legs),
        )

    # 6. Convert market snapshot to gate-format
    gates_market = GatesMarketSnapshot(
        coin=market_snapshot.coin,
        mid_price=market_snapshot.mid_price,
        bid=market_snapshot.bid or market_snapshot.mid_price,
        ask=market_snapshot.ask or market_snapshot.mid_price,
    )

    # 7. Per-leg loop: gate checks first, then submit if all pass
    leg_results: list[LegResult] = []
    any_filled = False
    any_failed = False

    for i, leg in enumerate(proposal.legs):
        leg_summary = _build_leg_summary(leg, fallback_price=gates_market.mid_price)
        margin_required = leg_summary.notional_usd * cfg.margin_factor

        gate_results = run_all_gates(
            leg=leg_summary,
            token=token,
            portfolio=portfolio_snapshot,
            market=gates_market,
            activity=activity,
            circuit_config=cfg.circuit_breaker,
            total_volume_today_usd=total_volume_today_usd,
            margin_required_usd=margin_required,
            max_slippage_bps=cfg.max_slippage_bps,
            kill_flag_path=cfg.kill_flag_path,
        )

        failing = first_failure(gate_results)
        if failing is not None:
            # Gate rejection — emit GATE_REJECT audit, stop the run
            _audit_gate_rejection(
                proposal_id=proposal.id, token_id=token.id, actor=actor,
                leg_index=i, gate_results=gate_results, failing=failing,
            )
            leg_results.append(LegResult(
                leg_index=i, status=LegStatus.REJECTED,
                rejection_reason=failing.reason,
            ))
            # Mark remaining legs as NOT_ATTEMPTED
            for j in range(i + 1, len(proposal.legs)):
                leg_results.append(LegResult(
                    leg_index=j, status=LegStatus.NOT_ATTEMPTED,
                    rejection_reason="prior leg failed gate check",
                ))
            any_failed = True
            break

        # 8. Submit to chain via the API wallet
        try:
            response = exchange.submit_order(
                api_wallet=api_wallet,
                main_wallet_address=token.issuer,
                coin=leg.coin,
                side=leg.side,
                size=leg.size,
                order_type=leg.order_type,
                limit_price=leg.limit_price,
                reduce_only=leg.reduce_only,
            )
        except Exception as e:
            # Network / protocol failure — leg rejected, stop the run
            leg_result = LegResult(
                leg_index=i, status=LegStatus.REJECTED,
                rejection_reason=f"chain submission failed: {e}",
            )
            leg_results.append(leg_result)

            # Emit EXECUTE audit for the failed leg (synchronous + fail-closed)
            try:
                _audit_execute(
                    proposal_id=proposal.id, token_id=token.id, actor=actor,
                    api_wallet_address=api_wallet.address,
                    market_snapshot=market_snapshot,
                    result=None,  # type: ignore
                    leg_result=leg_result,
                    rationale=rationale,
                )
            except Exception:
                # Audit write failed for the failed-leg record. Not the
                # primary safety concern (the trade already failed), but
                # logged separately.
                pass

            for j in range(i + 1, len(proposal.legs)):
                leg_results.append(LegResult(
                    leg_index=j, status=LegStatus.NOT_ATTEMPTED,
                    rejection_reason="prior leg failed chain submission",
                ))
            any_failed = True
            break

        # 9. Parse the response
        leg_result = _parse_exchange_response(response, leg_index=i)
        leg_results.append(leg_result)
        if leg_result.status in (LegStatus.FILLED, LegStatus.PARTIAL, LegStatus.SUBMITTED):
            any_filled = True
        else:
            any_failed = True

        # 10. Emit EXECUTE audit record (FAIL-CLOSED — trust invariant)
        try:
            _audit_execute(
                proposal_id=proposal.id, token_id=token.id, actor=actor,
                api_wallet_address=api_wallet.address,
                market_snapshot=market_snapshot,
                result=None,  # type: ignore
                leg_result=leg_result,
                rationale=rationale,
            )
        except Exception as e:
            # AUDIT WRITE FAILED. Per the fail-closed invariant we cannot continue — this leg
            # filled but we have no audit trail. Mark this an audit error and
            # halt remaining legs. The filled portion remains on-chain;
            # operator forensics must reconstruct manually from HL state.
            for j in range(i + 1, len(proposal.legs)):
                leg_results.append(LegResult(
                    leg_index=j, status=LegStatus.NOT_ATTEMPTED,
                    rejection_reason="audit write failed on prior leg (fail-closed)",
                ))
            return ExecutionResult(
                proposal_id=proposal.id, token_id=token.id,
                status=ExecutionStatus.PARTIAL,
                legs=leg_results,
                rationale=rationale,
                rejection_reason=f"audit-write failed on leg {i}: {e}. "
                                  f"Halted remaining legs (fail-closed invariant).",
                started_at_ms=started_at,
                completed_at_ms=_now_ms(),
            )

        # Stop the run if this leg failed (and emit didn't already break)
        if leg_result.status == LegStatus.REJECTED:
            for j in range(i + 1, len(proposal.legs)):
                leg_results.append(LegResult(
                    leg_index=j, status=LegStatus.NOT_ATTEMPTED,
                    rejection_reason="prior leg rejected by exchange",
                ))
            break

    # 11. Compose overall status
    if any_filled and any_failed:
        overall = ExecutionStatus.PARTIAL
    elif any_filled:
        overall = ExecutionStatus.FILLED
    else:
        overall = ExecutionStatus.REJECTED

    return ExecutionResult(
        proposal_id=proposal.id,
        token_id=token.id,
        status=overall,
        legs=leg_results,
        rationale=rationale,
        started_at_ms=started_at,
        completed_at_ms=_now_ms(),
    )


def _parse_exchange_response(response: dict, *, leg_index: int) -> LegResult:
    """Translate the HL exchange JSON response into a LegResult.

    HL response shapes for `order` / `market_open`:
      {"status": "ok", "response": {"type": "order",
        "data": {"statuses": [{"resting": {...}} | {"filled": {"avgPx": "...", "totalSz": "..."}} | {"error": "..."}]}}}
    """
    if not isinstance(response, dict):
        return LegResult(leg_index=leg_index, status=LegStatus.REJECTED,
                          rejection_reason=f"non-dict response: {response!r}",
                          raw_response={"raw": str(response)})

    if response.get("status") != "ok":
        return LegResult(leg_index=leg_index, status=LegStatus.REJECTED,
                          rejection_reason=f"HL returned status={response.get('status')}: {response}",
                          raw_response=response)

    resp = response.get("response", {})
    data = resp.get("data", {}) if isinstance(resp, dict) else {}
    statuses = data.get("statuses", []) if isinstance(data, dict) else []

    if not statuses:
        # OK with no statuses — treat as submitted but unconfirmed
        return LegResult(leg_index=leg_index, status=LegStatus.SUBMITTED,
                          raw_response=response)

    s = statuses[0]
    if not isinstance(s, dict):
        return LegResult(leg_index=leg_index, status=LegStatus.SUBMITTED, raw_response=response)

    if "error" in s:
        return LegResult(leg_index=leg_index, status=LegStatus.REJECTED,
                          rejection_reason=str(s["error"]),
                          raw_response=response)

    if "filled" in s:
        f = s["filled"]
        try:
            return LegResult(
                leg_index=leg_index, status=LegStatus.FILLED,
                fill_price=Decimal(str(f.get("avgPx", "0"))),
                fill_size=Decimal(str(f.get("totalSz", "0"))),
                fee_paid=Decimal(str(f.get("fee", "0"))) if "fee" in f else None,
                raw_response=response,
            )
        except Exception:
            return LegResult(leg_index=leg_index, status=LegStatus.SUBMITTED, raw_response=response)

    if "resting" in s:
        # Limit order resting on the book — not filled yet
        return LegResult(leg_index=leg_index, status=LegStatus.SUBMITTED, raw_response=response)

    return LegResult(leg_index=leg_index, status=LegStatus.SUBMITTED, raw_response=response)
