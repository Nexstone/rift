"""Markouts — post-fill price movements at standard time horizons.

A markout at horizon `h` for a fill is the price change from fill_price to
the mid `h` seconds after the fill, sign-adjusted by trade direction so
positive numbers always mean "the fill moved in your favor."

Markouts at t+1s/10s/60s/300s are the canonical post-trade execution-quality
diagnostic at top quant shops. Their use:

  - Detect adverse selection: if your markouts are consistently negative,
    the market was systematically running away from you after fills.
  - Compare execution methods: IOC trades vs TWAP vs limit-then-aggress.
  - Calibrate impact models: realized markouts are observations of impact decay.

Convention used here:
  Markout_h = sign * (P(fill_time + h) - fill_price) / fill_price * 10_000

  where sign = +1 for long, -1 for short.

So a long that fills at $100 and where price moves to $101 at t+60s has
+100 bps at the 60s horizon. A short in the same setup has -100 bps —
the market ran away from them.

Time alignment: forward-fill within the data extent. The price at target_time
is the last observed price with timestamp ≤ target_time, PROVIDED the series
extends at least to target_time. If the data ends before the horizon, that
markout is NaN — we don't extrapolate forward beyond what we observed.

(Forward-fill semantics inside the observed range; strict NaN past it. This is
what a quant expects: if you didn't see what happened by t+60s, you don't get
to claim a markout at that horizon.)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray


DEFAULT_HORIZONS_SECONDS = [1, 10, 60, 300]


@dataclass(frozen=True)
class MarkoutSeries:
    """Per-horizon markouts for a single fill.

    Attributes:
      fill_price:         the price at which the trade filled
      fill_timestamp_ms:  timestamp of the fill (epoch ms)
      side:               "long" or "short" — the trade direction
      horizons_seconds:   list of horizons evaluated (e.g., [1, 10, 60, 300])
      markouts_bps:       same length as horizons; NaN where the price series
                          doesn't reach that horizon
      prices_at_horizon:  the actual prices found at each target time
                          (NaN where no observation existed)
    """

    fill_price: float
    fill_timestamp_ms: int
    side: str
    horizons_seconds: list[int] = field(default_factory=list)
    markouts_bps: list[float] = field(default_factory=list)
    prices_at_horizon: list[float] = field(default_factory=list)

    def at(self, horizon_seconds: int) -> float:
        """Markout at the given horizon, in bps. NaN if horizon not evaluated."""
        for h, m in zip(self.horizons_seconds, self.markouts_bps):
            if h == horizon_seconds:
                return m
        return float("nan")

    def summary(self) -> str:
        rows = [
            f"  {h:>5}s: {m:>+8.2f} bps  (P = ${p:,.6g})"
            for h, m, p in zip(self.horizons_seconds, self.markouts_bps, self.prices_at_horizon)
        ]
        return "\n".join([
            f"Markouts ({self.side}, filled at ${self.fill_price:,.6g})",
            "─" * 50,
            *rows,
        ])


def compute_markouts(
    fill_price: float,
    fill_timestamp_ms: int,
    side: str,
    subsequent_timestamps_ms: NDArray | list[int],
    subsequent_prices: NDArray | list[float],
    horizons_seconds: list[int] | None = None,
) -> MarkoutSeries:
    """Compute markouts at standard horizons for a single fill.

    Args:
      fill_price:                the executed price
      fill_timestamp_ms:         fill timestamp (epoch ms)
      side:                      "long" or "short"
      subsequent_timestamps_ms:  (N,) post-fill timestamps, MONOTONE INCREASING (epoch ms)
      subsequent_prices:         (N,) mid prices aligned with timestamps
      horizons_seconds:          optional override of horizons (default [1, 10, 60, 300])

    Returns:
      `MarkoutSeries` with per-horizon markouts in bps. Sign convention:
      positive = move in trader's favor.

    Edge cases:
      - Empty subsequent series → all NaN
      - Fill price ≤ 0 → all NaN (can't compute relative bps)
      - Horizon further than series extends → NaN at that horizon
      - Multiple prices at the same horizon timestamp → forward-fill picks the last
    """
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short'; got {side!r}")

    horizons = list(horizons_seconds) if horizons_seconds else list(DEFAULT_HORIZONS_SECONDS)

    ts = np.asarray(subsequent_timestamps_ms, dtype=np.int64).ravel()
    px = np.asarray(subsequent_prices, dtype=np.float64).ravel()
    if ts.size != px.size:
        raise ValueError(f"timestamps ({ts.size}) != prices ({px.size})")

    sign = 1.0 if side == "long" else -1.0
    nan_series = [float("nan")] * len(horizons)
    if ts.size == 0 or fill_price <= 0 or not np.isfinite(fill_price):
        return MarkoutSeries(
            fill_price=fill_price,
            fill_timestamp_ms=int(fill_timestamp_ms),
            side=side,
            horizons_seconds=horizons,
            markouts_bps=list(nan_series),
            prices_at_horizon=list(nan_series),
        )

    markouts: list[float] = []
    prices_at: list[float] = []

    last_ts = int(ts[-1])
    for h in horizons:
        target_ts = int(fill_timestamp_ms) + int(h) * 1000
        # Strict: only compute if the data series reaches target_ts.
        # Forward-fill is allowed only inside the observed range.
        if target_ts > last_ts:
            markouts.append(float("nan"))
            prices_at.append(float("nan"))
            continue
        # Rightmost index where ts <= target_ts (forward-fill within range).
        ix = int(np.searchsorted(ts, target_ts, side="right")) - 1
        if ix < 0:
            markouts.append(float("nan"))
            prices_at.append(float("nan"))
            continue
        price_at = float(px[ix])
        if not np.isfinite(price_at):
            markouts.append(float("nan"))
            prices_at.append(float("nan"))
            continue
        markout_bps = sign * (price_at - fill_price) / fill_price * 10_000.0
        markouts.append(float(markout_bps))
        prices_at.append(price_at)

    return MarkoutSeries(
        fill_price=fill_price,
        fill_timestamp_ms=int(fill_timestamp_ms),
        side=side,
        horizons_seconds=horizons,
        markouts_bps=markouts,
        prices_at_horizon=prices_at,
    )
