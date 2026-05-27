"""Drawdown control — automatic size reduction as drawdown deepens.

A `DrawdownController` scales position sizes down as the strategy's
drawdown widens. Behaviour is specified by a step schedule:

    schedule = [
        (0.00, 1.00),   # 0% DD → full size
        (0.05, 0.75),   # 5% DD → 75% size
        (0.10, 0.50),   # 10% DD → 50% size
        (0.15, 0.25),   # 15% DD → 25% size
        (0.20, 0.00),   # 20% DD → stop trading
    ]

The controller interpolates linearly between steps for smooth scaling
(can also be configured for hard-step behaviour). The first threshold
must be 0 and scalers must be monotone non-increasing.

This is the operational form of the "circuit breaker" risk control most
quant shops have. It's NOT a substitute for proper sizing (vol-target /
Kelly with shrunk cov) — it sits on top, as a kill-switch / pull-back
layer when realized PnL deviates from expectation.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DrawdownStep:
    """One step in a drawdown-scaling schedule."""

    drawdown_threshold: float   # fraction in [0, 1], e.g., 0.10 = 10% DD
    size_scaler: float          # in [0, 1] — what fraction of base size to use


def default_schedule() -> list[DrawdownStep]:
    """Industry-typical step schedule: 5% / 10% / 15% / 20% thresholds."""
    return [
        DrawdownStep(0.00, 1.00),
        DrawdownStep(0.05, 0.75),
        DrawdownStep(0.10, 0.50),
        DrawdownStep(0.15, 0.25),
        DrawdownStep(0.20, 0.00),
    ]


class DrawdownController:
    """Scales position sizes down as drawdown deepens.

    Construct with an explicit schedule, or call `DrawdownController.default()`
    for the standard 5%/10%/15%/20% step pattern.

    Parameters:
      schedule:      list of `DrawdownStep` (must start at threshold=0).
      interpolate:   linear interpolation between steps for smooth scaling
                     (default True). When False, uses the last threshold
                     ≤ current DD as a hard step.

    Methods:
      size_scaler(current_drawdown) → float in [0, 1]
      is_killed(current_drawdown)   → bool
    """

    def __init__(
        self,
        schedule: list[DrawdownStep] | None = None,
        interpolate: bool = True,
    ):
        sched = list(schedule) if schedule is not None else default_schedule()
        if not sched:
            raise ValueError("schedule must contain at least one DrawdownStep")
        if sched[0].drawdown_threshold != 0:
            raise ValueError(
                f"schedule[0].drawdown_threshold must be 0; got {sched[0].drawdown_threshold}"
            )
        # Validate monotone non-decreasing thresholds, non-increasing scalers
        for prev, cur in zip(sched, sched[1:]):
            if cur.drawdown_threshold <= prev.drawdown_threshold:
                raise ValueError(
                    f"thresholds must be strictly increasing: {prev.drawdown_threshold} → {cur.drawdown_threshold}"
                )
            if cur.size_scaler > prev.size_scaler:
                raise ValueError(
                    f"scalers must be non-increasing: {prev.size_scaler} → {cur.size_scaler}"
                )
        for step in sched:
            if not 0 <= step.size_scaler <= 1:
                raise ValueError(f"size_scaler must be in [0, 1]; got {step.size_scaler}")

        self.schedule = sched
        self.interpolate = interpolate

    @classmethod
    def default(cls) -> "DrawdownController":
        return cls(schedule=default_schedule(), interpolate=True)

    def size_scaler(self, current_drawdown: float) -> float:
        """Return the size multiplier in [0, 1] for the given drawdown.

        `current_drawdown` is the unsigned fraction (e.g., 0.08 = 8% DD).
        Convention: positive = a real drawdown is in progress; 0 = at peak.
        """
        if current_drawdown <= 0:
            return self.schedule[0].size_scaler
        if current_drawdown >= self.schedule[-1].drawdown_threshold:
            return self.schedule[-1].size_scaler

        for prev, cur in zip(self.schedule, self.schedule[1:]):
            if prev.drawdown_threshold <= current_drawdown <= cur.drawdown_threshold:
                if not self.interpolate:
                    return prev.size_scaler
                # Linear interpolation between (prev.dd, prev.scaler) and (cur.dd, cur.scaler)
                if cur.drawdown_threshold == prev.drawdown_threshold:
                    return prev.size_scaler
                t = (current_drawdown - prev.drawdown_threshold) / (
                    cur.drawdown_threshold - prev.drawdown_threshold
                )
                return float(prev.size_scaler + t * (cur.size_scaler - prev.size_scaler))

        return self.schedule[-1].size_scaler  # past the last step

    def is_killed(self, current_drawdown: float) -> bool:
        """Trading should fully stop (scaler == 0)."""
        return self.size_scaler(current_drawdown) <= 1e-9
