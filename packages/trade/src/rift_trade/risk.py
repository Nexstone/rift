"""Portfolio Risk Management — exposure monitoring, position gating, correlation tracking.

Prevents hidden concentration, leverage stacking, and correlated drawdowns
across multiple strategies. Sits between strategy signals and order execution.

Flow: Strategy → Signal → Kelly Sizing → Volume Cap → RISK GATE → Order

Components:
1. PortfolioRiskMonitor — tracks net/gross exposure across all positions
2. PositionGate — checks if a proposed position fits within limits
3. CorrelationMonitor — detects when strategies become correlated

Usage:
    from rift.risk import PortfolioRiskMonitor, PositionGate, RiskLimits

    monitor = PortfolioRiskMonitor(RiskLimits())
    gate = PositionGate(monitor)

    # Before placing an order:
    decision = gate.check("trend_follow", "BTC", "long", 5000.0, 78000.0)
    if decision.allowed:
        actual_notional = decision.permitted_notional
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class RiskLimits:
    """Configurable portfolio risk limits."""
    # Institutional defaults: allow leveraged single positions, prevent portfolio stacking
    max_net_exposure_pct: float = 100.0     # max net long or short as % of equity
    max_gross_exposure_pct: float = 150.0   # max total absolute exposure as % of equity
    max_long_exposure_pct: float = 120.0    # max total long as % of equity
    max_short_exposure_pct: float = 120.0   # max total short as % of equity
    max_per_asset_pct: float = 100.0        # max single-asset concentration as % of equity
    correlation_alert_threshold: float = 0.7
    correlation_reduce_threshold: float = 0.85


@dataclass
class PositionRecord:
    """A tracked position for a specific strategy."""
    strategy_name: str
    coin: str
    side: str               # "long" or "short"
    size: float             # asset units
    notional: float         # size * current_price
    entry_price: float


@dataclass
class ExposureSnapshot:
    """Point-in-time portfolio exposure."""
    equity: float
    net_exposure_pct: float       # (long - short) / equity * 100
    gross_exposure_pct: float     # (long + short) / equity * 100
    long_exposure_pct: float
    short_exposure_pct: float
    long_notional: float
    short_notional: float
    per_asset: dict[str, float] = field(default_factory=dict)   # {coin: signed_notional}
    per_strategy: dict[str, float] = field(default_factory=dict)  # {name: signed_notional}


@dataclass
class GateDecision:
    """Result of the position gate check."""
    allowed: bool
    original_notional: float
    permitted_notional: float
    scale_factor: float            # permitted / original (0.0-1.0)
    reason: str
    violations: list[str] = field(default_factory=list)


@dataclass
class CorrelationAlert:
    """Alert when strategies become correlated."""
    strategy_a: str
    strategy_b: str
    correlation: float
    exceeds_threshold: bool
    recommendation: str   # "none", "alert", "reduce_weaker"


# ─── Portfolio Risk Monitor ──────────────────────────────────

class PortfolioRiskMonitor:
    """Tracks total exposure across all active positions."""

    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()
        self._positions: dict[str, PositionRecord] = {}  # keyed by strategy_name
        self._equity: float = 10000.0

    def update_equity(self, equity: float) -> None:
        """Update current portfolio equity."""
        self._equity = equity

    def register_position(self, strategy_name: str, coin: str, side: str, size: float, price: float) -> None:
        """Register a new position for a strategy."""
        notional = size * price
        self._positions[strategy_name] = PositionRecord(
            strategy_name=strategy_name,
            coin=coin,
            side=side,
            size=size,
            notional=notional,
            entry_price=price,
        )

    def clear_position(self, strategy_name: str) -> None:
        """Remove a position when it closes."""
        self._positions.pop(strategy_name, None)

    def update_prices(self, prices: dict[str, float]) -> None:
        """Update notional values with current prices."""
        for pos in self._positions.values():
            if pos.coin in prices:
                pos.notional = pos.size * prices[pos.coin]

    def snapshot(self) -> ExposureSnapshot:
        """Get current portfolio exposure snapshot."""
        long_notional = 0.0
        short_notional = 0.0
        per_asset: dict[str, float] = {}
        per_strategy: dict[str, float] = {}

        for pos in self._positions.values():
            signed = pos.notional if pos.side == "long" else -pos.notional
            per_strategy[pos.strategy_name] = signed

            if pos.coin not in per_asset:
                per_asset[pos.coin] = 0.0
            per_asset[pos.coin] += signed

            if pos.side == "long":
                long_notional += pos.notional
            else:
                short_notional += pos.notional

        equity = max(self._equity, 1.0)
        net = long_notional - short_notional
        gross = long_notional + short_notional

        return ExposureSnapshot(
            equity=equity,
            net_exposure_pct=round(net / equity * 100, 2),
            gross_exposure_pct=round(gross / equity * 100, 2),
            long_exposure_pct=round(long_notional / equity * 100, 2),
            short_exposure_pct=round(short_notional / equity * 100, 2),
            long_notional=long_notional,
            short_notional=short_notional,
            per_asset=per_asset,
            per_strategy=per_strategy,
        )

    def get_headroom(self, side: str, coin: str) -> float:
        """Get maximum additional notional allowed for a given side and coin.

        Returns the dollar amount that can still be deployed without
        breaching any limit.
        """
        snap = self.snapshot()
        equity = max(self._equity, 1.0)
        headrooms = []

        # Net exposure limit
        if side == "long":
            net_limit = equity * self.limits.max_net_exposure_pct / 100
            current_net = snap.long_notional - snap.short_notional
            headrooms.append(net_limit - current_net)
        else:
            net_limit = equity * self.limits.max_net_exposure_pct / 100
            current_net = snap.short_notional - snap.long_notional
            headrooms.append(net_limit - current_net)

        # Gross exposure limit
        gross_limit = equity * self.limits.max_gross_exposure_pct / 100
        current_gross = snap.long_notional + snap.short_notional
        headrooms.append(gross_limit - current_gross)

        # Directional limit
        if side == "long":
            dir_limit = equity * self.limits.max_long_exposure_pct / 100
            headrooms.append(dir_limit - snap.long_notional)
        else:
            dir_limit = equity * self.limits.max_short_exposure_pct / 100
            headrooms.append(dir_limit - snap.short_notional)

        # Per-asset concentration
        asset_limit = equity * self.limits.max_per_asset_pct / 100
        current_asset = abs(snap.per_asset.get(coin, 0.0))
        headrooms.append(asset_limit - current_asset)

        return max(0.0, min(headrooms))


# ─── Position Gate ────────────────────────────────────────────

class PositionGate:
    """Gate that checks proposed positions against portfolio limits."""

    def __init__(self, monitor: PortfolioRiskMonitor):
        self.monitor = monitor

    def check(
        self,
        strategy_name: str,
        coin: str,
        side: str,
        requested_notional: float,
        price: float,
    ) -> GateDecision:
        """Check if a proposed position fits within portfolio limits.

        Returns GateDecision with the maximum allowed notional.
        """
        if requested_notional <= 0:
            return GateDecision(
                allowed=False, original_notional=0, permitted_notional=0,
                scale_factor=0, reason="Zero or negative notional",
            )

        headroom = self.monitor.get_headroom(side, coin)

        if headroom <= 0:
            snap = self.monitor.snapshot()
            violations = []
            equity = max(self.monitor._equity, 1.0)
            if side == "long" and snap.long_exposure_pct >= self.monitor.limits.max_long_exposure_pct:
                violations.append(f"long exposure {snap.long_exposure_pct:.0f}% >= {self.monitor.limits.max_long_exposure_pct:.0f}% limit")
            if side == "short" and snap.short_exposure_pct >= self.monitor.limits.max_short_exposure_pct:
                violations.append(f"short exposure {snap.short_exposure_pct:.0f}% >= {self.monitor.limits.max_short_exposure_pct:.0f}% limit")
            if snap.gross_exposure_pct >= self.monitor.limits.max_gross_exposure_pct:
                violations.append(f"gross exposure {snap.gross_exposure_pct:.0f}% >= {self.monitor.limits.max_gross_exposure_pct:.0f}% limit")
            if not violations:
                violations.append("exposure limit reached")

            return GateDecision(
                allowed=False, original_notional=requested_notional, permitted_notional=0,
                scale_factor=0, reason="; ".join(violations), violations=violations,
            )

        permitted = min(requested_notional, headroom)
        scale = permitted / requested_notional

        reason = "within limits"
        violations = []
        if scale < 1.0:
            reason = f"scaled to {scale:.0%} of requested (headroom: ${headroom:,.0f})"
            violations.append(reason)

        return GateDecision(
            allowed=True,
            original_notional=requested_notional,
            permitted_notional=permitted,
            scale_factor=round(scale, 4),
            reason=reason,
            violations=violations,
        )


# ─── Correlation Monitor ─────────────────────────────────────

class CorrelationMonitor:
    """Tracks per-strategy equity curves and detects correlation."""

    def __init__(self, alert_threshold: float = 0.7, reduce_threshold: float = 0.85, window: int = 48):
        self.alert_threshold = alert_threshold
        self.reduce_threshold = reduce_threshold
        self.window = window
        self._curves: dict[str, list[float]] = {}

    def record(self, strategy_name: str, equity: float) -> None:
        """Record an equity tick for a strategy."""
        if strategy_name not in self._curves:
            self._curves[strategy_name] = []
        self._curves[strategy_name].append(equity)
        # Keep only last 200 ticks
        if len(self._curves[strategy_name]) > 200:
            self._curves[strategy_name] = self._curves[strategy_name][-200:]

    def check_correlation(self) -> list[CorrelationAlert]:
        """Compute pairwise correlations across all tracked strategies."""
        alerts = []
        names = list(self._curves.keys())

        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a_curve = self._curves[names[i]]
                b_curve = self._curves[names[j]]

                # Need at least window data points
                n = min(len(a_curve), len(b_curve), self.window)
                if n < 10:
                    continue

                # Compute returns
                a_arr = np.array(a_curve[-n:])
                b_arr = np.array(b_curve[-n:])

                a_ret = np.diff(a_arr) / a_arr[:-1]
                b_ret = np.diff(b_arr) / b_arr[:-1]

                # Remove NaN/Inf
                mask = np.isfinite(a_ret) & np.isfinite(b_ret)
                a_ret = a_ret[mask]
                b_ret = b_ret[mask]

                if len(a_ret) < 5:
                    continue

                corr = float(np.corrcoef(a_ret, b_ret)[0, 1])

                if np.isnan(corr):
                    continue

                exceeds = abs(corr) > self.alert_threshold
                if abs(corr) > self.reduce_threshold:
                    rec = "reduce_weaker"
                elif exceeds:
                    rec = "alert"
                else:
                    rec = "none"

                alerts.append(CorrelationAlert(
                    strategy_a=names[i],
                    strategy_b=names[j],
                    correlation=round(corr, 4),
                    exceeds_threshold=exceeds,
                    recommendation=rec,
                ))

        return alerts

    def get_allocation_adjustment(self, strategy_sharpes: dict[str, float]) -> dict[str, float]:
        """Get allocation multipliers based on correlation analysis.

        When two strategies are highly correlated (> reduce_threshold),
        the weaker one (lower Sharpe) gets its allocation halved.
        """
        adjustments = {name: 1.0 for name in strategy_sharpes}
        alerts = self.check_correlation()

        for alert in alerts:
            if alert.recommendation == "reduce_weaker":
                sharpe_a = strategy_sharpes.get(alert.strategy_a, 0)
                sharpe_b = strategy_sharpes.get(alert.strategy_b, 0)
                weaker = alert.strategy_a if sharpe_a < sharpe_b else alert.strategy_b
                adjustments[weaker] = min(adjustments[weaker], 0.5)

        return adjustments
