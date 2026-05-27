"""Alpha decay primitives.

Three functions:

  make_forward_returns(prices, horizons)
    Convenience: build a (T, H) matrix of realized forward returns from a
    1D price series. NaNs where t+h would exceed array bounds.

  compute_ic_curve(signal, forward_returns, horizons, ...)
    Compute IC at each horizon. Optionally bootstrap (pair-resampled with
    a stationary block scheme) for 95% CIs on each IC.

  estimate_half_life(curve)
    Fit IC(h) = IC_0 · exp(-h / τ) via log-linear regression on |IC|.
    Returns half-life = τ · ln(2). Uses |IC| so a sign-flipped signal still
    fits (we care about magnitude decay).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from scipy.stats import spearmanr


# ─── Dataclasses ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class AlphaDecayCurve:
    """IC measured at each forward horizon, with optional bootstrap CIs.

    Attributes:
      horizons:        (H,) horizons in periods (e.g., [1, 5, 10, 30])
      ics:             (H,) Information Coefficient at each horizon
      ic_ci_lower:     (H,) lower 2.5% bootstrap CI (NaN if not bootstrapped)
      ic_ci_upper:     (H,) upper 97.5% bootstrap CI (NaN if not bootstrapped)
      method:          "pearson" or "spearman"
      n_observations:  number of (signal, return) pairs per horizon
      n_bootstrap:     number of bootstrap resamples (0 if not done)
    """

    horizons: NDArray
    ics: NDArray
    ic_ci_lower: NDArray
    ic_ci_upper: NDArray
    method: str
    n_observations: int
    n_bootstrap: int = 0

    def summary(self) -> str:
        lines = [
            f"AlphaDecayCurve  ({self.method}, n={self.n_observations}"
            + (f", bootstrap={self.n_bootstrap})" if self.n_bootstrap else ")"),
            "─" * 56,
            f"  {'horizon':>8}  {'IC':>8}   95% CI",
        ]
        for h, ic, lo, hi in zip(
            self.horizons, self.ics, self.ic_ci_lower, self.ic_ci_upper
        ):
            ci_str = (
                f"[{lo:+.4f}, {hi:+.4f}]"
                if np.isfinite(lo) and np.isfinite(hi)
                else ""
            )
            lines.append(f"  {h:>8d}  {ic:>+8.4f}   {ci_str}")
        return "\n".join(lines)


@dataclass(frozen=True)
class HalfLifeFit:
    """Exponential decay fit: IC(h) = IC_0 · exp(-h / τ).

    Attributes:
      half_life:   h at which |IC| drops to half — half_life = τ · ln(2)
                   inf if no decay detected (or growing IC); NaN if fit fails
      tau:         decay time constant in periods (1 / decay_rate)
      ic_initial:  fitted IC_0 (magnitude at h=0)
      r_squared:   fit quality on log-IC space
      n_points:    number of curve points used in fit
    """

    half_life: float
    tau: float
    ic_initial: float
    r_squared: float
    n_points: int

    def summary(self) -> str:
        hl_str = f"{self.half_life:.2f}" if np.isfinite(self.half_life) else "n/a"
        return "\n".join([
            "HalfLifeFit",
            "─" * 56,
            f"  Half-life:      {hl_str} periods",
            f"  τ (decay time): {self.tau:.2f}",
            f"  IC₀ (initial):  {self.ic_initial:+.4f}",
            f"  R² (log-fit):   {self.r_squared:.3f}  (n={self.n_points} points)",
        ])


# ─── Forward returns helper ──────────────────────────────────────────


def make_forward_returns(
    prices: NDArray | list[float],
    horizons: NDArray | list[int],
) -> NDArray:
    """Build a (T, H) matrix of realized forward returns from a 1D price series.

    `result[t, h]` = prices[t + horizons[h]] / prices[t] - 1, or NaN if
    t + horizons[h] exceeds the price array.

    Args:
      prices:   (T,) prices at each period
      horizons: H integer horizons (each ≥ 1)

    Returns:
      (T, H) ndarray of forward returns. NaN where the horizon would
      reach past the end of `prices`.
    """
    p = np.asarray(prices, dtype=np.float64).ravel()
    h = np.asarray(horizons, dtype=np.int64).ravel()
    if h.size == 0:
        return np.empty((p.size, 0), dtype=np.float64)
    if np.any(h < 1):
        raise ValueError("all horizons must be ≥ 1")
    T = p.size
    out = np.full((T, h.size), np.nan, dtype=np.float64)
    for hi, hv in enumerate(h):
        if hv >= T:
            continue
        # forward_returns[t] = p[t+h] / p[t] - 1, for t in [0, T-h)
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = p[hv:] / p[:T - hv]
        out[: T - hv, hi] = ratio - 1.0
    return out


# ─── IC computation ──────────────────────────────────────────────────


def _ic_one_horizon(
    signal: NDArray,
    fwd_return: NDArray,
    method: Literal["pearson", "spearman"],
) -> float:
    """IC for a single horizon. Drops NaN pairs."""
    mask = np.isfinite(signal) & np.isfinite(fwd_return)
    if mask.sum() < 2:
        return float("nan")
    s = signal[mask]
    r = fwd_return[mask]
    if np.std(s) == 0 or np.std(r) == 0:
        return float("nan")
    if method == "spearman":
        rho, _ = spearmanr(s, r)
        return float(rho)
    # pearson
    return float(np.corrcoef(s, r)[0, 1])


def _bootstrap_ic_ci(
    signal: NDArray,
    fwd_return: NDArray,
    method: Literal["pearson", "spearman"],
    n_bootstrap: int,
    avg_block_size: int,
    rng: np.random.Generator,
) -> tuple[float, float]:
    """Stationary block bootstrap of paired (signal, return) indices.

    Returns (2.5%, 97.5%) percentile bounds.
    """
    mask = np.isfinite(signal) & np.isfinite(fwd_return)
    s = signal[mask]
    r = fwd_return[mask]
    n = s.size
    if n < 2:
        return (float("nan"), float("nan"))

    p = 1.0 / max(1, avg_block_size)
    ics = np.empty(n_bootstrap, dtype=np.float64)
    for b in range(n_bootstrap):
        # Build a block-bootstrapped INDEX sequence, then apply to both arrays
        idx = np.empty(n, dtype=np.int64)
        i = 0
        while i < n:
            start = int(rng.integers(0, n))
            block_len = max(1, int(rng.geometric(p)))
            end = min(i + block_len, n)
            take = end - i
            idx[i:end] = (start + np.arange(take)) % n
            i = end
        s_b = s[idx]
        r_b = r[idx]
        if np.std(s_b) == 0 or np.std(r_b) == 0:
            ics[b] = np.nan
            continue
        if method == "spearman":
            rho, _ = spearmanr(s_b, r_b)
            ics[b] = float(rho)
        else:
            ics[b] = float(np.corrcoef(s_b, r_b)[0, 1])

    ics_clean = ics[np.isfinite(ics)]
    if ics_clean.size < 2:
        return (float("nan"), float("nan"))
    return (
        float(np.percentile(ics_clean, 2.5)),
        float(np.percentile(ics_clean, 97.5)),
    )


def compute_ic_curve(
    signal: NDArray | list[float],
    forward_returns: NDArray,
    horizons: NDArray | list[int],
    method: Literal["pearson", "spearman"] = "spearman",
    n_bootstrap: int = 0,
    avg_block_size: int | None = None,
    seed: int | None = None,
) -> AlphaDecayCurve:
    """Compute IC at each forward horizon.

    Args:
      signal:           (T,) signal/score series
      forward_returns:  (T, H) realized forward returns at each horizon.
                        Construct via `make_forward_returns()` or pass your
                        own (e.g., funding-net returns, log returns, etc.)
      horizons:         (H,) horizons in periods, aligned with the columns
                        of `forward_returns`
      method:           "spearman" (default — robust to outliers) or "pearson"
      n_bootstrap:      number of bootstrap resamples for CIs (0 = skip CIs)
      avg_block_size:   bootstrap block size (default: max(1, sqrt(T)) — a
                        reasonable default; pass explicit value for series
                        with known autocorrelation length)
      seed:             RNG seed for bootstrap reproducibility

    Returns:
      AlphaDecayCurve. NaN ICs where insufficient data or zero-variance.

    Edge cases:
      - n_bootstrap=0: CI arrays are filled with NaN
      - mismatched shapes raise ValueError
    """
    s = np.asarray(signal, dtype=np.float64).ravel()
    fr = np.asarray(forward_returns, dtype=np.float64)
    if fr.ndim == 1:
        fr = fr.reshape(-1, 1)
    h = np.asarray(horizons, dtype=np.int64).ravel()
    if fr.shape[1] != h.size:
        raise ValueError(
            f"forward_returns has {fr.shape[1]} columns but {h.size} horizons given"
        )
    if fr.shape[0] != s.size:
        raise ValueError(
            f"signal length ({s.size}) != forward_returns rows ({fr.shape[0]})"
        )
    if method not in ("pearson", "spearman"):
        raise ValueError(f"method must be 'pearson' or 'spearman'; got {method!r}")

    H = h.size
    ics = np.full(H, np.nan, dtype=np.float64)
    ci_lo = np.full(H, np.nan, dtype=np.float64)
    ci_hi = np.full(H, np.nan, dtype=np.float64)

    if avg_block_size is None:
        avg_block_size = max(1, int(np.sqrt(s.size)))

    rng = np.random.default_rng(seed) if n_bootstrap > 0 else None

    for hi in range(H):
        col = fr[:, hi]
        ics[hi] = _ic_one_horizon(s, col, method)
        if n_bootstrap > 0 and rng is not None:
            lo, hi_ = _bootstrap_ic_ci(
                s, col, method, n_bootstrap, avg_block_size, rng,
            )
            ci_lo[hi] = lo
            ci_hi[hi] = hi_

    return AlphaDecayCurve(
        horizons=h,
        ics=ics,
        ic_ci_lower=ci_lo,
        ic_ci_upper=ci_hi,
        method=method,
        n_observations=int(s.size),
        n_bootstrap=int(n_bootstrap),
    )


# ─── Half-life fitting ───────────────────────────────────────────────


def estimate_half_life(curve: AlphaDecayCurve) -> HalfLifeFit:
    """Fit IC(h) = IC_0 · exp(-h / τ) via OLS on log(|IC|) vs h.

    Uses |IC| to support sign-flipped signals (magnitude decay is what
    matters operationally). If the fit produces a non-positive decay rate
    (IC growing or flat), half_life is +inf and tau is +inf.

    Returns NaN fit if fewer than 2 finite, positive |IC| values exist.
    """
    abs_ics = np.abs(curve.ics)
    horizons = curve.horizons.astype(np.float64)

    mask = np.isfinite(abs_ics) & (abs_ics > 0)
    n_pts = int(mask.sum())
    if n_pts < 2:
        return HalfLifeFit(
            half_life=float("nan"),
            tau=float("nan"),
            ic_initial=float("nan"),
            r_squared=float("nan"),
            n_points=n_pts,
        )

    log_ics = np.log(abs_ics[mask])
    h_used = horizons[mask]

    # If log|IC| has no meaningful variation, the input is essentially constant
    # → no decay (return inf half-life). Threshold chosen so that floating-point
    # noise in OLS doesn't produce a fake huge-but-finite half-life.
    if np.std(log_ics) < 1e-10:
        return HalfLifeFit(
            half_life=float("inf"),
            tau=float("inf"),
            ic_initial=float(np.exp(log_ics.mean())),
            r_squared=float("nan"),
            n_points=n_pts,
        )

    # Fit log|IC| = log(IC_0) - (1/τ) · h
    X = np.column_stack([np.ones_like(h_used), h_used])
    beta, *_ = np.linalg.lstsq(X, log_ics, rcond=None)
    log_ic_0 = float(beta[0])
    slope = float(beta[1])  # -1/τ

    # R² on log-scale
    fitted = X @ beta
    ss_res = float(np.sum((log_ics - fitted) ** 2))
    ss_tot = float(np.sum((log_ics - log_ics.mean()) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    if slope >= 0:
        # IC growing with horizon — no decay
        return HalfLifeFit(
            half_life=float("inf"),
            tau=float("inf"),
            ic_initial=float(np.exp(log_ic_0)),
            r_squared=r_squared,
            n_points=n_pts,
        )

    tau = -1.0 / slope
    half_life = tau * np.log(2.0)
    return HalfLifeFit(
        half_life=float(half_life),
        tau=float(tau),
        ic_initial=float(np.exp(log_ic_0)),
        r_squared=r_squared,
        n_points=n_pts,
    )
