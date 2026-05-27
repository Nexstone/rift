"""Hidden Markov Model regime detector.

`HMMRegimeDetector` wraps `hmmlearn.hmm.GaussianHMM` with a clean API and
state management. Three-feature input — log returns, realized volatility,
funding rate — produces a regime classification: `"calm"`, `"volatile"`,
or `"crisis"`.

The HMM is fitted on a training window, then used to classify the latest
candle. Regimes are labeled by sorting the trained states by their volatility
variance — lowest is "calm", highest is "crisis", middle (if any) is "volatile".

`hmmlearn` is imported lazily inside `fit()` so users who never enable regime
detection don't pay for an unused dependency at import time.

Usage:

    from rift_substrate.regime import HMMRegimeDetector

    hmm = HMMRegimeDetector(n_states=3, n_restarts=10, vol_window=24)

    # Periodically retrain on a window of recent data
    hmm.fit(closes[-720:], funding[-720:])

    # Then classify the current regime
    regime = hmm.predict_regime(closes[-720:], funding[-720:])
    if regime == "crisis":
        # stay flat
        ...

The behaviour here is a verbatim port of the inline HMM helpers that used
to live as a string template inside `rift_engine.workbench` — same numerics,
same edge cases, same feature construction.
"""

from __future__ import annotations

import warnings
from typing import Sequence

import numpy as np
from numpy.typing import NDArray


# ─── Module-level pure helpers (testable in isolation) ────────────────


def compute_hmm_features(
    closes: NDArray | Sequence[float],
    funding_rates: NDArray | Sequence[float],
    vol_window: int = 24,
) -> tuple[NDArray, int]:
    """Build the [log_returns, realized_vol, funding] feature matrix.

    Returns `(features, valid_from)` where `valid_from` is the index past
    which the rolling-vol column is fully populated. Callers should slice
    `features[valid_from:]` before fitting or predicting.
    """
    closes_arr = np.asarray(closes, dtype=np.float64)
    funding_arr = np.asarray(funding_rates, dtype=np.float64)
    n = len(closes_arr)

    log_returns = np.zeros(n)
    if n > 1:
        log_returns[1:] = np.log(closes_arr[1:] / closes_arr[:-1])

    realized_vol = np.zeros(n)
    for i in range(vol_window, n):
        realized_vol[i] = np.std(log_returns[i - vol_window + 1 : i + 1])

    features = np.column_stack([log_returns, realized_vol, funding_arr])
    return features, vol_window


def classify_states(model) -> dict[str, int]:
    """Map trained-state indices to regime labels by ascending vol variance.

    Lowest-variance state → `"calm"`. Highest → `"crisis"`. Middle (when present)
    → `"volatile"`. For 2-state models, both `"volatile"` and `"crisis"` point at
    the high-vol state; for 1-state models, all three labels point at it.
    """
    if model is None:
        return {"calm": 0, "volatile": 1, "crisis": 2}

    vol_variances = []
    for i in range(model.n_components):
        cov = model.covars_[i]
        vol_variances.append(float(cov[1]) if cov.ndim == 1 else float(cov[1, 1]))
    sorted_idx = list(np.argsort(vol_variances))

    if model.n_components == 3:
        return {"calm": sorted_idx[0], "volatile": sorted_idx[1], "crisis": sorted_idx[2]}
    elif model.n_components == 2:
        return {"calm": sorted_idx[0], "volatile": sorted_idx[1], "crisis": sorted_idx[1]}
    else:
        return {"calm": sorted_idx[0], "volatile": sorted_idx[-1], "crisis": sorted_idx[-1]}


# ─── Detector class ───────────────────────────────────────────────────


class HMMRegimeDetector:
    """Stateful HMM regime detector composed into a strategy.

    Holds the trained model and state→label mapping. Call `fit()` to (re)train
    on a window of recent data, then `predict_regime()` to classify the most
    recent candle.
    """

    def __init__(
        self,
        n_states: int = 3,
        n_restarts: int = 10,
        vol_window: int = 24,
    ):
        self.n_states = n_states
        self.n_restarts = n_restarts
        self.vol_window = vol_window
        self.model = None
        self.state_labels: dict[str, int] = {"calm": 0, "volatile": 1, "crisis": 2}
        self.trained = False

    def fit(
        self,
        closes: NDArray | Sequence[float],
        funding_rates: NDArray | Sequence[float],
    ) -> bool:
        """Fit the HMM on the provided window. Returns `True` on success.

        Internally tries `n_restarts` random seeds and keeps the model with
        the highest data likelihood. Returns `False` if no restart converged
        or the valid-feature window is too short (< 100 rows after warmup).
        """
        # Deferred import — users without HMM enabled don't pay for hmmlearn at
        # substrate import time.
        from hmmlearn.hmm import GaussianHMM

        features, valid_from = compute_hmm_features(closes, funding_rates, self.vol_window)
        valid_features = features[valid_from:]
        if len(valid_features) < 100:
            return False

        best_model = None
        best_score = -np.inf
        for seed in range(self.n_restarts):
            try:
                candidate = GaussianHMM(
                    n_components=self.n_states,
                    covariance_type="diag",
                    n_iter=200,
                    random_state=seed,
                    tol=1e-4,
                )
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    candidate.fit(valid_features)
                score = candidate.score(valid_features)
                if score > best_score:
                    best_score = score
                    best_model = candidate
            except Exception:
                continue

        if best_model is None:
            return False

        self.model = best_model
        self.state_labels = classify_states(best_model)
        self.trained = True
        return True

    def predict_regime(
        self,
        closes: NDArray | Sequence[float],
        funding_rates: NDArray | Sequence[float],
    ) -> str | None:
        """Classify the most recent candle's regime.

        Returns `"calm"`, `"volatile"`, `"crisis"`, or `None` if the model is
        untrained or the input is too short to evaluate.
        """
        if self.model is None:
            return None

        features, valid_from = compute_hmm_features(closes, funding_rates, self.vol_window)
        valid_features = features[valid_from:]
        if len(valid_features) < 10:
            return None

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                probs = self.model.predict_proba(valid_features)
            current_probs = probs[-1]
            p_crisis = float(current_probs[self.state_labels["crisis"]])
            p_volatile = float(current_probs[self.state_labels["volatile"]])
            p_calm = float(current_probs[self.state_labels["calm"]])
            if p_crisis > 0.5:
                return "crisis"
            elif p_volatile > p_calm:
                return "volatile"
            else:
                return "calm"
        except Exception:
            return None
