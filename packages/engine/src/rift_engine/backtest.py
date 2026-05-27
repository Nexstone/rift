"""Vectorized backtesting engine."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field

import numpy as np
import polars as pl

from rift_engine.strategy import Candle, Indicator, Signal, Strategy, StrategyState


@dataclass
class Trade:
    """A completed trade."""

    entry_time: int
    exit_time: int
    side: str
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    pnl_pct: float
    exit_reason: str = ""                                # "stop_loss", "signal", "max_hold", "end"
    indicators_at_entry: dict[str, float] | None = None
    indicators_at_exit: dict[str, float] | None = None


@dataclass
class BacktestResult:
    """Results from a backtest run."""

    strategy_name: str
    pair: str
    interval: str
    start_time: int
    end_time: int
    initial_equity: float
    final_equity: float
    total_return_pct: float
    num_trades: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    profit_factor: float
    total_funding: float = 0.0
    # Advanced metrics
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    expectancy: float = 0.0         # expected $ per trade
    payoff_ratio: float = 0.0       # avg_win / avg_loss
    recovery_factor: float = 0.0    # total return / max drawdown
    max_consec_wins: int = 0
    max_consec_losses: int = 0
    avg_trade_duration: float = 0.0  # in candles
    long_win_rate: float = 0.0
    short_win_rate: float = 0.0
    # Drawdown recovery
    max_drawdown_duration_candles: int = 0
    avg_recovery_candles: float = 0.0
    # Institutional validation
    deflated_sharpe: float = 0.0          # Sharpe adjusted for number of trials
    outlier_return_pct: float = 0.0       # return with top 5 trades removed
    outlier_sharpe: float = 0.0           # Sharpe with top 5 trades removed
    num_trials: int = 17                  # total strategies tested (including deleted)
    outlier_dependent: bool = False       # True if top 5 trades drive >50% of returns
    equity_curve: list[float] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    # Monthly performance breakdown — key: "YYYY-MM", value: return % for that month
    monthly_returns: dict[str, float] = field(default_factory=dict)
    # Strategy diagnostics — signal counts, stop rates, blocked entries
    diagnostics: dict = field(default_factory=dict)
    # Per-regime performance breakdown (bull/bear/sideways)
    regime_performance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy_name,
            "pair": self.pair,
            "interval": self.interval,
            "initial_equity": self.initial_equity,
            "final_equity": round(self.final_equity, 2),
            "total_return_pct": round(self.total_return_pct, 2),
            "num_trades": self.num_trades,
            "win_rate": round(self.win_rate, 2),
            "avg_win_pct": round(self.avg_win_pct, 2),
            "avg_loss_pct": round(self.avg_loss_pct, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "sortino_ratio": round(self.sortino_ratio, 4),
            "calmar_ratio": round(self.calmar_ratio, 4),
            "profit_factor": round(self.profit_factor, 2),
            "expectancy": round(self.expectancy, 2),
            "payoff_ratio": round(self.payoff_ratio, 2),
            "recovery_factor": round(self.recovery_factor, 2),
            "max_consec_wins": self.max_consec_wins,
            "max_consec_losses": self.max_consec_losses,
            "avg_trade_duration": round(self.avg_trade_duration, 1),
            "long_win_rate": round(self.long_win_rate, 2),
            "short_win_rate": round(self.short_win_rate, 2),
            "total_funding": round(self.total_funding, 2),
            "max_drawdown_duration_candles": self.max_drawdown_duration_candles,
            "avg_recovery_candles": round(self.avg_recovery_candles, 1),
            "deflated_sharpe": round(self.deflated_sharpe, 4),
            "outlier_return_pct": round(self.outlier_return_pct, 2),
            "outlier_sharpe": round(self.outlier_sharpe, 4),
            "num_trials": self.num_trials,
            "outlier_dependent": self.outlier_dependent,
            "monthly_returns": {k: round(v, 2) for k, v in self.monthly_returns.items()},
            "diagnostics": self.diagnostics,
            "regime_performance": self.regime_performance,
        }

    def to_export_dict(self) -> dict:
        """Full export including trades and equity curve."""
        d = self.to_dict()
        d["trades"] = [
            {
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "side": t.side,
                "entry_price": round(t.entry_price, 2),
                "exit_price": round(t.exit_price, 2),
                "size": t.size,
                "pnl": round(t.pnl, 2),
                "pnl_pct": round(t.pnl_pct, 2),
                "exit_reason": t.exit_reason,
                "indicators_at_entry": t.indicators_at_entry,
                "indicators_at_exit": t.indicators_at_exit,
            }
            for t in self.trades
        ]
        d["equity_curve"] = [round(e, 2) for e in self.equity_curve]
        return d


def _compute_regime_performance(trades: list[Trade], closes: np.ndarray, timestamps: np.ndarray) -> dict:
    """Bucket trades by market regime (bull/bear/sideways) and compute per-regime stats."""
    if not trades or len(closes) < 100:
        return {}

    n = len(closes)
    lookback = min(720, n // 4)  # ~30 days on 1h, adaptive for shorter data
    if lookback < 10:
        return {}

    regime_trades: dict[str, list] = {"bull": [], "bear": [], "sideways": []}
    for t in trades:
        idx = int(np.searchsorted(timestamps, t.entry_time))
        if idx >= lookback and idx < n:
            ret = (closes[idx] - closes[idx - lookback]) / closes[idx - lookback] if closes[idx - lookback] > 0 else 0
            if ret > 0.05:
                regime_trades["bull"].append(t)
            elif ret < -0.05:
                regime_trades["bear"].append(t)
            else:
                regime_trades["sideways"].append(t)

    result = {}
    for regime, rtrades in regime_trades.items():
        if not rtrades:
            result[regime] = {"trades": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0, "total_pnl_pct": 0.0}
            continue
        wins = sum(1 for t in rtrades if t.pnl > 0)
        pnls = [t.pnl_pct for t in rtrades]
        result[regime] = {
            "trades": len(rtrades),
            "win_rate": round(wins / len(rtrades) * 100, 1),
            "avg_pnl_pct": round(float(np.mean(pnls)), 2),
            "total_pnl_pct": round(float(np.sum(pnls)), 2),
        }
    return result


def _periods_per_year(interval: str) -> float:
    """Convert candle interval string to number of periods per year for annualization."""
    mapping = {
        "1m": 365 * 24 * 60,
        "3m": 365 * 24 * 20,
        "5m": 365 * 24 * 12,
        "15m": 365 * 24 * 4,
        "30m": 365 * 24 * 2,
        "1h": 365 * 24,
        "2h": 365 * 12,
        "4h": 365 * 6,
        "8h": 365 * 3,
        "12h": 365 * 2,
        "1d": 365,
        "3d": 365 / 3,
        "1w": 52,
        "1M": 12,
    }
    return mapping.get(interval, 365 * 24)  # default to hourly


def _compute_indicator(name: str, indicator: Indicator, close: np.ndarray, high: np.ndarray, low: np.ndarray, volumes: np.ndarray | None = None, timestamps: np.ndarray | None = None, interval: str = "1h", buy_volumes: np.ndarray | None = None, sell_volumes: np.ndarray | None = None, gt_taker_ratio: np.ndarray | None = None, gt_net_flow: np.ndarray | None = None, gt_total_pnl: np.ndarray | None = None) -> np.ndarray:
    """Compute a single indicator series."""
    params = indicator.params
    n = len(close)

    if indicator.name == "ema":
        period = params["period"]
        ema = np.full(n, np.nan)
        if n >= period:
            ema[period - 1] = np.mean(close[:period])
            alpha = 2.0 / (period + 1)
            for i in range(period, n):
                ema[i] = alpha * close[i] + (1 - alpha) * ema[i - 1]
        return ema

    elif indicator.name == "sma":
        period = params["period"]
        sma = np.full(n, np.nan)
        for i in range(period - 1, n):
            sma[i] = np.mean(close[i - period + 1 : i + 1])
        return sma

    elif indicator.name == "rsi":
        period = params["period"]
        rsi = np.full(n, np.nan)
        if n > period:
            deltas = np.diff(close)
            gains = np.where(deltas > 0, deltas, 0.0)
            losses = np.where(deltas < 0, -deltas, 0.0)
            avg_gain = np.mean(gains[:period])
            avg_loss = np.mean(losses[:period])
            for i in range(period, len(deltas)):
                avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                avg_loss = (avg_loss * (period - 1) + losses[i]) / period
                rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
                rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))
        return rsi

    elif indicator.name == "atr":
        period = params["period"]
        atr = np.full(n, np.nan)
        if n > 1:
            tr = np.maximum(high[1:] - low[1:], np.abs(high[1:] - close[:-1]))
            tr = np.maximum(tr, np.abs(low[1:] - close[:-1]))
            if len(tr) >= period:
                atr[period] = np.mean(tr[:period])
                for i in range(period, len(tr)):
                    atr[i + 1] = (atr[i] * (period - 1) + tr[i]) / period
        return atr

    elif indicator.name == "bbands_upper":
        period = params["period"]
        std_mult = params.get("std", 2.0)
        result = np.full(n, np.nan)
        for i in range(period - 1, n):
            window = close[i - period + 1 : i + 1]
            result[i] = np.mean(window) + std_mult * np.std(window)
        return result

    elif indicator.name == "bbands_lower":
        period = params["period"]
        std_mult = params.get("std", 2.0)
        result = np.full(n, np.nan)
        for i in range(period - 1, n):
            window = close[i - period + 1 : i + 1]
            result[i] = np.mean(window) - std_mult * np.std(window)
        return result

    elif indicator.name == "bbands_width":
        period = params["period"]
        std_mult = params.get("std", 2.0)
        result = np.full(n, np.nan)
        for i in range(period - 1, n):
            window = close[i - period + 1 : i + 1]
            mean = np.mean(window)
            if mean > 0:
                result[i] = (std_mult * np.std(window) * 2) / mean
        return result

    elif indicator.name == "vol_ratio":
        # Volume relative to its moving average
        period = params["period"]
        result = np.full(n, np.nan)
        if volumes is not None and len(volumes) == n:
            for i in range(period - 1, n):
                avg_vol = np.mean(volumes[i - period + 1 : i + 1])
                if avg_vol > 0:
                    result[i] = volumes[i] / avg_vol
        return result

    elif indicator.name == "atr_sma":
        # Rolling average of ATR — measures "normal" volatility
        atr_period = params.get("atr_period", 14)
        avg_period = params["period"]
        # First compute ATR
        atr_vals = np.full(n, np.nan)
        if n > 1:
            tr = np.maximum(high[1:] - low[1:], np.abs(high[1:] - close[:-1]))
            tr = np.maximum(tr, np.abs(low[1:] - close[:-1]))
            if len(tr) >= atr_period:
                atr_vals[atr_period] = np.mean(tr[:atr_period])
                for i in range(atr_period, len(tr)):
                    atr_vals[i + 1] = (atr_vals[i] * (atr_period - 1) + tr[i]) / atr_period
        # Then compute SMA of ATR
        result = np.full(n, np.nan)
        for i in range(avg_period - 1, n):
            window = atr_vals[i - avg_period + 1 : i + 1]
            valid = window[~np.isnan(window)]
            if len(valid) > 0:
                result[i] = np.mean(valid)
        return result

    elif indicator.name == "macd":
        fast = params.get("fast", 12)
        slow = params.get("slow", 26)
        sig = params.get("signal", 9)
        # Compute fast and slow EMAs
        ema_fast = np.full(n, np.nan)
        ema_slow = np.full(n, np.nan)
        if n >= slow:
            ema_fast[fast - 1] = np.mean(close[:fast])
            alpha_f = 2.0 / (fast + 1)
            for i in range(fast, n):
                ema_fast[i] = alpha_f * close[i] + (1 - alpha_f) * ema_fast[i - 1]
            ema_slow[slow - 1] = np.mean(close[:slow])
            alpha_s = 2.0 / (slow + 1)
            for i in range(slow, n):
                ema_slow[i] = alpha_s * close[i] + (1 - alpha_s) * ema_slow[i - 1]
        # MACD line = fast EMA - slow EMA
        macd_line = ema_fast - ema_slow
        return macd_line

    elif indicator.name == "adx":
        # Average Directional Index — measures trend strength (0-100)
        period = params["period"]
        adx = np.full(n, np.nan)
        if n > period + 1:
            # True Range
            tr = np.zeros(n)
            plus_dm = np.zeros(n)
            minus_dm = np.zeros(n)
            for i in range(1, n):
                h_diff = high[i] - high[i-1]
                l_diff = low[i-1] - low[i]
                plus_dm[i] = h_diff if h_diff > l_diff and h_diff > 0 else 0
                minus_dm[i] = l_diff if l_diff > h_diff and l_diff > 0 else 0
                tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))

            # Smoothed TR, +DM, -DM
            atr_s = np.zeros(n)
            plus_dm_s = np.zeros(n)
            minus_dm_s = np.zeros(n)
            atr_s[period] = np.sum(tr[1:period+1])
            plus_dm_s[period] = np.sum(plus_dm[1:period+1])
            minus_dm_s[period] = np.sum(minus_dm[1:period+1])
            for i in range(period+1, n):
                atr_s[i] = atr_s[i-1] - atr_s[i-1]/period + tr[i]
                plus_dm_s[i] = plus_dm_s[i-1] - plus_dm_s[i-1]/period + plus_dm[i]
                minus_dm_s[i] = minus_dm_s[i-1] - minus_dm_s[i-1]/period + minus_dm[i]

            # +DI, -DI
            plus_di = np.zeros(n)
            minus_di = np.zeros(n)
            dx = np.zeros(n)
            for i in range(period, n):
                if atr_s[i] > 0:
                    plus_di[i] = 100 * plus_dm_s[i] / atr_s[i]
                    minus_di[i] = 100 * minus_dm_s[i] / atr_s[i]
                di_sum = plus_di[i] + minus_di[i]
                if di_sum > 0:
                    dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / di_sum

            # ADX = smoothed DX
            adx[2*period] = np.mean(dx[period+1:2*period+1])
            for i in range(2*period+1, n):
                adx[i] = (adx[i-1] * (period-1) + dx[i]) / period
        return adx

    elif indicator.name == "plus_di":
        # +DI component (trend direction — positive = uptrend)
        period = params["period"]
        result = np.full(n, np.nan)
        if n > period + 1:
            tr = np.zeros(n)
            plus_dm = np.zeros(n)
            for i in range(1, n):
                h_diff = high[i] - high[i-1]
                l_diff = low[i-1] - low[i]
                plus_dm[i] = h_diff if h_diff > l_diff and h_diff > 0 else 0
                tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
            atr_s = np.zeros(n)
            plus_dm_s = np.zeros(n)
            atr_s[period] = np.sum(tr[1:period+1])
            plus_dm_s[period] = np.sum(plus_dm[1:period+1])
            for i in range(period+1, n):
                atr_s[i] = atr_s[i-1] - atr_s[i-1]/period + tr[i]
                plus_dm_s[i] = plus_dm_s[i-1] - plus_dm_s[i-1]/period + plus_dm[i]
            for i in range(period, n):
                if atr_s[i] > 0:
                    result[i] = 100 * plus_dm_s[i] / atr_s[i]
        return result

    elif indicator.name == "minus_di":
        # -DI component (trend direction — positive = downtrend)
        period = params["period"]
        result = np.full(n, np.nan)
        if n > period + 1:
            tr = np.zeros(n)
            minus_dm = np.zeros(n)
            for i in range(1, n):
                h_diff = high[i] - high[i-1]
                l_diff = low[i-1] - low[i]
                minus_dm[i] = l_diff if l_diff > h_diff and l_diff > 0 else 0
                tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
            atr_s = np.zeros(n)
            minus_dm_s = np.zeros(n)
            atr_s[period] = np.sum(tr[1:period+1])
            minus_dm_s[period] = np.sum(minus_dm[1:period+1])
            for i in range(period+1, n):
                atr_s[i] = atr_s[i-1] - atr_s[i-1]/period + tr[i]
                minus_dm_s[i] = minus_dm_s[i-1] - minus_dm_s[i-1]/period + minus_dm[i]
            for i in range(period, n):
                if atr_s[i] > 0:
                    result[i] = 100 * minus_dm_s[i] / atr_s[i]
        return result

    elif indicator.name == "swing_high":
        # Recent swing high (highest high in lookback window)
        period = params["period"]
        result = np.full(n, np.nan)
        for i in range(period, n):
            result[i] = np.max(high[i-period:i+1])
        return result

    elif indicator.name == "swing_low":
        # Recent swing low (lowest low in lookback window)
        period = params["period"]
        result = np.full(n, np.nan)
        for i in range(period, n):
            result[i] = np.min(low[i-period:i+1])
        return result

    elif indicator.name == "vwap":
        # Rolling Volume-Weighted Average Price
        period = params["period"]
        result = np.full(n, np.nan)
        if volumes is not None and len(volumes) == n:
            for i in range(period - 1, n):
                window_price = close[i - period + 1 : i + 1]
                window_vol = volumes[i - period + 1 : i + 1]
                total_vol = np.sum(window_vol)
                if total_vol > 0:
                    result[i] = np.sum(window_price * window_vol) / total_vol
                else:
                    result[i] = np.mean(window_price)
        return result

    elif indicator.name == "vwap_std":
        # Rolling std of price-VWAP deviation series.
        # First compute VWAP at each point, then compute rolling std of (close - vwap).
        # This measures "how far does price typically stray from VWAP" — the z-score
        # denominator. Using within-window std dampens z-scores because the current
        # price inflates its own std.
        period = params["period"]
        result = np.full(n, np.nan)
        if volumes is not None and len(volumes) == n:
            # Step 1: compute VWAP at each point
            vwap_series = np.full(n, np.nan)
            for i in range(period - 1, n):
                window_price = close[i - period + 1 : i + 1]
                window_vol = volumes[i - period + 1 : i + 1]
                total_vol = np.sum(window_vol)
                if total_vol > 0:
                    vwap_series[i] = np.sum(window_price * window_vol) / total_vol
            # Step 2: rolling std of deviation series (close - vwap)
            deviations = close - vwap_series
            for i in range(period - 1, n):
                window = deviations[i - period + 1 : i + 1]
                valid = window[~np.isnan(window)]
                if len(valid) > 2:
                    result[i] = np.std(valid)
        return result

    # ─── MOMENTUM ──────────────────────────────────────────────

    elif indicator.name == "macd_signal":
        fast = params.get("fast", 12)
        slow = params.get("slow", 26)
        sig_period = params.get("signal", 9)
        ema_fast = np.full(n, np.nan)
        ema_slow = np.full(n, np.nan)
        if n >= slow:
            ema_fast[fast - 1] = np.mean(close[:fast])
            alpha_f = 2.0 / (fast + 1)
            for i in range(fast, n):
                ema_fast[i] = alpha_f * close[i] + (1 - alpha_f) * ema_fast[i - 1]
            ema_slow[slow - 1] = np.mean(close[:slow])
            alpha_s = 2.0 / (slow + 1)
            for i in range(slow, n):
                ema_slow[i] = alpha_s * close[i] + (1 - alpha_s) * ema_slow[i - 1]
        macd_line = ema_fast - ema_slow
        # Signal line = EMA of MACD line
        result = np.full(n, np.nan)
        start = slow + sig_period - 2
        if n > start:
            valid_macd = macd_line[slow - 1:]
            valid_macd = valid_macd[~np.isnan(valid_macd)]
            if len(valid_macd) >= sig_period:
                sig_val = np.mean(valid_macd[:sig_period])
                result[start] = sig_val
                alpha_sig = 2.0 / (sig_period + 1)
                idx = start + 1
                for v in valid_macd[sig_period:]:
                    sig_val = alpha_sig * v + (1 - alpha_sig) * sig_val
                    if idx < n:
                        result[idx] = sig_val
                    idx += 1
        return result

    elif indicator.name == "macd_histogram":
        fast = params.get("fast", 12)
        slow = params.get("slow", 26)
        sig_period = params.get("signal", 9)
        # Compute MACD and signal, return difference
        from rift_engine.strategy import MACDSignal as _MS
        macd_line = _compute_indicator(name + "_m", Indicator("macd", fast=fast, slow=slow, signal=sig_period), close, high, low, volumes)
        sig_line = _compute_indicator(name + "_s", _MS(fast, slow, sig_period), close, high, low, volumes)
        return macd_line - sig_line

    elif indicator.name == "stoch_k":
        period = params["period"]
        smooth = params.get("smooth", 3)
        raw_k = np.full(n, np.nan)
        for i in range(period - 1, n):
            hh = np.max(high[i - period + 1: i + 1])
            ll = np.min(low[i - period + 1: i + 1])
            if hh != ll:
                raw_k[i] = 100.0 * (close[i] - ll) / (hh - ll)
            else:
                raw_k[i] = 50.0
        # Smooth %K with SMA
        result = np.full(n, np.nan)
        for i in range(period + smooth - 2, n):
            window = raw_k[i - smooth + 1: i + 1]
            valid = window[~np.isnan(window)]
            if len(valid) > 0:
                result[i] = np.mean(valid)
        return result

    elif indicator.name == "stoch_d":
        period = params["period"]
        smooth = params.get("smooth", 3)
        # %D = SMA of %K
        stoch_k = _compute_indicator(name + "_k", Indicator("stoch_k", period=period, smooth=smooth), close, high, low, volumes)
        result = np.full(n, np.nan)
        for i in range(smooth - 1, n):
            window = stoch_k[i - smooth + 1: i + 1]
            valid = window[~np.isnan(window)]
            if len(valid) == smooth:
                result[i] = np.mean(valid)
        return result

    elif indicator.name == "williams_r":
        period = params["period"]
        result = np.full(n, np.nan)
        for i in range(period - 1, n):
            hh = np.max(high[i - period + 1: i + 1])
            ll = np.min(low[i - period + 1: i + 1])
            if hh != ll:
                result[i] = -100.0 * (hh - close[i]) / (hh - ll)
            else:
                result[i] = -50.0
        return result

    elif indicator.name == "cci":
        period = params["period"]
        result = np.full(n, np.nan)
        tp = (high + low + close) / 3.0
        for i in range(period - 1, n):
            window = tp[i - period + 1: i + 1]
            mean_tp = np.mean(window)
            mean_dev = np.mean(np.abs(window - mean_tp))
            if mean_dev > 0:
                result[i] = (tp[i] - mean_tp) / (0.015 * mean_dev)
        return result

    elif indicator.name == "roc":
        period = params["period"]
        result = np.full(n, np.nan)
        for i in range(period, n):
            if close[i - period] != 0:
                result[i] = ((close[i] - close[i - period]) / close[i - period]) * 100.0
        return result

    elif indicator.name == "mfi":
        period = params["period"]
        result = np.full(n, np.nan)
        if volumes is not None and len(volumes) == n:
            tp = (high + low + close) / 3.0
            mf = tp * volumes  # money flow
            for i in range(period, n):
                pos_mf = 0.0
                neg_mf = 0.0
                for j in range(i - period + 1, i + 1):
                    if tp[j] > tp[j - 1]:
                        pos_mf += mf[j]
                    elif tp[j] < tp[j - 1]:
                        neg_mf += mf[j]
                if neg_mf > 0:
                    ratio = pos_mf / neg_mf
                    result[i] = 100.0 - (100.0 / (1.0 + ratio))
                else:
                    result[i] = 100.0
        return result

    # ─── VOLUME ───────────────────────────────────────────────

    elif indicator.name == "obv":
        result = np.full(n, np.nan)
        if volumes is not None and len(volumes) == n:
            result[0] = 0.0
            for i in range(1, n):
                if close[i] > close[i - 1]:
                    result[i] = result[i - 1] + volumes[i]
                elif close[i] < close[i - 1]:
                    result[i] = result[i - 1] - volumes[i]
                else:
                    result[i] = result[i - 1]
        return result

    elif indicator.name == "cmf":
        period = params["period"]
        result = np.full(n, np.nan)
        if volumes is not None and len(volumes) == n:
            # Money Flow Multiplier = ((close - low) - (high - close)) / (high - low)
            mfm = np.zeros(n)
            for i in range(n):
                hl = high[i] - low[i]
                if hl > 0:
                    mfm[i] = ((close[i] - low[i]) - (high[i] - close[i])) / hl
            mfv = mfm * volumes  # Money Flow Volume
            for i in range(period - 1, n):
                vol_sum = np.sum(volumes[i - period + 1: i + 1])
                if vol_sum > 0:
                    result[i] = np.sum(mfv[i - period + 1: i + 1]) / vol_sum
        return result

    # ─── VOLATILITY ───────────────────────────────────────────

    elif indicator.name == "keltner_upper":
        ema_period = params["period"]
        atr_period = params.get("atr_period", 14)
        mult = params.get("mult", 2.0)
        # EMA
        ema_vals = _compute_indicator(name + "_e", Indicator("ema", period=ema_period), close, high, low, volumes)
        # ATR
        atr_vals = _compute_indicator(name + "_a", Indicator("atr", period=atr_period), close, high, low, volumes)
        return ema_vals + mult * np.where(np.isnan(atr_vals), 0, atr_vals)

    elif indicator.name == "keltner_lower":
        ema_period = params["period"]
        atr_period = params.get("atr_period", 14)
        mult = params.get("mult", 2.0)
        ema_vals = _compute_indicator(name + "_e", Indicator("ema", period=ema_period), close, high, low, volumes)
        atr_vals = _compute_indicator(name + "_a", Indicator("atr", period=atr_period), close, high, low, volumes)
        return ema_vals - mult * np.where(np.isnan(atr_vals), 0, atr_vals)

    elif indicator.name == "donchian_upper":
        period = params["period"]
        result = np.full(n, np.nan)
        for i in range(period - 1, n):
            result[i] = np.max(high[i - period + 1: i + 1])
        return result

    elif indicator.name == "donchian_lower":
        period = params["period"]
        result = np.full(n, np.nan)
        for i in range(period - 1, n):
            result[i] = np.min(low[i - period + 1: i + 1])
        return result

    elif indicator.name == "stddev":
        period = params["period"]
        result = np.full(n, np.nan)
        for i in range(period - 1, n):
            result[i] = np.std(close[i - period + 1: i + 1])
        return result

    elif indicator.name == "histvol":
        period = params["period"]
        result = np.full(n, np.nan)
        if n > 1:
            log_returns = np.log(close[1:] / close[:-1])
            for i in range(period, n):
                window = log_returns[i - period: i]
                result[i] = np.std(window) * np.sqrt(365 * 24)  # annualized for hourly
        return result

    # ─── TREND ────────────────────────────────────────────────

    elif indicator.name == "supertrend":
        period = params["period"]
        mult = params.get("mult", 3.0)
        result = np.full(n, np.nan)
        atr_vals = _compute_indicator(name + "_a", Indicator("atr", period=period), close, high, low, volumes)
        if n > period:
            upper_band = np.zeros(n)
            lower_band = np.zeros(n)
            supertrend = np.zeros(n)
            direction = np.ones(n)  # 1 = up, -1 = down
            mid = (high + low) / 2.0
            for i in range(period, n):
                if np.isnan(atr_vals[i]):
                    continue
                upper_band[i] = mid[i] + mult * atr_vals[i]
                lower_band[i] = mid[i] - mult * atr_vals[i]
                if i > period:
                    if lower_band[i] < lower_band[i-1] and close[i-1] > lower_band[i-1]:
                        lower_band[i] = lower_band[i-1]
                    if upper_band[i] > upper_band[i-1] and close[i-1] < upper_band[i-1]:
                        upper_band[i] = upper_band[i-1]
                    if direction[i-1] == 1:
                        direction[i] = -1 if close[i] < lower_band[i] else 1
                    else:
                        direction[i] = 1 if close[i] > upper_band[i] else -1
                supertrend[i] = lower_band[i] if direction[i] == 1 else upper_band[i]
                result[i] = direction[i]  # 1 = uptrend, -1 = downtrend
        return result

    elif indicator.name == "psar":
        af_start = params.get("af_start", 0.02)
        af_step = params.get("af_step", 0.02)
        af_max = params.get("af_max", 0.2)
        result = np.full(n, np.nan)
        if n > 2:
            af = af_start
            uptrend = True
            ep = low[0]
            sar = high[0]
            for i in range(2, n):
                if uptrend:
                    sar = sar + af * (ep - sar)
                    sar = min(sar, low[i-1], low[i-2])
                    if high[i] > ep:
                        ep = high[i]
                        af = min(af + af_step, af_max)
                    if low[i] < sar:
                        uptrend = False
                        sar = ep
                        ep = low[i]
                        af = af_start
                else:
                    sar = sar + af * (ep - sar)
                    sar = max(sar, high[i-1], high[i-2])
                    if low[i] < ep:
                        ep = low[i]
                        af = min(af + af_step, af_max)
                    if high[i] > sar:
                        uptrend = True
                        sar = ep
                        ep = high[i]
                        af = af_start
                result[i] = sar
        return result

    elif indicator.name == "aroon_up":
        period = params["period"]
        result = np.full(n, np.nan)
        for i in range(period, n):
            window = high[i - period: i + 1]
            days_since = period - np.argmax(window)
            result[i] = ((period - days_since) / period) * 100.0
        return result

    elif indicator.name == "aroon_down":
        period = params["period"]
        result = np.full(n, np.nan)
        for i in range(period, n):
            window = low[i - period: i + 1]
            days_since = period - np.argmin(window)
            result[i] = ((period - days_since) / period) * 100.0
        return result

    elif indicator.name == "hma":
        period = params["period"]
        # HMA = WMA(2*WMA(n/2) - WMA(n), sqrt(n))
        half = max(1, period // 2)
        sqrt_p = max(1, int(np.sqrt(period)))
        wma_half = np.full(n, np.nan)
        wma_full = np.full(n, np.nan)
        # WMA helper
        def _wma(data, p):
            out = np.full(len(data), np.nan)
            weights = np.arange(1, p + 1, dtype=float)
            w_sum = weights.sum()
            for i in range(p - 1, len(data)):
                if np.any(np.isnan(data[i - p + 1: i + 1])):
                    continue
                out[i] = np.dot(data[i - p + 1: i + 1], weights) / w_sum
            return out
        wma_half = _wma(close, half)
        wma_full = _wma(close, period)
        diff = 2.0 * wma_half - wma_full
        result = _wma(diff, sqrt_p)
        return result

    elif indicator.name == "dema":
        period = params["period"]
        ema1 = _compute_indicator(name + "_1", Indicator("ema", period=period), close, high, low, volumes)
        ema2 = np.full(n, np.nan)
        if n >= period:
            # EMA of EMA
            valid_start = period - 1
            ema2[valid_start + period - 1] = np.nanmean(ema1[valid_start: valid_start + period])
            alpha = 2.0 / (period + 1)
            for i in range(valid_start + period, n):
                if not np.isnan(ema1[i]) and not np.isnan(ema2[i-1]):
                    ema2[i] = alpha * ema1[i] + (1 - alpha) * ema2[i-1]
        return 2.0 * ema1 - ema2

    elif indicator.name == "tema":
        period = params["period"]
        ema1 = _compute_indicator(name + "_1", Indicator("ema", period=period), close, high, low, volumes)
        # EMA of EMA1
        ema2 = np.full(n, np.nan)
        alpha = 2.0 / (period + 1)
        start2 = 2 * (period - 1)
        if n > start2:
            ema2[start2] = np.nanmean(ema1[period - 1: start2 + 1])
            for i in range(start2 + 1, n):
                if not np.isnan(ema1[i]) and not np.isnan(ema2[i-1]):
                    ema2[i] = alpha * ema1[i] + (1 - alpha) * ema2[i-1]
        # EMA of EMA2
        ema3 = np.full(n, np.nan)
        start3 = 3 * (period - 1)
        if n > start3:
            ema3[start3] = np.nanmean(ema2[start2: start3 + 1])
            for i in range(start3 + 1, n):
                if not np.isnan(ema2[i]) and not np.isnan(ema3[i-1]):
                    ema3[i] = alpha * ema2[i] + (1 - alpha) * ema3[i-1]
        return 3.0 * ema1 - 3.0 * ema2 + ema3

    elif indicator.name == "linreg_slope":
        period = params["period"]
        result = np.full(n, np.nan)
        x = np.arange(period, dtype=float)
        x_mean = np.mean(x)
        x_var = np.sum((x - x_mean) ** 2)
        for i in range(period - 1, n):
            y = close[i - period + 1: i + 1]
            y_mean = np.mean(y)
            result[i] = np.sum((x - x_mean) * (y - y_mean)) / x_var if x_var > 0 else 0
        return result

    elif indicator.name == "ichimoku_tenkan":
        period = params["period"]
        result = np.full(n, np.nan)
        for i in range(period - 1, n):
            result[i] = (np.max(high[i - period + 1: i + 1]) + np.min(low[i - period + 1: i + 1])) / 2.0
        return result

    elif indicator.name == "ichimoku_kijun":
        period = params["period"]
        result = np.full(n, np.nan)
        for i in range(period - 1, n):
            result[i] = (np.max(high[i - period + 1: i + 1]) + np.min(low[i - period + 1: i + 1])) / 2.0
        return result

    elif indicator.name == "ichimoku_senkou_a":
        tenkan_p = params.get("tenkan", 9)
        kijun_p = params.get("kijun", 26)
        tenkan = _compute_indicator(name + "_t", Indicator("ichimoku_tenkan", period=tenkan_p), close, high, low, volumes)
        kijun = _compute_indicator(name + "_k", Indicator("ichimoku_kijun", period=kijun_p), close, high, low, volumes)
        # Senkou A = (Tenkan + Kijun) / 2, shifted forward 26 periods
        raw = (tenkan + kijun) / 2.0
        result = np.full(n, np.nan)
        for i in range(n - kijun_p):
            if not np.isnan(raw[i]):
                result[i + kijun_p] = raw[i]
        return result

    elif indicator.name == "ichimoku_senkou_b":
        period = params["period"]
        kijun_p = 26  # standard displacement
        raw = np.full(n, np.nan)
        for i in range(period - 1, n):
            raw[i] = (np.max(high[i - period + 1: i + 1]) + np.min(low[i - period + 1: i + 1])) / 2.0
        # Shift forward
        result = np.full(n, np.nan)
        for i in range(n - kijun_p):
            if not np.isnan(raw[i]):
                result[i + kijun_p] = raw[i]
        return result

    # ─── STRUCTURE ────────────────────────────────────────────

    elif indicator.name == "pivot_point":
        # Pivot = (prior high + prior low + prior close) / 3
        period = params.get("period", 1)
        result = np.full(n, np.nan)
        for i in range(period, n):
            result[i] = (high[i - period] + low[i - period] + close[i - period]) / 3.0
        return result

    # ─── ADAPTIVE INDICATORS ────────────────────────────────────

    elif indicator.name == "kama":
        period = params["period"]
        fast_sc = 2.0 / (params.get("fast", 2) + 1)
        slow_sc = 2.0 / (params.get("slow", 30) + 1)
        result = np.full(n, np.nan)
        if n > period:
            result[period - 1] = np.mean(close[:period])
            for i in range(period, n):
                direction = abs(close[i] - close[i - period])
                volatility = np.sum(np.abs(np.diff(close[i - period:i + 1])))
                er = direction / volatility if volatility > 0 else 0
                sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
                result[i] = result[i - 1] + sc * (close[i] - result[i - 1])
        return result

    elif indicator.name == "adaptive_ema":
        base_period = params.get("base_period", 20)
        atr_period = params.get("atr_period", 14)
        min_p = params.get("min_period", 5)
        max_p = params.get("max_period", 50)
        atr = _compute_indicator(name + "_atr", Indicator("atr", period=atr_period), close, high, low, volumes)
        result = np.full(n, np.nan)
        warmup = max(base_period, atr_period) + 10
        if n > warmup:
            result[warmup - 1] = np.mean(close[:warmup])
            for i in range(warmup, n):
                if not np.isnan(atr[i]) and not np.isnan(atr[i - 1]):
                    atr_window = atr[max(0, i - 100):i + 1]
                    valid_atr = atr_window[~np.isnan(atr_window)]
                    if len(valid_atr) > 5:
                        pct = np.sum(valid_atr <= atr[i]) / len(valid_atr)
                        period = int(min_p + (1 - pct) * (max_p - min_p))
                    else:
                        period = base_period
                else:
                    period = base_period
                alpha = 2.0 / (period + 1)
                result[i] = alpha * close[i] + (1 - alpha) * result[i - 1]
        return result

    elif indicator.name == "adaptive_rsi":
        base_period = params.get("base_period", 14)
        atr_period = params.get("atr_period", 14)
        min_p = params.get("min_period", 7)
        max_p = params.get("max_period", 28)
        atr = _compute_indicator(name + "_atr", Indicator("atr", period=atr_period), close, high, low, volumes)
        # Compute RSI with variable period
        result = np.full(n, np.nan)
        deltas = np.diff(close, prepend=close[0])
        avg_gain = np.zeros(n)
        avg_loss = np.zeros(n)
        warmup = max(max_p, atr_period) + 10
        if n > warmup:
            avg_gain[warmup - 1] = np.mean(np.maximum(deltas[1:warmup], 0))
            avg_loss[warmup - 1] = np.mean(np.maximum(-deltas[1:warmup], 0))
            for i in range(warmup, n):
                # Determine adaptive period from ATR percentile
                atr_window = atr[max(0, i - 100):i + 1]
                valid_atr = atr_window[~np.isnan(atr_window)]
                if len(valid_atr) > 5 and not np.isnan(atr[i]):
                    pct = np.sum(valid_atr <= atr[i]) / len(valid_atr)
                    period = int(min_p + (1 - pct) * (max_p - min_p))
                else:
                    period = base_period
                alpha = 1.0 / period
                gain = max(deltas[i], 0)
                loss = max(-deltas[i], 0)
                avg_gain[i] = alpha * gain + (1 - alpha) * avg_gain[i - 1]
                avg_loss[i] = alpha * loss + (1 - alpha) * avg_loss[i - 1]
                if avg_loss[i] > 0:
                    rs = avg_gain[i] / avg_loss[i]
                    result[i] = 100 - (100 / (1 + rs))
                else:
                    result[i] = 100.0
        return result

    elif indicator.name == "vol_regime":
        atr_period = params.get("atr_period", 14)
        lookback = params.get("lookback", 100)
        atr = _compute_indicator(name + "_atr", Indicator("atr", period=atr_period), close, high, low, volumes)
        result = np.full(n, np.nan)
        for i in range(lookback, n):
            if np.isnan(atr[i]):
                continue
            window = atr[i - lookback + 1:i + 1]
            valid = window[~np.isnan(window)]
            if len(valid) < 10:
                continue
            pct = np.sum(valid <= atr[i]) / len(valid)
            if pct < 0.25:
                result[i] = 0.0  # low vol
            elif pct > 0.75:
                result[i] = 2.0  # high vol
            else:
                result[i] = 1.0  # normal
        return result

    # ─── MULTI-TIMEFRAME INDICATOR ──────────────────────────────

    elif indicator.name == "htf":
        inner_ind = params["inner"]
        target_tf = params["timeframe"]
        tf_minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
                      "1h": 60, "2h": 120, "4h": 240, "8h": 480, "12h": 720, "1d": 1440}
        # Determine base interval from the run_backtest caller
        # We infer from data density: candles per day
        if n > 48:
            avg_gap = (close[-1] - close[0]) if n < 2 else float(np.median(np.diff(close[:100])))
            # Use timestamps if available via closure
            pass
        base_minutes = tf_minutes.get(interval, 60)
        target_minutes = tf_minutes.get(target_tf, 240)
        ratio = max(1, target_minutes // base_minutes)

        if ratio <= 1:
            return _compute_indicator(name, inner_ind, close, high, low, volumes)

        n_resampled = n // ratio
        if n_resampled < 2:
            return np.full(n, np.nan)

        r_close = np.array([close[(i + 1) * ratio - 1] for i in range(n_resampled)])
        r_high = np.array([np.max(high[i * ratio:(i + 1) * ratio]) for i in range(n_resampled)])
        r_low = np.array([np.min(low[i * ratio:(i + 1) * ratio]) for i in range(n_resampled)])
        r_vol = np.array([np.sum(volumes[i * ratio:(i + 1) * ratio]) for i in range(n_resampled)])

        htf_values = _compute_indicator(name + "_htf", inner_ind, r_close, r_high, r_low, r_vol)

        result = np.full(n, np.nan)
        for i in range(len(htf_values)):
            s = i * ratio
            e = min((i + 1) * ratio, n)
            result[s:e] = htf_values[i]
        return result

    # ─── CROSS-ASSET INDICATORS ─────────────────────────────────

    elif indicator.name == "cross_asset":
        ref_coin = params.get("coin", "")
        ref_field = params.get("field", "close")
        from rift_data.historical import load_candles_smart
        ref_df = load_candles_smart(ref_coin, interval if interval else "1h")
        if ref_df is None or len(ref_df) == 0:
            return np.full(n, np.nan)
        # Align by timestamp — forward-fill to match primary asset
        ref_ts = ref_df["timestamp"].to_numpy()
        ref_vals = ref_df[ref_field].to_numpy().astype(float) if ref_field in ref_df.columns else ref_df["close"].to_numpy().astype(float)
        # For each primary timestamp, find the most recent ref value
        result = np.full(n, np.nan)
        ref_idx = 0
        for i in range(n):
            ts_i = int(timestamps[i]) if timestamps is not None else i
            while ref_idx < len(ref_ts) - 1 and ref_ts[ref_idx + 1] <= ts_i:
                ref_idx += 1
            if ref_idx < len(ref_ts) and ref_ts[ref_idx] <= ts_i:
                result[i] = float(ref_vals[ref_idx])
        return result

    elif indicator.name == "cross_correlation":
        ref_coin = params.get("coin", "")
        period = params.get("period", 24)
        from rift_data.historical import load_candles_smart
        ref_df = load_candles_smart(ref_coin, interval if interval else "1h")
        if ref_df is None or len(ref_df) == 0:
            return np.full(n, np.nan)
        # Load and align reference close prices
        ref_ts = ref_df["timestamp"].to_numpy()
        ref_close = ref_df["close"].to_numpy().astype(float)
        ref_aligned = np.full(n, np.nan)
        ref_idx = 0
        for i in range(n):
            ts_i = int(timestamps[i]) if timestamps is not None else i
            while ref_idx < len(ref_ts) - 1 and ref_ts[ref_idx + 1] <= ts_i:
                ref_idx += 1
            if ref_idx < len(ref_ts) and ref_ts[ref_idx] <= ts_i:
                ref_aligned[i] = float(ref_close[ref_idx])
        # Rolling correlation of returns
        result = np.full(n, np.nan)
        for i in range(period, n):
            a = np.diff(close[i - period:i + 1])
            b = np.diff(ref_aligned[i - period:i + 1])
            valid = ~(np.isnan(a) | np.isnan(b))
            if np.sum(valid) >= period // 2:
                a_v, b_v = a[valid], b[valid]
                if np.std(a_v) > 0 and np.std(b_v) > 0:
                    result[i] = float(np.corrcoef(a_v, b_v)[0, 1])
        return result

    elif indicator.name == "cross_beta":
        ref_coin = params.get("coin", "")
        period = params.get("period", 48)
        from rift_data.historical import load_candles_smart
        ref_df = load_candles_smart(ref_coin, interval if interval else "1h")
        if ref_df is None or len(ref_df) == 0:
            return np.full(n, np.nan)
        ref_ts = ref_df["timestamp"].to_numpy()
        ref_close = ref_df["close"].to_numpy().astype(float)
        ref_aligned = np.full(n, np.nan)
        ref_idx = 0
        for i in range(n):
            ts_i = int(timestamps[i]) if timestamps is not None else i
            while ref_idx < len(ref_ts) - 1 and ref_ts[ref_idx + 1] <= ts_i:
                ref_idx += 1
            if ref_idx < len(ref_ts) and ref_ts[ref_idx] <= ts_i:
                ref_aligned[i] = float(ref_close[ref_idx])
        result = np.full(n, np.nan)
        for i in range(period, n):
            a = np.diff(close[i - period:i + 1])
            b = np.diff(ref_aligned[i - period:i + 1])
            valid = ~(np.isnan(a) | np.isnan(b))
            if np.sum(valid) >= period // 2:
                b_v = b[valid]
                var_b = np.var(b_v)
                if var_b > 0:
                    result[i] = float(np.cov(a[valid], b_v)[0, 1] / var_b)
        return result

    elif indicator.name == "cross_lead_lag":
        ref_coin = params.get("coin", "")
        lag = params.get("lag", 1)
        from rift_data.historical import load_candles_smart
        ref_df = load_candles_smart(ref_coin, interval if interval else "1h")
        if ref_df is None or len(ref_df) == 0:
            return np.full(n, np.nan)
        ref_ts = ref_df["timestamp"].to_numpy()
        ref_close = ref_df["close"].to_numpy().astype(float)
        ref_aligned = np.full(n, np.nan)
        ref_idx = 0
        for i in range(n):
            ts_i = int(timestamps[i]) if timestamps is not None else i
            while ref_idx < len(ref_ts) - 1 and ref_ts[ref_idx + 1] <= ts_i:
                ref_idx += 1
            if ref_idx < len(ref_ts) and ref_ts[ref_idx] <= ts_i:
                ref_aligned[i] = float(ref_close[ref_idx])
        # Lagged return of reference asset
        result = np.full(n, np.nan)
        for i in range(lag + 1, n):
            if not np.isnan(ref_aligned[i - lag]) and not np.isnan(ref_aligned[i - lag - 1]) and ref_aligned[i - lag - 1] > 0:
                result[i] = (ref_aligned[i - lag] - ref_aligned[i - lag - 1]) / ref_aligned[i - lag - 1]
        return result

    # ─── ORDER FLOW INDICATORS (S3 ground-truth) ───────────────

    elif indicator.name == "taker_ratio_ind":
        period = params.get("period", 20)
        if gt_taker_ratio is not None:
            result = np.full(n, np.nan)
            for i in range(period - 1, n):
                result[i] = float(np.mean(gt_taker_ratio[i - period + 1:i + 1]))
            return result
        return np.full(n, np.nan)

    elif indicator.name == "buy_sell_imbalance":
        period = params.get("period", 14)
        if buy_volumes is not None and sell_volumes is not None:
            result = np.full(n, np.nan)
            for i in range(period - 1, n):
                buy_sum = float(np.nansum(buy_volumes[i - period + 1:i + 1]))
                sell_sum = float(np.nansum(sell_volumes[i - period + 1:i + 1]))
                total = buy_sum + sell_sum
                result[i] = (buy_sum - sell_sum) / total if total > 0 else 0
            return result
        return np.full(n, np.nan)

    elif indicator.name == "position_flow":
        period = params.get("period", 20)
        if gt_net_flow is not None:
            result = np.full(n, np.nan)
            for i in range(period - 1, n):
                result[i] = float(np.nansum(gt_net_flow[i - period + 1:i + 1]))
            return result
        return np.full(n, np.nan)

    elif indicator.name == "pnl_flow":
        period = params.get("period", 20)
        if gt_total_pnl is not None:
            result = np.full(n, np.nan)
            for i in range(period - 1, n):
                result[i] = float(np.nansum(gt_total_pnl[i - period + 1:i + 1]))
            return result
        return np.full(n, np.nan)

    elif indicator.name == "trade_intensity":
        period = params.get("period", 10)
        if gt_taker_ratio is not None:
            result = np.full(n, np.nan)
            warmup = min(period, n)
            if warmup > 0:
                valid = gt_taker_ratio[:warmup]
                valid = valid[~np.isnan(valid)]
                if len(valid) > 0:
                    result[warmup - 1] = float(np.mean(valid))
                    alpha = 2.0 / (period + 1)
                    for i in range(warmup, n):
                        if not np.isnan(gt_taker_ratio[i]):
                            result[i] = alpha * gt_taker_ratio[i] + (1 - alpha) * result[i - 1]
                        else:
                            result[i] = result[i - 1]
            return result
        return np.full(n, np.nan)

    else:
        return np.full(n, np.nan)


def run_backtest(
    strategy: Strategy,
    df: pl.DataFrame,
    strategy_name: str = "unknown",
    pair: str = "unknown",
    interval: str = "unknown",
    initial_equity: float = 10000.0,
    fee_rate: float = 0.00034,  # Blended: 70% maker (-0.01%) + 30% taker (0.035%) + 0.03% builder
    leverage: float = 1.0,
    silent: bool = False,
    funding_df: pl.DataFrame | None = None,
    slippage_pct: float = 0.0005,  # 0.05% default slippage
    oi_df: pl.DataFrame | None = None,
    risk_monitor: 'PortfolioRiskMonitor | None' = None,
    # Backtest-mode flags — disable live-trading features that distort validation
    use_kelly: bool = False,       # Kelly sizing: circular in backtest, useful live
    use_confluence: bool = False,  # Confluence sizing: needs rich data, useful live
    use_volume_cap: bool = False,  # Volume cap: prevents market impact, useful live
    use_risk_gate: bool = False,   # Portfolio risk gate: for multi-strategy live only
    use_fractional_sizing: bool = True,  # signal.size = fraction of equity (HF default)
    skip_prepare: bool = False,  # True when caller (walk-forward) handles prepare() externally
) -> BacktestResult:
    """Run a backtest on historical candle data.

    Args:
        strategy: Strategy instance to test
        df: Polars DataFrame with columns: timestamp, open, high, low, close, volume
        strategy_name: Name for reporting
        pair: Trading pair for reporting
        interval: Candle interval for reporting
        initial_equity: Starting equity in USDC
        fee_rate: Trading fee per side
        leverage: Position leverage multiplier
        funding_df: Optional funding rate DataFrame with columns: timestamp, funding_rate
        slippage_pct: Slippage per trade as decimal (0.0005 = 0.05%)
    """
    timestamps = df["timestamp"].to_numpy()
    opens = df["open"].to_numpy().astype(float)
    highs = df["high"].to_numpy().astype(float)
    lows = df["low"].to_numpy().astype(float)
    closes = df["close"].to_numpy().astype(float)
    volumes = df["volume"].to_numpy().astype(float)
    n = len(closes)

    # S3 ground-truth order flow columns (available after rift sync)
    has_order_flow = "buy_volume" in df.columns and "sell_volume" in df.columns
    has_position_flow = "opens_long" in df.columns

    buy_volumes = df["buy_volume"].to_numpy().astype(float) if has_order_flow else None
    sell_volumes = df["sell_volume"].to_numpy().astype(float) if has_order_flow else None
    gt_taker_ratio = df["taker_ratio"].to_numpy().astype(float) if "taker_ratio" in df.columns else None
    gt_total_pnl = df["total_pnl"].to_numpy().astype(float) if "total_pnl" in df.columns else None
    gt_total_fees = df["total_fees"].to_numpy().astype(float) if "total_fees" in df.columns else None

    gt_opens_long = df["opens_long"].to_numpy().astype(float) if has_position_flow else None
    gt_closes_long = df["closes_long"].to_numpy().astype(float) if has_position_flow else None
    gt_opens_short = df["opens_short"].to_numpy().astype(float) if has_position_flow else None
    gt_closes_short = df["closes_short"].to_numpy().astype(float) if has_position_flow else None
    gt_net_flow = df["net_flow"].to_numpy().astype(float) if "net_flow" in df.columns else None

    # Build funding rate lookup: timestamp → rate
    # Funding settles every hour on Hyperliquid
    funding_map: dict[int, float] = {}
    if funding_df is not None and len(funding_df) > 0:
        f_timestamps = funding_df["timestamp"].to_numpy()
        f_rates = funding_df["funding_rate"].to_numpy().astype(float)
        for ft, fr in zip(f_timestamps, f_rates):
            # Round to nearest hour for matching
            hour_ts = int(ft) // 3600000 * 3600000
            funding_map[hour_ts] = float(fr)

    # Pre-compute rolling z-score of funding rates (168-hour / 7-day window)
    funding_zscore_map: dict[int, float] = {}
    # Build predicted funding map: each hour maps to the NEXT hour's actual rate.
    # This simulates the predictedFundings endpoint which shows what funding
    # will be at the next settlement based on the current premium index.
    predicted_funding_map: dict[int, float] = {}
    if funding_map:
        sorted_hours = sorted(funding_map.keys())
        rates_list = [funding_map[h] for h in sorted_hours]
        window = 168  # 7 days of hourly data
        for idx in range(window, len(rates_list)):
            recent = rates_list[idx - window : idx]
            mean = np.mean(recent)
            std = np.std(recent)
            if std > 0:
                zscore = (rates_list[idx] - mean) / std
                funding_zscore_map[sorted_hours[idx]] = float(zscore)
        # Predicted = next hour's actual rate (best historical approximation)
        for idx in range(len(sorted_hours) - 1):
            predicted_funding_map[sorted_hours[idx]] = funding_map[sorted_hours[idx + 1]]

    # Build OI lookup: timestamp → OI value
    # OI data is daily — each day's OI applies to all candles within that day
    oi_map: dict[int, float] = {}
    oi_roc_map: dict[int, float] = {}      # OI rate of change (% change from prior day)
    oi_delta_map: dict[int, float] = {}    # OI absolute change from prior day
    oi_zscore_map: dict[int, float] = {}   # OI z-score vs 30-day rolling window
    if oi_df is not None and len(oi_df) > 0:
        oi_timestamps = oi_df["timestamp"].to_numpy()
        oi_values = oi_df["oi_close"].to_numpy().astype(float)
        sorted_days = []
        for ot, ov in zip(oi_timestamps, oi_values):
            day_ts = int(ot) // 86400000 * 86400000
            oi_map[day_ts] = float(ov)
            sorted_days.append((day_ts, float(ov)))
        sorted_days.sort(key=lambda x: x[0])

        # Compute OI ROC (% change) and delta (absolute change)
        for idx in range(1, len(sorted_days)):
            day_ts = sorted_days[idx][0]
            curr_oi = sorted_days[idx][1]
            prev_oi = sorted_days[idx - 1][1]
            if prev_oi > 0:
                oi_roc_map[day_ts] = ((curr_oi - prev_oi) / prev_oi) * 100.0
            oi_delta_map[day_ts] = curr_oi - prev_oi

        # Compute OI z-score (30-day rolling window)
        oi_vals = [d[1] for d in sorted_days]
        window = 30
        for idx in range(window, len(sorted_days)):
            day_ts = sorted_days[idx][0]
            recent = oi_vals[idx - window: idx]
            mean = np.mean(recent)
            std = np.std(recent)
            if std > 0:
                oi_zscore_map[day_ts] = (oi_vals[idx] - mean) / std

    # Build premium lookup from funding_df (premium column if available)
    premium_map: dict[int, float] = {}
    if funding_df is not None and "premium" in funding_df.columns:
        f_timestamps = funding_df["timestamp"].to_numpy()
        f_premiums = funding_df["premium"].to_numpy().astype(float)
        for ft, fp in zip(f_timestamps, f_premiums):
            hour_ts = int(ft) // 3600000 * 3600000
            premium_map[hour_ts] = float(fp)

    if hasattr(strategy.config, "leverage"):
        leverage = strategy.config.leverage

    # Pre-compute all indicators
    indicator_defs = strategy.indicators()
    indicator_series: dict[str, np.ndarray] = {}
    for ind_name, ind in indicator_defs.items():
        indicator_series[ind_name] = _compute_indicator(ind_name, ind, closes, highs, lows, volumes, timestamps=timestamps, interval=interval, buy_volumes=buy_volumes, sell_volumes=sell_volumes, gt_taker_ratio=gt_taker_ratio, gt_net_flow=gt_net_flow, gt_total_pnl=gt_total_pnl)

    # Pre-processing hook — lets strategies compute on full data before the candle loop
    # Critical for ML strategies (HMM, etc.) that need vectorized bulk operations
    # skip_prepare=True when walk-forward handles prepare() externally with train_df
    if not skip_prepare and hasattr(strategy, 'prepare'):
        strategy.prepare(df, funding_df=funding_df, pair=pair)

    # Simulate
    equity = initial_equity
    position = 0.0  # positive = long, negative = short
    entry_price = 0.0
    entry_time = 0
    total_funding_paid = 0.0
    last_funding_hour = 0  # track last funding settlement
    trailing_stop_price = 0.0  # ratchets with price, never moves backward

    # Pre-compute ATR for trailing stops (14-period ATR)
    atr_series = np.full(n, 0.0)
    if n > 1:
        tr = np.maximum(highs[1:] - lows[1:], np.abs(highs[1:] - closes[:-1]))
        tr = np.maximum(tr, np.abs(lows[1:] - closes[:-1]))
        atr_period = 14
        if len(tr) >= atr_period:
            atr_series[atr_period] = np.mean(tr[:atr_period])
            for j in range(atr_period, len(tr)):
                atr_series[j + 1] = (atr_series[j] * (atr_period - 1) + tr[j]) / atr_period

    # Risk gate (optional — for portfolio-level exposure limits)
    from rift.risk import PortfolioRiskMonitor, PositionGate, RiskLimits
    if use_risk_gate:
        if risk_monitor is None:
            risk_monitor = PortfolioRiskMonitor(RiskLimits())
        risk_monitor.update_equity(initial_equity)
        risk_gate = PositionGate(risk_monitor)

    # Net positioning accumulators (Leviathan method)
    cum_longs_entering = 0.0
    cum_longs_exiting = 0.0
    cum_shorts_entering = 0.0
    cum_shorts_exiting = 0.0
    prev_oi_for_net = 0.0

    # CVD accumulators (Leviathan Volume Suite)
    cumulative_volume_delta = 0.0

    equity_curve = [equity]
    trades: list[Trade] = []
    total_candles = n

    # Diagnostic counters
    diag_signals_long = 0
    diag_signals_short = 0
    diag_signals_close = 0
    diag_signals_none = 0
    diag_stops_fired = 0
    diag_blocked_by_cooldown = 0
    entry_indicators: dict[str, float] | None = None

    def _snap_indicators(idx: int) -> dict[str, float]:
        """Snapshot indicator values at a given candle index."""
        snap = {}
        for name, series in indicator_series.items():
            if idx < len(series) and not np.isnan(series[idx]):
                snap[name] = round(float(series[idx]), 6)
        return snap

    # Load orderbook snapshots if available
    orderbook_map: dict[int, dict] = {}
    if pair and pair != "unknown":
        try:
            from rift_data.data import load_orderbook_snapshots
            ob_coin = pair.replace("-PERP", "").replace("-perp", "").upper()
            ob_data = load_orderbook_snapshots(ob_coin, int(timestamps[0]), int(timestamps[-1]))
            for snap in ob_data:
                ts_5m = (snap["timestamp"] // 300000) * 300000
                orderbook_map[ts_5m] = snap
        except Exception:
            pass

    # Monthly performance tracking
    from datetime import datetime
    monthly_equity: dict[str, float] = {}  # "YYYY-MM" → equity at start of month
    current_month_key = ""

    for i in range(n):
        stop_fired_this_candle = False

        # Get current funding rate and z-score for this candle's hour
        current_hour = int(timestamps[i]) // 3600000 * 3600000
        current_day = int(timestamps[i]) // 86400000 * 86400000
        current_funding_rate = funding_map.get(current_hour, 0.0)
        current_funding_zscore = funding_zscore_map.get(current_hour, 0.0)
        current_predicted_funding = predicted_funding_map.get(current_hour, 0.0)
        current_oi = oi_map.get(current_day, 0.0)
        current_oi_roc = oi_roc_map.get(current_day, 0.0)
        current_oi_delta = oi_delta_map.get(current_day, 0.0)
        current_oi_zscore = oi_zscore_map.get(current_day, 0.0)
        current_premium = premium_map.get(current_hour, 0.0)

        # Net positioning: ground truth from S3 or estimated from OI + price (Leviathan)
        if has_position_flow and gt_opens_long is not None:
            cum_longs_entering += float(gt_opens_long[i]) if not np.isnan(gt_opens_long[i]) else 0.0
            cum_longs_exiting += float(gt_closes_long[i]) if not np.isnan(gt_closes_long[i]) else 0.0
            cum_shorts_entering += float(gt_opens_short[i]) if not np.isnan(gt_opens_short[i]) else 0.0
            cum_shorts_exiting += float(gt_closes_short[i]) if not np.isnan(gt_closes_short[i]) else 0.0
        else:
            if current_oi > 0 and prev_oi_for_net > 0:
                oi_change = current_oi - prev_oi_for_net
                price_up = closes[i] > opens[i]
                price_down = closes[i] < opens[i]
                if oi_change > 0 and price_up:
                    cum_longs_entering += abs(oi_change)
                elif oi_change > 0 and price_down:
                    cum_shorts_entering += abs(oi_change)
                elif oi_change < 0 and price_down:
                    cum_longs_exiting += abs(oi_change)
                elif oi_change < 0 and price_up:
                    cum_shorts_exiting += abs(oi_change)
        if current_oi > 0:
            prev_oi_for_net = current_oi

        current_net_longs = cum_longs_entering - cum_longs_exiting
        current_net_shorts = cum_shorts_entering - cum_shorts_exiting
        current_net_delta = current_net_longs - current_net_shorts

        # Volume delta / CVD: ground truth from S3 or estimated from candle direction
        if has_order_flow and not np.isnan(buy_volumes[i]):
            buy_vol = float(buy_volumes[i])
            sell_vol = float(sell_volumes[i])
        else:
            buy_vol = volumes[i] if closes[i] > opens[i] else 0.0
            sell_vol = volumes[i] if closes[i] < opens[i] else 0.0
        current_vol_delta = buy_vol - sell_vol
        cumulative_volume_delta += current_vol_delta
        # Relative volume: current vs rolling 20-period average
        vol_window = min(i + 1, 20)
        avg_vol = float(np.mean(volumes[max(0, i - vol_window + 1):i + 1])) if vol_window > 0 else 1.0
        current_rvol = volumes[i] / avg_vol if avg_vol > 0 else 1.0

        # Build state for this candle
        # Orderbook microstructure lookup (5-min resolution)
        ob_imbalance = 0.0
        ob_spread_bps = 0.0
        ob_bid_depth = 0.0
        ob_ask_depth = 0.0
        ob_depth_ratio = 0.0
        if orderbook_map:
            ts_5m = (int(timestamps[i]) // 300000) * 300000
            ob_snap = orderbook_map.get(ts_5m)
            if ob_snap:
                bids = ob_snap.get("bids", [])
                asks = ob_snap.get("asks", [])
                # Bids/asks may be dicts {"px": ..., "sz": ...} (HL L2 format) or
                # [price, size] lists (some legacy collectors). Handle both.
                def _bid_sz(b):
                    return float(b.get("sz", 0)) if isinstance(b, dict) else (float(b[1]) if len(b) >= 2 else 0)
                def _bid_px(b):
                    return float(b.get("px", 0)) if isinstance(b, dict) else (float(b[0]) if len(b) >= 1 else 0)
                ob_bid_depth = sum(_bid_sz(b) for b in bids)
                ob_ask_depth = sum(_bid_sz(a) for a in asks)
                total_depth = ob_bid_depth + ob_ask_depth
                ob_imbalance = (ob_bid_depth - ob_ask_depth) / total_depth if total_depth > 0 else 0
                ob_depth_ratio = ob_bid_depth / ob_ask_depth if ob_ask_depth > 0 else 1.0
                best_bid = _bid_px(bids[0]) if bids else 0
                best_ask = _bid_px(asks[0]) if asks else 0
                ob_spread_bps = ((best_ask - best_bid) / best_bid * 10000) if best_bid > 0 else 0

        state = StrategyState(
            indicators={name: float(series[i]) if not np.isnan(series[i]) else float('nan') for name, series in indicator_series.items()},
            position=position,
            equity=equity,
            funding_rate=current_funding_rate,
            funding_rate_zscore=current_funding_zscore,
            cumulative_funding=total_funding_paid,
            predicted_funding=current_predicted_funding,
            open_interest=current_oi,
            oi_roc=current_oi_roc,
            oi_delta=current_oi_delta,
            oi_zscore=current_oi_zscore,
            net_longs=current_net_longs,
            net_shorts=current_net_shorts,
            net_delta=current_net_delta,
            volume_delta=current_vol_delta,
            cvd=cumulative_volume_delta,
            relative_volume=current_rvol,
            premium=current_premium,
            oracle_price=float(closes[i]),
            bid_ask_imbalance=ob_imbalance,
            spread_bps=ob_spread_bps,
            bid_depth=ob_bid_depth,
            ask_depth=ob_ask_depth,
            depth_ratio=ob_depth_ratio,
            buy_volume=buy_vol,
            sell_volume=sell_vol,
            taker_ratio=float(gt_taker_ratio[i]) if gt_taker_ratio is not None and not np.isnan(gt_taker_ratio[i]) else 0.0,
            opens_long=float(gt_opens_long[i]) if gt_opens_long is not None and not np.isnan(gt_opens_long[i]) else 0.0,
            closes_long=float(gt_closes_long[i]) if gt_closes_long is not None and not np.isnan(gt_closes_long[i]) else 0.0,
            opens_short=float(gt_opens_short[i]) if gt_opens_short is not None and not np.isnan(gt_opens_short[i]) else 0.0,
            closes_short=float(gt_closes_short[i]) if gt_closes_short is not None and not np.isnan(gt_closes_short[i]) else 0.0,
            net_flow=float(gt_net_flow[i]) if gt_net_flow is not None and not np.isnan(gt_net_flow[i]) else 0.0,
            candle_pnl=float(gt_total_pnl[i]) if gt_total_pnl is not None and not np.isnan(gt_total_pnl[i]) else 0.0,
            candle_fees=float(gt_total_fees[i]) if gt_total_fees is not None and not np.isnan(gt_total_fees[i]) else 0.0,
        )

        candle = Candle(
            timestamp=int(timestamps[i]),
            open=float(opens[i]),
            high=float(highs[i]),
            low=float(lows[i]),
            close=float(closes[i]),
            volume=float(volumes[i]),
        )

        # Apply funding rate if position is open and we've crossed an hour boundary
        if position != 0.0 and funding_map:
            current_hour = int(timestamps[i]) // 3600000 * 3600000
            if current_hour > last_funding_hour and current_hour in funding_map:
                rate = funding_map[current_hour]
                # Funding payment = position_value * funding_rate
                # Positive rate: longs pay shorts. Negative rate: shorts pay longs.
                position_value = abs(position) * closes[i] * leverage
                if position > 0:
                    funding_payment = -position_value * rate  # longs pay when rate > 0
                else:
                    funding_payment = position_value * rate   # shorts receive when rate > 0
                equity += funding_payment
                total_funding_paid += funding_payment
                last_funding_hour = current_hour

        # Update trailing stop (ATR-based: trail by 2x ATR behind price)
        # Only enabled if strategy config has trailing_stop = True
        use_trailing = hasattr(strategy.config, 'trailing_stop') and strategy.config.trailing_stop
        if use_trailing and position != 0.0 and atr_series[i] > 0:
            trail_mult = strategy.config.trail_atr_mult if hasattr(strategy.config, 'trail_atr_mult') else 2.0
            trail_distance = atr_series[i] * trail_mult
            if position > 0:
                new_trail = highs[i] - trail_distance
                if new_trail > trailing_stop_price:
                    trailing_stop_price = new_trail
            else:
                new_trail = lows[i] + trail_distance
                if trailing_stop_price == 0 or new_trail < trailing_stop_price:
                    trailing_stop_price = new_trail

        # Check stop loss
        if position != 0.0 and hasattr(strategy.config, "stop_loss_pct") and strategy.config.stop_loss_pct:
            sl_pct = strategy.config.stop_loss_pct
            initial_stop = entry_price * (1 - sl_pct) if position > 0 else entry_price * (1 + sl_pct)
            # Use trailing stop only if enabled and tighter than initial
            if use_trailing and trailing_stop_price > 0:
                if position > 0:
                    effective_stop = max(initial_stop, trailing_stop_price)
                else:
                    effective_stop = min(initial_stop, trailing_stop_price)
            else:
                effective_stop = initial_stop

            if position > 0 and lows[i] <= effective_stop:
                exit_price = effective_stop
                pnl = position * (exit_price - entry_price) * leverage - abs(position * exit_price * leverage * fee_rate)
                pnl_pct = pnl / equity * 100
                equity += pnl
                trades.append(Trade(entry_time, int(timestamps[i]), "long", entry_price, exit_price, position, pnl, pnl_pct, exit_reason="stop_loss", indicators_at_entry=entry_indicators, indicators_at_exit=_snap_indicators(i)))
                position = 0.0
                trailing_stop_price = 0.0
                stop_fired_this_candle = True
                diag_stops_fired += 1
                if use_risk_gate:
                    risk_monitor.clear_position(strategy_name)
                    risk_monitor.update_equity(equity)
            elif position < 0 and highs[i] >= effective_stop:
                exit_price = effective_stop
                pnl = abs(position) * (entry_price - exit_price) * leverage - abs(position * exit_price * leverage * fee_rate)
                pnl_pct = pnl / equity * 100
                equity += pnl
                trades.append(Trade(entry_time, int(timestamps[i]), "short", entry_price, exit_price, abs(position), pnl, pnl_pct, exit_reason="stop_loss", indicators_at_entry=entry_indicators, indicators_at_exit=_snap_indicators(i)))
                position = 0.0
                trailing_stop_price = 0.0
                stop_fired_this_candle = True
                diag_stops_fired += 1
                if use_risk_gate:
                    risk_monitor.clear_position(strategy_name)
                    risk_monitor.update_equity(equity)

        # Rebuild state after stop loss so strategy sees correct position
        if state.position != position:
            state = StrategyState(
                indicators=state.indicators,
                position=position,
                equity=equity,
                funding_rate=state.funding_rate,
                funding_rate_zscore=state.funding_rate_zscore,
                cumulative_funding=total_funding_paid,
                predicted_funding=state.predicted_funding,
                open_interest=state.open_interest,
                oi_roc=state.oi_roc,
                oi_delta=state.oi_delta,
                oi_zscore=state.oi_zscore,
                net_longs=state.net_longs,
                net_shorts=state.net_shorts,
                net_delta=state.net_delta,
                volume_delta=state.volume_delta,
                cvd=state.cvd,
                relative_volume=state.relative_volume,
                premium=state.premium,
                oracle_price=state.oracle_price,
            )

        # Get signal from strategy
        signal = strategy.on_candle(candle, state)

        # Diagnostic signal counting
        if signal is None:
            diag_signals_none += 1
        elif signal.reduce_only:
            diag_signals_close += 1
        elif signal.side.value == "long":
            diag_signals_long += 1
        else:
            diag_signals_short += 1

        if signal is not None:
            # Apply slippage: volume-aware — scales with position size vs candle volume
            # Base slippage + market impact component for larger orders
            raw_price = closes[i]
            effective_slippage = slippage_pct
            if use_volume_cap and not signal.reduce_only and signal.size > 0 and volumes is not None and volumes[i] > 0:
                # Estimate notional from current equity and signal
                est_risk = 0.02 * signal.size  # flat estimate for slippage calc
                if use_kelly:
                    from rift_engine.strategy import compute_kelly_risk
                    est_risk = compute_kelly_risk(trades) * signal.size
                est_notional = (equity * min(est_risk, 0.10)) / (signal.stop_loss or 0.02)
                est_notional = min(est_notional, equity * leverage)
                # Market impact: position_notional / candle_volume * impact_factor
                # At 0.1% of volume = negligible, at 10% of volume = significant
                candle_notional = volumes[i] * raw_price
                if candle_notional > 0:
                    volume_ratio = est_notional / candle_notional
                    market_impact = volume_ratio * 0.1  # 10% of volume ratio as additional slippage
                    effective_slippage = slippage_pct + min(market_impact, 0.02)  # cap impact at 2%

            if signal.side.value == "long":
                price = raw_price * (1 + effective_slippage)
            else:
                price = raw_price * (1 - effective_slippage)

            # Close existing position (full or partial)
            if signal.reduce_only or (position > 0 and signal.side.value == "short") or (position < 0 and signal.side.value == "long"):
                if position != 0.0:
                    close_pct = signal.close_pct if hasattr(signal, 'close_pct') else 1.0
                    close_size = abs(position) * close_pct

                    if position > 0:
                        pnl = close_size * (price - entry_price) * leverage - abs(close_size * price * leverage * fee_rate)
                        pnl_pct = pnl / equity * 100
                        trades.append(Trade(entry_time, int(timestamps[i]), "long", entry_price, price, close_size, pnl, pnl_pct, exit_reason="signal", indicators_at_entry=entry_indicators, indicators_at_exit=_snap_indicators(i)))
                    else:
                        pnl = close_size * (entry_price - price) * leverage - abs(close_size * price * leverage * fee_rate)
                        pnl_pct = pnl / equity * 100
                        trades.append(Trade(entry_time, int(timestamps[i]), "short", entry_price, price, close_size, pnl, pnl_pct, exit_reason="signal", indicators_at_entry=entry_indicators, indicators_at_exit=_snap_indicators(i)))
                    equity += pnl

                    if close_pct >= 1.0:
                        position = 0.0
                        trailing_stop_price = 0.0
                        if use_risk_gate:
                            risk_monitor.clear_position(strategy_name)
                            risk_monitor.update_equity(equity)
                    else:
                        # Partial exit: reduce position, keep trailing stop
                        if position > 0:
                            position -= close_size
                        else:
                            position += close_size

            # Open new position with proportional sizing
            # Block re-entry on same candle a stop loss fired (prevents bleed loops)
            if not signal.reduce_only and signal.size > 0 and stop_fired_this_candle:
                diag_blocked_by_cooldown += 1
            if not signal.reduce_only and signal.size > 0 and not stop_fired_this_candle:
                if use_kelly:
                    # Kelly Criterion sizing: compute optimal risk from trade history
                    # Useful live (feeds from real track record), circular in backtest
                    from rift_engine.strategy import compute_kelly_risk
                    base_risk = compute_kelly_risk(trades)
                    risk_per_trade = base_risk * signal.size
                    risk_per_trade = min(risk_per_trade, 0.10)
                else:
                    # Flat 2% risk — matches raw research methodology
                    risk_per_trade = 0.02 * signal.size
                    risk_per_trade = min(risk_per_trade, 0.10)

                if use_confluence:
                    # Confluence sizing — adjust risk by how many data points agree
                    # Useful live with rich data, noisy in backtest (sparse OI/CVD)
                    direction = signal.side.value
                    confluence = 0
                    confluence_checks = 0

                    if state.oi_roc != 0:
                        confluence_checks += 1
                        if (state.oi_roc > 0 and direction == "long") or \
                           (state.oi_roc < 0 and direction == "short"):
                            confluence += 1

                    if abs(state.premium) > 0.0003:
                        confluence_checks += 1
                        if (state.premium < -0.0003 and direction == "long") or \
                           (state.premium > 0.0003 and direction == "short"):
                            confluence += 1

                    if state.relative_volume > 0:
                        confluence_checks += 1
                        if state.relative_volume > 1.2:
                            confluence += 1

                    if state.cvd != 0:
                        confluence_checks += 1
                        if (state.cvd > 0 and direction == "long") or \
                           (state.cvd < 0 and direction == "short"):
                            confluence += 1

                    if confluence_checks > 0:
                        confluence_ratio = confluence / confluence_checks
                        risk_per_trade *= (0.5 + confluence_ratio)

                sl = signal.stop_loss or 0.02
                if use_fractional_sizing:
                    # Model A: signal.size = fraction of equity to allocate as position
                    # 0.10 means "put 10% of equity into this trade"
                    position_value = equity * signal.size
                else:
                    # Model B: signal.size scales a risk-per-trade target, divided by stop loss
                    position_value = (equity * risk_per_trade) / sl
                position_value = min(position_value, equity * leverage)  # cap at max leveraged equity

                if use_volume_cap:
                    # Volume cap: limit position to 1% of daily volume to prevent market impact
                    if volumes is not None and len(volumes) > 24:
                        avg_daily_volume = float(np.mean(volumes[max(0, i-24):i+1])) * price * 24 if i > 0 else 0
                        if avg_daily_volume > 0:
                            volume_cap = avg_daily_volume * 0.01
                            position_value = min(position_value, volume_cap)

                # Risk gate: check portfolio exposure limits
                trade_allowed = True
                if use_risk_gate:
                    gate_decision = risk_gate.check(strategy_name, pair, signal.side.value, position_value, price)
                    if gate_decision.allowed:
                        position_value = gate_decision.permitted_notional
                    else:
                        trade_allowed = False

                if trade_allowed:
                    size = position_value / price

                    if size * price >= 10:  # Hyperliquid minimum $10 notional
                        fee = size * price * leverage * fee_rate
                        equity -= fee
                        if signal.side.value == "long":
                            position = size
                        else:
                            position = -size
                        entry_price = price
                        entry_time = int(timestamps[i])
                        entry_indicators = _snap_indicators(i)
                        if use_risk_gate:
                            risk_monitor.register_position(strategy_name, pair, signal.side.value, abs(position), price)
                            risk_monitor.update_equity(equity)
                        # Initialize trailing stop at initial stop level
                        sl_init = signal.stop_loss or (strategy.config.stop_loss_pct if hasattr(strategy.config, 'stop_loss_pct') else 0.02)
                        if position > 0:
                            trailing_stop_price = price * (1 - sl_init)
                        else:
                            trailing_stop_price = price * (1 + sl_init)

        equity_curve.append(equity)

        # Monthly performance tracking — snapshot equity at month transitions
        month_key = datetime.utcfromtimestamp(int(timestamps[i]) / 1000).strftime("%Y-%m")
        if month_key != current_month_key:
            if current_month_key:
                # Record the return for the month that just ended
                month_start_eq = monthly_equity.get(current_month_key, equity)
                monthly_equity[month_key] = equity  # start of new month
            else:
                monthly_equity[month_key] = equity  # first month
            current_month_key = month_key

        # Progress output (NDJSON) — suppressed when called as sub-step
        if not silent and i % max(1, n // 20) == 0:
            pct = round(i / n * 100)
            progress = {"type": "progress", "pct": pct, "candle": i, "total": n, "equity": round(equity, 2)}
            print(json.dumps(progress), flush=True)

    # Close any remaining position at last price
    if position != 0.0:
        price = closes[-1]
        if position > 0:
            pnl = position * (price - entry_price) * leverage - abs(position * price * leverage * fee_rate)
            pnl_pct = pnl / equity * 100
            trades.append(Trade(entry_time, int(timestamps[-1]), "long", entry_price, price, position, pnl, pnl_pct, exit_reason="end", indicators_at_entry=entry_indicators, indicators_at_exit=_snap_indicators(n - 1)))
        else:
            pnl = abs(position) * (entry_price - price) * leverage - abs(position * price * leverage * fee_rate)
            pnl_pct = pnl / equity * 100
            trades.append(Trade(entry_time, int(timestamps[-1]), "short", entry_price, price, abs(position), pnl, pnl_pct, exit_reason="end", indicators_at_entry=entry_indicators, indicators_at_exit=_snap_indicators(n - 1)))
        equity += pnl
        equity_curve.append(equity)  # ensure final equity is in the curve

    # Compute metrics
    equity_arr = np.array(equity_curve)
    returns = np.diff(equity_arr) / equity_arr[:-1]
    returns = returns[~np.isnan(returns)]

    winning = [t for t in trades if t.pnl > 0]
    losing = [t for t in trades if t.pnl <= 0]

    win_rate = len(winning) / len(trades) * 100 if trades else 0
    avg_win = np.mean([t.pnl_pct for t in winning]) if winning else 0
    avg_loss = np.mean([t.pnl_pct for t in losing]) if losing else 0

    # Max drawdown
    peak = np.maximum.accumulate(equity_arr)
    drawdown = (equity_arr - peak) / peak
    max_dd = float(np.min(drawdown)) * 100 if len(drawdown) > 0 else 0

    # Sharpe ratio (annualized based on candle interval)
    periods_per_year = _periods_per_year(interval)
    sharpe = 0.0
    if len(returns) > 1 and np.std(returns) > 0:
        sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(periods_per_year))

    # Profit factor
    gross_profit = sum(t.pnl for t in winning) if winning else 0
    gross_loss = abs(sum(t.pnl for t in losing)) if losing else 1
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

    total_return = (equity - initial_equity) / initial_equity * 100

    # Sortino ratio (only penalizes downside volatility)
    sortino = 0.0
    downside_returns = returns[returns < 0]
    if len(downside_returns) > 1 and np.std(downside_returns) > 0:
        sortino = float(np.mean(returns) / np.std(downside_returns) * np.sqrt(periods_per_year))

    # Calmar ratio (annual return / max drawdown)
    calmar = 0.0
    if n > 1 and max_dd != 0:
        duration_years = (timestamps[-1] - timestamps[0]) / (365.25 * 24 * 3600 * 1000)
        if duration_years > 0:
            annual_return = total_return / duration_years
            calmar = annual_return / abs(max_dd)

    # Expectancy (expected $ per trade)
    expectancy = 0.0
    if trades:
        expectancy = sum(t.pnl for t in trades) / len(trades)

    # Payoff ratio (avg win / avg loss)
    payoff_ratio = abs(float(avg_win) / float(avg_loss)) if avg_loss != 0 else 0.0

    # Recovery factor (total return / max drawdown)
    recovery_factor = abs(total_return / max_dd) if max_dd != 0 else 0.0

    # Max consecutive wins/losses
    max_cw = 0
    max_cl = 0
    cw = 0
    cl = 0
    for t in trades:
        if t.pnl > 0:
            cw += 1
            cl = 0
            max_cw = max(max_cw, cw)
        else:
            cl += 1
            cw = 0
            max_cl = max(max_cl, cl)

    # Average trade duration (in candles)
    avg_duration = 0.0
    if trades and n > 0:
        interval_ms = (timestamps[1] - timestamps[0]) if n > 1 else 3600000
        if interval_ms > 0:
            durations = [(t.exit_time - t.entry_time) / interval_ms for t in trades]
            avg_duration = float(np.mean(durations)) if durations else 0.0

    # Win rate by side
    long_trades = [t for t in trades if t.side == "long"]
    short_trades = [t for t in trades if t.side == "short"]
    long_wr = (len([t for t in long_trades if t.pnl > 0]) / len(long_trades) * 100) if long_trades else 0.0
    short_wr = (len([t for t in short_trades if t.pnl > 0]) / len(short_trades) * 100) if short_trades else 0.0

    # Drawdown recovery tracking
    max_dd_duration = 0
    dd_durations: list[int] = []
    eq_arr = np.array(equity_curve) if equity_curve else np.array([initial_equity])
    if len(eq_arr) > 1:
        peak = eq_arr[0]
        dd_start_idx = -1
        for i in range(len(eq_arr)):
            if eq_arr[i] < peak * 0.999:
                if dd_start_idx < 0:
                    dd_start_idx = i
            else:
                if dd_start_idx >= 0:
                    duration = i - dd_start_idx
                    dd_durations.append(duration)
                    if duration > max_dd_duration:
                        max_dd_duration = duration
                    dd_start_idx = -1
            if eq_arr[i] > peak:
                peak = eq_arr[i]
        # If still in drawdown at end
        if dd_start_idx >= 0:
            duration = len(eq_arr) - dd_start_idx
            dd_durations.append(duration)
            if duration > max_dd_duration:
                max_dd_duration = duration
    avg_recovery = float(np.mean(dd_durations)) if dd_durations else 0.0

    # ─── Institutional validation metrics ───

    # Deflated Sharpe Ratio (Bailey & Lopez de Prado)
    # Adjusts Sharpe for the number of strategies tested (selection bias)
    # DSR = Sharpe * sqrt(1 - skew*Sharpe/3 + kurt*Sharpe^2/24) * (1 - num_trials/(2*num_observations))
    num_trials = 17  # total strategies tested including deleted ones
    deflated_sharpe = 0.0
    if len(returns) > 10 and sharpe > 0:
        from scipy.stats import norm
        T = len(returns)
        skew_r = float(np.mean(((returns - np.mean(returns)) / (np.std(returns, ddof=1) + 1e-10)) ** 3)) if np.std(returns, ddof=1) > 0 else 0
        kurt_r = float(np.mean(((returns - np.mean(returns)) / (np.std(returns, ddof=1) + 1e-10)) ** 4) - 3) if np.std(returns, ddof=1) > 0 else 0
        # Expected max Sharpe under null hypothesis (no real edge) with N trials
        e_max_sharpe = float(norm.ppf(1 - 1 / num_trials)) * (1 / np.sqrt(T))
        # Sharpe standard error accounting for non-normality
        se_sharpe = np.sqrt((1 + 0.5 * sharpe**2 - skew_r * sharpe + (kurt_r / 4) * sharpe**2) / T)
        if se_sharpe > 0:
            # Probability that observed Sharpe exceeds expected max under null
            psr = float(norm.cdf((sharpe - e_max_sharpe) / se_sharpe))
            deflated_sharpe = sharpe * psr  # deflated = observed * probability it's real
        else:
            deflated_sharpe = 0.0

    # Outlier sensitivity — remove top 5 trades and recompute
    outlier_return_pct = 0.0
    outlier_sharpe = 0.0
    outlier_dependent = False
    if len(trades) > 5:
        sorted_trades = sorted(trades, key=lambda t: t.pnl, reverse=True)
        top5_pnl = sum(t.pnl for t in sorted_trades[:5])
        total_pnl = sum(t.pnl for t in trades)
        remaining_pnl = total_pnl - top5_pnl

        outlier_return_pct = (remaining_pnl / initial_equity) * 100
        outlier_dependent = bool(top5_pnl > total_pnl * 0.5) if total_pnl > 0 else False

        # Recompute Sharpe without top 5
        remaining_trades = sorted_trades[5:]
        if len(remaining_trades) > 1:
            rem_returns = np.array([t.pnl_pct / 100 for t in remaining_trades])
            rem_mean = float(np.mean(rem_returns))
            rem_std = float(np.std(rem_returns, ddof=1))
            outlier_sharpe = (rem_mean / rem_std * np.sqrt(8760 / max(1, len(returns) / max(1, len(trades))))) if rem_std > 0 else 0.0

    # Compute monthly returns from equity snapshots
    computed_monthly_returns: dict[str, float] = {}
    sorted_months = sorted(monthly_equity.keys())
    for idx_m in range(1, len(sorted_months)):
        month = sorted_months[idx_m]
        prev_month = sorted_months[idx_m - 1]
        start_eq = monthly_equity[prev_month]
        end_eq = monthly_equity[month]
        if start_eq > 0:
            computed_monthly_returns[prev_month] = ((end_eq - start_eq) / start_eq) * 100
    # Last month: use final equity
    if sorted_months and equity > 0:
        last_month = sorted_months[-1]
        last_start = monthly_equity[last_month]
        if last_start > 0:
            computed_monthly_returns[last_month] = ((equity - last_start) / last_start) * 100

    return BacktestResult(
        strategy_name=strategy_name,
        pair=pair,
        interval=interval,
        start_time=int(timestamps[0]) if n > 0 else 0,
        end_time=int(timestamps[-1]) if n > 0 else 0,
        initial_equity=initial_equity,
        final_equity=equity,
        total_return_pct=total_return,
        num_trades=len(trades),
        win_rate=win_rate,
        avg_win_pct=float(avg_win),
        avg_loss_pct=float(avg_loss),
        max_drawdown_pct=max_dd,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        profit_factor=profit_factor,
        expectancy=expectancy,
        payoff_ratio=payoff_ratio,
        recovery_factor=recovery_factor,
        max_consec_wins=max_cw,
        max_consec_losses=max_cl,
        avg_trade_duration=avg_duration,
        long_win_rate=long_wr,
        short_win_rate=short_wr,
        total_funding=total_funding_paid,
        max_drawdown_duration_candles=max_dd_duration,
        avg_recovery_candles=avg_recovery,
        deflated_sharpe=deflated_sharpe,
        outlier_return_pct=outlier_return_pct,
        outlier_sharpe=outlier_sharpe,
        num_trials=num_trials,
        outlier_dependent=outlier_dependent,
        equity_curve=equity_curve,
        trades=trades,
        monthly_returns=computed_monthly_returns,
        diagnostics={
            "signals_generated": diag_signals_long + diag_signals_short + diag_signals_close,
            "signals_long": diag_signals_long,
            "signals_short": diag_signals_short,
            "signals_close": diag_signals_close,
            "candles_no_signal": diag_signals_none,
            "stops_fired": diag_stops_fired,
            "entries_blocked_by_cooldown": diag_blocked_by_cooldown,
            "signal_rate_pct": round((diag_signals_long + diag_signals_short) / max(n, 1) * 100, 2),
            "stop_rate_pct": round(diag_stops_fired / max(len(trades), 1) * 100, 2) if trades else 0.0,
        },
        regime_performance=_compute_regime_performance(trades, closes, timestamps),
    )
