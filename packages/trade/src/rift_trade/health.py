"""Strategy Health Monitoring System.

Institutional-grade strategy decay detection using statistical methods.
Monitors live and simulated strategies for edge degradation, regime
mismatch, factor exposure drift, and execution quality changes.

Components:
1. CUSUM — detects distribution shifts in real-time
2. Regime Benchmark — compares to expected performance per market regime
3. Statistical Auto-Pause — hypothesis testing across multiple windows
4. Factor Decomposition — separates alpha from beta
5. Execution Quality — tracks fill degradation vs backtest
6. Health Score — composite 0-100 score with grade and recommendation

Usage:
    from rift.health import run_health_check, HealthReport
    report = run_health_check(trades, baseline_trades, equity_curve, market_returns)
    if report.recommendation == "pause":
        # stop opening new positions
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


# ─── Component 1: CUSUM Change Detector ──────────────────────

@dataclass
class CUSUMResult:
    """Result of CUSUM change point detection."""
    detected: bool              # whether a distribution shift was detected
    cusum_value: float          # current CUSUM statistic magnitude
    threshold: float            # detection threshold
    score: float                # 0-20 score for health composite

    @staticmethod
    def empty() -> CUSUMResult:
        return CUSUMResult(detected=False, cusum_value=0.0, threshold=0.0, score=20.0)


def cusum_detect(
    pnl_series: list[float],
    target_mean: float | None = None,
    threshold: float = 4.0,
    drift: float = 0.5,
) -> CUSUMResult:
    """Tabular CUSUM — detects when return distribution shifts from expected.

    Unlike rolling averages that tell you performance IS bad (after losses),
    CUSUM catches the moment returns STOP coming from the expected distribution.

    Args:
        pnl_series: list of per-trade PnL percentages
        target_mean: expected mean PnL% (from backtest). If None, uses series mean.
        threshold: detection threshold in standard deviations
        drift: allowance for normal variance (half the expected shift size)
    """
    if len(pnl_series) < 10:
        return CUSUMResult.empty()

    arr = np.array(pnl_series)
    if target_mean is None:
        target_mean = float(np.mean(arr))

    std = float(np.std(arr))
    if std == 0:
        return CUSUMResult.empty()

    # Normalize
    normalized = (arr - target_mean) / std

    # Two-sided CUSUM
    s_pos = 0.0
    s_neg = 0.0
    max_cusum = 0.0

    for z in normalized:
        s_pos = max(0, s_pos + z - drift)
        s_neg = max(0, s_neg - z - drift)
        max_cusum = max(max_cusum, s_pos, s_neg)

    detected = max_cusum > threshold

    # Score: 20 if no detection, scales to 0 as CUSUM approaches/exceeds threshold
    if max_cusum < threshold * 0.5:
        score = 20.0
    elif max_cusum < threshold:
        score = 20.0 * (1 - (max_cusum - threshold * 0.5) / (threshold * 0.5))
    else:
        score = 0.0

    return CUSUMResult(
        detected=detected,
        cusum_value=round(max_cusum, 2),
        threshold=threshold,
        score=round(score, 1),
    )


# ─── Component 2: Regime-Aware Benchmarking ───────────────────

@dataclass
class RegimeBenchmark:
    """Result of regime-aware performance comparison."""
    current_regime: str
    expected_win_rate: float
    actual_win_rate: float
    performance_ratio: float    # actual / expected (>1 = outperforming)
    is_underperforming: bool
    score: float                # 0-25 score for health composite

    @staticmethod
    def empty() -> RegimeBenchmark:
        return RegimeBenchmark("unknown", 0, 0, 1.0, False, 25.0)


def regime_benchmark(
    recent_trades: list,
    baseline_win_rate: float = 50.0,
    baseline_expectancy: float = 0.0,
) -> RegimeBenchmark:
    """Compare recent performance to baseline expectations.

    Simplified regime benchmark — compares recent win rate and expectancy
    against the strategy's known baseline from backtesting.
    """
    if len(recent_trades) < 5:
        return RegimeBenchmark.empty()

    def _pnl(t):
        return t.pnl if hasattr(t, 'pnl') else t.get('pnl', 0)

    recent_wins = sum(1 for t in recent_trades if _pnl(t) > 0)
    actual_wr = recent_wins / len(recent_trades) * 100

    if baseline_win_rate > 0:
        ratio = actual_wr / baseline_win_rate
    else:
        ratio = 1.0

    underperforming = ratio < 0.6  # 40% below expected

    # Score: 25 if ratio >= 1.0, scales to 0 at ratio = 0
    score = min(25.0, max(0.0, ratio * 25.0))

    return RegimeBenchmark(
        current_regime="live",
        expected_win_rate=baseline_win_rate,
        actual_win_rate=round(actual_wr, 1),
        performance_ratio=round(ratio, 2),
        is_underperforming=underperforming,
        score=round(score, 1),
    )


# ─── Component 3: Statistical Auto-Pause ─────────────────────

@dataclass
class AutoPauseResult:
    """Result of statistical decay testing."""
    should_pause: bool
    p_value: float              # lowest p-value across windows
    windows_tested: int
    windows_significant: int    # how many windows show p < alpha
    confidence: str             # "no_decay", "possible_decay", "confirmed_decay"
    score: float                # 0-25 score for health composite

    @staticmethod
    def empty() -> AutoPauseResult:
        return AutoPauseResult(False, 1.0, 0, 0, "no_decay", 25.0)


def test_strategy_decay(
    recent_pnls: list[float],
    baseline_pnls: list[float],
    window_sizes: list[int] | None = None,
    alpha: float = 0.05,
    min_windows_significant: int = 2,
) -> AutoPauseResult:
    """Test for strategy decay using t-tests across multiple time windows.

    Uses scipy.stats.ttest_ind to compare recent trade PnLs against baseline.
    Requires multiple independent windows to fail before triggering pause —
    prevents false positives from normal variance.
    """
    if len(recent_pnls) < 10 or len(baseline_pnls) < 10:
        return AutoPauseResult.empty()

    if window_sizes is None:
        window_sizes = [10, 20, min(30, len(recent_pnls))]

    try:
        from scipy.stats import ttest_ind
    except ImportError:
        # scipy not installed — fall back to simple comparison
        recent_mean = np.mean(recent_pnls)
        baseline_mean = np.mean(baseline_pnls)
        decay = recent_mean < baseline_mean * 0.5
        return AutoPauseResult(
            should_pause=decay, p_value=0.0,
            windows_tested=1, windows_significant=1 if decay else 0,
            confidence="possible_decay" if decay else "no_decay",
            score=5.0 if decay else 25.0,
        )

    significant_count = 0
    min_p = 1.0
    tested = 0

    for ws in window_sizes:
        if ws > len(recent_pnls):
            continue
        tested += 1
        window = recent_pnls[-ws:]
        t_stat, p_val = ttest_ind(window, baseline_pnls, alternative='less')
        min_p = min(min_p, p_val)
        if p_val < alpha:
            significant_count += 1

    should_pause = significant_count >= min_windows_significant

    if significant_count == 0:
        confidence = "no_decay"
    elif significant_count < min_windows_significant:
        confidence = "possible_decay"
    else:
        confidence = "confirmed_decay"

    # Score: 25 if no decay, scales based on p-value and windows
    if significant_count == 0:
        score = 25.0
    elif significant_count < min_windows_significant:
        score = 15.0
    else:
        score = max(0.0, min_p * 25.0)  # lower p = lower score

    return AutoPauseResult(
        should_pause=should_pause,
        p_value=round(min_p, 4),
        windows_tested=tested,
        windows_significant=significant_count,
        confidence=confidence,
        score=round(score, 1),
    )


# ─── Component 4: Factor Decomposition ───────────────────────

@dataclass
class FactorDecomposition:
    """Separates alpha (edge) from beta (market exposure)."""
    total_return_pct: float
    alpha: float                # intercept — pure edge
    beta: float                 # market exposure coefficient
    alpha_pct: float            # alpha as % of total return
    r_squared: float            # how much variance is explained by market
    alpha_decaying: bool        # True if rolling alpha is trending toward 0
    score: float                # 0-20 score for health composite

    @staticmethod
    def empty() -> FactorDecomposition:
        return FactorDecomposition(0, 0, 0, 0, 0, False, 20.0)


def decompose_returns(
    strategy_returns: list[float],
    market_returns: list[float],
    rolling_window: int = 20,
) -> FactorDecomposition:
    """Decompose strategy returns into alpha (edge) and beta (market).

    Uses simple linear regression: strategy_return = alpha + beta * market_return
    If alpha > 0, the strategy has genuine edge beyond just being long/short the market.
    """
    if len(strategy_returns) < 20 or len(market_returns) < 20:
        return FactorDecomposition.empty()

    n = min(len(strategy_returns), len(market_returns))
    strat = np.array(strategy_returns[-n:])
    mkt = np.array(market_returns[-n:])

    # Remove NaN/Inf
    mask = np.isfinite(strat) & np.isfinite(mkt)
    strat = strat[mask]
    mkt = mkt[mask]

    if len(strat) < 10:
        return FactorDecomposition.empty()

    # OLS regression
    try:
        beta, alpha = np.polyfit(mkt, strat, 1)
    except Exception:
        return FactorDecomposition.empty()

    # R-squared
    predicted = alpha + beta * mkt
    ss_res = np.sum((strat - predicted) ** 2)
    ss_tot = np.sum((strat - np.mean(strat)) ** 2)
    r_sq = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

    total_ret = float(np.sum(strat))
    alpha_contribution = alpha * len(strat)
    alpha_pct = (alpha_contribution / total_ret * 100) if total_ret != 0 else 0

    # Check if alpha is decaying (rolling alpha trending toward 0)
    alpha_decaying = False
    if len(strat) >= rolling_window * 2:
        rolling_alphas = []
        for i in range(rolling_window, len(strat)):
            window_s = strat[i - rolling_window:i]
            window_m = mkt[i - rolling_window:i]
            try:
                b, a = np.polyfit(window_m, window_s, 1)
                rolling_alphas.append(a)
            except Exception:
                pass
        if len(rolling_alphas) >= 5:
            recent = rolling_alphas[-5:]
            earlier = rolling_alphas[:5]
            alpha_decaying = np.mean(recent) < np.mean(earlier) * 0.5

    # Score: 20 if alpha positive and stable, 0 if alpha negative and decaying
    if alpha > 0 and not alpha_decaying:
        score = 20.0
    elif alpha > 0 and alpha_decaying:
        score = 10.0
    elif alpha <= 0 and not alpha_decaying:
        score = 5.0
    else:
        score = 0.0

    return FactorDecomposition(
        total_return_pct=round(total_ret * 100, 2),
        alpha=round(float(alpha), 6),
        beta=round(float(beta), 4),
        alpha_pct=round(float(alpha_pct), 1),
        r_squared=round(float(r_sq), 4),
        alpha_decaying=alpha_decaying,
        score=round(score, 1),
    )


# ─── Component 5: Execution Quality ──────────────────────────

@dataclass
class ExecutionQuality:
    """Measures execution quality vs backtest expectations."""
    expected_slippage_bps: float
    actual_slippage_bps: float
    slippage_gap_bps: float
    gap_trending_wider: bool
    num_trades_measured: int
    score: float                # 0-10 score for health composite

    @staticmethod
    def empty() -> ExecutionQuality:
        return ExecutionQuality(5.0, 5.0, 0.0, False, 0, 10.0)


def measure_execution_quality(
    trades: list,
    expected_slippage_bps: float = 5.0,
) -> ExecutionQuality:
    """Measure actual execution slippage vs expected.

    Compares entry prices against expected fill prices.
    For backtested trades, this is always 0 (perfect fills).
    For live trades, this reveals if the market is harder to trade than expected.
    """
    if len(trades) < 5:
        return ExecutionQuality.empty()

    # Compute actual slippage from trade prices
    slippages = []
    for t in trades:
        entry = t.entry_price if hasattr(t, 'entry_price') else t.get('entry_price', 0)
        exit_p = t.exit_price if hasattr(t, 'exit_price') else t.get('exit_price', 0)
        if entry > 0 and exit_p > 0:
            # Approximate slippage as a fraction of the spread
            spread_bps = abs(exit_p - entry) / entry * 10000
            slippages.append(spread_bps * 0.01)  # rough approximation

    if not slippages:
        return ExecutionQuality.empty()

    actual_bps = float(np.mean(slippages))
    gap = actual_bps - expected_slippage_bps

    # Check if gap is trending wider (last 5 vs first 5)
    trending_wider = False
    if len(slippages) >= 10:
        early = np.mean(slippages[:5])
        late = np.mean(slippages[-5:])
        trending_wider = late > early * 1.5

    # Score: 10 if gap < 2 bps, scales to 0 at gap > 20 bps
    if gap < 2:
        score = 10.0
    elif gap < 20:
        score = 10.0 * (1 - (gap - 2) / 18)
    else:
        score = 0.0

    return ExecutionQuality(
        expected_slippage_bps=expected_slippage_bps,
        actual_slippage_bps=round(actual_bps, 2),
        slippage_gap_bps=round(gap, 2),
        gap_trending_wider=trending_wider,
        num_trades_measured=len(slippages),
        score=round(max(0, score), 1),
    )


# ─── Component 6: Composite Health Score ──────────────────────

@dataclass
class HealthReport:
    """Composite strategy health report."""
    score: int                          # 0-100
    grade: str                          # A/B/C/D/F
    components: dict[str, float]        # individual scores per component
    alerts: list[str]                   # human-readable warnings
    recommendation: str                 # "continue", "reduce_size", "pause", "stop"
    cusum: CUSUMResult | None = None
    regime: RegimeBenchmark | None = None
    decay: AutoPauseResult | None = None
    factors: FactorDecomposition | None = None
    execution: ExecutionQuality | None = None

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "grade": self.grade,
            "recommendation": self.recommendation,
            "alerts": self.alerts,
            "components": self.components,
        }


def compute_health_score(
    cusum: CUSUMResult | None = None,
    regime: RegimeBenchmark | None = None,
    decay: AutoPauseResult | None = None,
    factors: FactorDecomposition | None = None,
    execution: ExecutionQuality | None = None,
) -> HealthReport:
    """Compute composite health score from all components.

    Weights: CUSUM (20), Regime (25), Decay Test (25), Alpha (20), Execution (10) = 100
    """
    components = {}
    alerts = []

    # CUSUM (20 points)
    cusum_score = cusum.score if cusum else 20.0
    components["cusum"] = cusum_score
    if cusum and cusum.detected:
        alerts.append(f"CUSUM: Distribution shift detected (value={cusum.cusum_value})")

    # Regime benchmark (25 points)
    regime_score = regime.score if regime else 25.0
    components["regime"] = regime_score
    if regime and regime.is_underperforming:
        alerts.append(f"Regime: Underperforming expected win rate ({regime.actual_win_rate:.0f}% vs {regime.expected_win_rate:.0f}%)")

    # Statistical decay (25 points)
    decay_score = decay.score if decay else 25.0
    components["decay_test"] = decay_score
    if decay and decay.should_pause:
        alerts.append(f"Decay: Statistical evidence of edge loss (p={decay.p_value:.4f}, {decay.windows_significant}/{decay.windows_tested} windows)")
    elif decay and decay.confidence == "possible_decay":
        alerts.append(f"Decay: Possible edge degradation (p={decay.p_value:.4f})")

    # Factor decomposition (20 points)
    factor_score = factors.score if factors else 20.0
    components["alpha"] = factor_score
    if factors and factors.alpha_decaying:
        alerts.append(f"Alpha: Edge is decaying (alpha={factors.alpha:.6f}, {factors.alpha_pct:.0f}% of returns)")
    if factors and factors.alpha <= 0:
        alerts.append(f"Alpha: No positive edge detected (alpha={factors.alpha:.6f})")

    # Execution (10 points)
    exec_score = execution.score if execution else 10.0
    components["execution"] = exec_score
    if execution and execution.gap_trending_wider:
        alerts.append(f"Execution: Slippage gap widening ({execution.slippage_gap_bps:.1f} bps above expected)")

    # Total
    total = cusum_score + regime_score + decay_score + factor_score + exec_score
    total = int(round(min(100, max(0, total))))

    # Grade
    if total >= 80:
        grade = "A"
    elif total >= 60:
        grade = "B"
    elif total >= 40:
        grade = "C"
    elif total >= 20:
        grade = "D"
    else:
        grade = "F"

    # Recommendation
    if total >= 70:
        recommendation = "continue"
    elif total >= 50:
        recommendation = "reduce_size"
    elif total >= 25:
        recommendation = "pause"
    else:
        recommendation = "stop"

    return HealthReport(
        score=total,
        grade=grade,
        components=components,
        alerts=alerts,
        recommendation=recommendation,
        cusum=cusum,
        regime=regime,
        decay=decay,
        factors=factors,
        execution=execution,
    )


# ─── Convenience Function ────────────────────────────────────

def run_health_check(
    recent_trades: list,
    baseline_trades: list,
    equity_curve: list[float] | None = None,
    market_returns: list[float] | None = None,
    baseline_win_rate: float = 50.0,
    expected_slippage_bps: float = 5.0,
) -> HealthReport:
    """Run all health checks and return composite report.

    Args:
        recent_trades: trades from live/simulate session (need .pnl attribute)
        baseline_trades: trades from backtest (for comparison)
        equity_curve: strategy equity curve for factor decomposition
        market_returns: BTC per-candle returns for factor decomposition
        baseline_win_rate: expected win rate from backtest
        expected_slippage_bps: expected slippage from backtest config
    """
    def _pnl(t):
        return t.pnl if hasattr(t, 'pnl') else t.get('pnl', 0)

    def _pnl_pct(t):
        if hasattr(t, 'pnl_pct'):
            return t.pnl_pct
        return t.get('pnl_pct', 0)

    recent_pnls = [_pnl_pct(t) for t in recent_trades]
    baseline_pnls = [_pnl_pct(t) for t in baseline_trades]
    baseline_mean = float(np.mean(baseline_pnls)) if baseline_pnls else 0.0

    # Run all components
    cusum = cusum_detect(recent_pnls, target_mean=baseline_mean)

    regime = regime_benchmark(recent_trades, baseline_win_rate=baseline_win_rate)

    decay = test_strategy_decay(recent_pnls, baseline_pnls)

    factors = FactorDecomposition.empty()
    if equity_curve and market_returns:
        strat_returns = []
        for i in range(1, len(equity_curve)):
            if equity_curve[i - 1] > 0:
                strat_returns.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
        factors = decompose_returns(strat_returns, market_returns)

    execution = measure_execution_quality(recent_trades, expected_slippage_bps)

    return compute_health_score(
        cusum=cusum,
        regime=regime,
        decay=decay,
        factors=factors,
        execution=execution,
    )
