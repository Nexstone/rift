"""Phase 0 execution gates — pre-trade safety checks that fire before
every T3 (chain submission) action.

NOTE on naming: there's an existing `trading_gates.py` in this package
that handles SETUP gates (do you have an API key? did you approve the
builder fee?). This module is a DIFFERENT concern: execution-time gates
(is the kill switch on? does this trade fit the auth token's scope?
is expected slippage too high?). They coexist; trading_gates runs once
at install/auth time; gates runs on every trade.

All gate functions are PURE (no chain access, no network). The kill
switch does a single file existence check; everything else operates on
state/snapshots passed in as arguments. This makes gates fully unit-
testable and predictable.

Per the Phase 0 doc invariant: any gate failure on a T3 action MUST
result in a GATE_REJECT audit record and the trade NOT being submitted.
The execute.py module is the one that enforces this; gates here just
return structured pass/reject results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from rift_core.keys import AuthorizationToken, TokenScope, TradeAction, TradeSide


# ─── Constants ────────────────────────────────────────────────────────

# Default location for the kill switch file flag. Existence = active.
DEFAULT_KILL_FLAG_PATH = Path.home() / ".rift" / "KILL"


# ─── Gate result ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class GateResult:
    """Structured outcome of a single gate check.

    `passed=True` means the trade may proceed (this gate is OK).
    `passed=False` means this gate blocks the trade; `reason` explains why.
    """
    passed: bool
    gate_name: str
    reason: str | None = None
    detail: dict = field(default_factory=dict)

    @classmethod
    def ok(cls, gate_name: str) -> "GateResult":
        return cls(passed=True, gate_name=gate_name)

    @classmethod
    def reject(cls, gate_name: str, reason: str, **detail) -> "GateResult":
        return cls(passed=False, gate_name=gate_name, reason=reason, detail=detail)


# ─── Inputs (data shapes the gates operate on) ────────────────────────

@dataclass(frozen=True)
class ProposalLegSummary:
    """Just enough of a ProposalLeg for gates to inspect.

    Gates intentionally don't depend on rift_core.audit_schemas.ProposalLeg
    so this module stays a leaf — no circular dependency on the audit layer.
    """
    coin: str
    side: TradeSide
    size: Decimal
    notional_usd: Decimal
    action: TradeAction


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Account state at the moment a trade is being proposed/executed."""
    margin_used: Decimal
    margin_available: Decimal
    open_positions: int
    realized_pnl_today: Decimal


def build_portfolio_snapshot(info, address: str, realized_pnl_today: Decimal = Decimal("0")) -> "PortfolioSnapshot":
    """Build a PortfolioSnapshot from live HL state, mode-aware.

    Uses rift_data.account_mode.read_collateral so that Unified and
    Portfolio-Margin users get their spot USDC counted as available
    margin (which it is, under those modes — HL routes from spot
    automatically). Standard users only see perp balance.

    `realized_pnl_today` is not derivable from a single state read —
    caller passes it (typically computed from the daily-activity tracker).
    """
    from rift_data.account_mode import read_collateral

    collateral = read_collateral(info, address)
    state = info.user_state(address.lower())
    open_positions = len(state.get("assetPositions", []))
    return PortfolioSnapshot(
        margin_used=collateral.perp_margin_used,
        margin_available=collateral.total,
        open_positions=open_positions,
        realized_pnl_today=realized_pnl_today,
    )


@dataclass(frozen=True)
class DailyActivity:
    """How much trading happened under this token today (for circuit breakers
    and daily scope cap)."""
    token_id: str
    volume_today_usd: Decimal
    actions_today: int


@dataclass(frozen=True)
class MarketSnapshot:
    """Current book at attempt time, used for slippage checks."""
    coin: str
    mid_price: Decimal
    bid: Decimal
    ask: Decimal


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """Hard caps that supersede any token. Operator config, not AI-modifiable."""
    max_daily_volume_usd: Decimal | None = None
    max_open_positions: int | None = None
    max_drawdown_pct_today: Decimal | None = None  # e.g. Decimal("0.05") = 5%
    starting_equity_today: Decimal | None = None   # needed to compute drawdown


# ─── Individual gates ─────────────────────────────────────────────────

def check_kill_switch(flag_path: Path = DEFAULT_KILL_FLAG_PATH) -> GateResult:
    """Highest-priority gate: if `~/.rift/KILL` exists, all T3 actions
    are blocked regardless of token, regardless of source. The operator
    sets the flag; AI cannot clear it."""
    if flag_path.exists():
        try:
            content = flag_path.read_text(errors="replace").strip()[:200]
        except OSError:
            content = ""
        return GateResult.reject(
            "kill_switch",
            "Global kill switch is active. No new T3 actions will proceed.",
            flag_path=str(flag_path),
            flag_content=content,
        )
    return GateResult.ok("kill_switch")


def check_token_scope(leg: ProposalLegSummary, token: AuthorizationToken) -> GateResult:
    """Verify a proposed leg fits within the auth token's scope envelope."""
    if not token.is_valid():
        return GateResult.reject(
            "scope",
            "Token is revoked or expired.",
            token_id=str(token.id),
            revoked=token.revoked,
            expired=token.is_expired(),
        )

    scope = token.scope

    # Coins
    if scope.coins != "any" and leg.coin not in scope.coins:
        return GateResult.reject(
            "scope",
            f"Coin {leg.coin} not allowed by token (allowed: {scope.coins}).",
            allowed_coins=scope.coins,
            requested_coin=leg.coin,
        )

    # Sides
    if scope.sides != "any" and leg.side not in scope.sides:
        return GateResult.reject(
            "scope",
            f"Side {leg.side.value} not allowed by token.",
            allowed_sides=[s.value for s in scope.sides] if isinstance(scope.sides, list) else scope.sides,
            requested_side=leg.side.value,
        )

    # Actions
    if scope.actions != "any" and leg.action not in scope.actions:
        return GateResult.reject(
            "scope",
            f"Action {leg.action.value} not allowed by token.",
            allowed_actions=[a.value for a in scope.actions] if isinstance(scope.actions, list) else scope.actions,
            requested_action=leg.action.value,
        )

    # Per-action notional cap
    if leg.notional_usd > scope.max_notional:
        return GateResult.reject(
            "scope",
            f"Notional ${leg.notional_usd} exceeds token cap ${scope.max_notional}.",
            requested_notional=str(leg.notional_usd),
            max_notional=str(scope.max_notional),
        )

    return GateResult.ok("scope")


def check_daily_cap(
    leg: ProposalLegSummary,
    token: AuthorizationToken,
    activity: DailyActivity,
) -> GateResult:
    """Verify this trade + already-traded volume today won't exceed the
    token's daily cap."""
    if activity.token_id != str(token.id):
        # Programming error — caller passed wrong activity record
        return GateResult.reject(
            "daily_cap",
            "Activity record token_id does not match token.id (programming error).",
        )
    projected = activity.volume_today_usd + leg.notional_usd
    if projected > token.scope.max_daily:
        return GateResult.reject(
            "daily_cap",
            f"Trade would push today's volume to ${projected}, "
            f"exceeding token's daily cap ${token.scope.max_daily}.",
            volume_today=str(activity.volume_today_usd),
            requested_notional=str(leg.notional_usd),
            projected_total=str(projected),
            max_daily=str(token.scope.max_daily),
        )
    return GateResult.ok("daily_cap")


def check_circuit_breakers(
    leg: ProposalLegSummary,
    portfolio: PortfolioSnapshot,
    config: CircuitBreakerConfig,
    total_volume_today_usd: Decimal,
) -> GateResult:
    """Operator-config hard caps that supersede any token. Cannot be
    overridden by AI."""
    # Global daily volume across all tokens
    if config.max_daily_volume_usd is not None:
        projected = total_volume_today_usd + leg.notional_usd
        if projected > config.max_daily_volume_usd:
            return GateResult.reject(
                "circuit_breaker",
                f"Account would exceed daily volume cap ${config.max_daily_volume_usd}.",
                volume_today=str(total_volume_today_usd),
                requested_notional=str(leg.notional_usd),
                max_daily_volume=str(config.max_daily_volume_usd),
            )

    # Max open positions (block opens, allow closes)
    if config.max_open_positions is not None and leg.action == TradeAction.OPEN:
        if portfolio.open_positions >= config.max_open_positions:
            return GateResult.reject(
                "circuit_breaker",
                f"Open position count ({portfolio.open_positions}) at cap ({config.max_open_positions}). "
                f"Close existing positions before opening new ones.",
                open_positions=portfolio.open_positions,
                max_open_positions=config.max_open_positions,
            )

    # Drawdown trigger
    if (config.max_drawdown_pct_today is not None
            and config.starting_equity_today is not None
            and config.starting_equity_today > 0):
        # realized_pnl_today is signed: negative = drawdown
        drawdown_pct = -portfolio.realized_pnl_today / config.starting_equity_today
        if drawdown_pct >= config.max_drawdown_pct_today:
            return GateResult.reject(
                "circuit_breaker",
                f"Daily drawdown {drawdown_pct:.2%} exceeds cap {config.max_drawdown_pct_today:.2%}. "
                f"Trading paused until UTC reset.",
                drawdown_pct=str(drawdown_pct),
                max_drawdown_pct=str(config.max_drawdown_pct_today),
            )

    return GateResult.ok("circuit_breaker")


def check_margin(
    leg: ProposalLegSummary,
    portfolio: PortfolioSnapshot,
    margin_required_usd: Decimal,
) -> GateResult:
    """Account has enough free margin for this trade."""
    if margin_required_usd > portfolio.margin_available:
        return GateResult.reject(
            "margin",
            f"Required margin ${margin_required_usd} exceeds available ${portfolio.margin_available}.",
            required=str(margin_required_usd),
            available=str(portfolio.margin_available),
        )
    return GateResult.ok("margin")


def check_slippage(
    leg: ProposalLegSummary,
    market: MarketSnapshot,
    max_slippage_bps: int,
) -> GateResult:
    """For market orders, verify expected slippage from mid is within limits.
    For limit orders this is a no-op (limit price IS the slippage protection)."""
    # Crude estimate: distance between bid/ask vs mid, in basis points.
    # Real implementation should walk the book by leg.size.
    if market.bid <= 0 or market.ask <= 0 or market.mid_price <= 0:
        return GateResult.reject(
            "slippage",
            "Market snapshot has invalid prices (zero or negative).",
            bid=str(market.bid), ask=str(market.ask), mid=str(market.mid_price),
        )
    if leg.coin != market.coin:
        return GateResult.reject(
            "slippage",
            f"Market snapshot is for {market.coin} but leg is for {leg.coin}.",
        )

    # Naïve half-spread estimate. Production should walk the book.
    half_spread_bps = ((market.ask - market.bid) / market.mid_price) * Decimal("10000") / Decimal("2")
    if half_spread_bps > Decimal(max_slippage_bps):
        return GateResult.reject(
            "slippage",
            f"Expected slippage {half_spread_bps:.1f} bps exceeds limit {max_slippage_bps} bps.",
            expected_bps=str(half_spread_bps),
            max_bps=max_slippage_bps,
        )
    return GateResult.ok("slippage")


# ─── Convenience: run all gates ───────────────────────────────────────

def run_all_gates(
    *,
    leg: ProposalLegSummary,
    token: AuthorizationToken,
    portfolio: PortfolioSnapshot,
    market: MarketSnapshot,
    activity: DailyActivity,
    circuit_config: CircuitBreakerConfig,
    total_volume_today_usd: Decimal,
    margin_required_usd: Decimal,
    max_slippage_bps: int = 50,
    kill_flag_path: Path = DEFAULT_KILL_FLAG_PATH,
) -> list[GateResult]:
    """Run every gate in priority order. Returns the full list of results
    (even after the first failure) so the audit log captures the complete
    picture.

    Caller (execute.py) inspects the list: if any GateResult.passed is False,
    the trade is rejected. The first failing gate's reason is the headline
    rejection reason; the rest provide forensic context.
    """
    return [
        check_kill_switch(flag_path=kill_flag_path),
        check_token_scope(leg, token),
        check_daily_cap(leg, token, activity),
        check_circuit_breakers(leg, portfolio, circuit_config, total_volume_today_usd),
        check_margin(leg, portfolio, margin_required_usd),
        check_slippage(leg, market, max_slippage_bps),
    ]


def first_failure(results: list[GateResult]) -> GateResult | None:
    """Return the first failing gate, or None if all passed."""
    for r in results:
        if not r.passed:
            return r
    return None
