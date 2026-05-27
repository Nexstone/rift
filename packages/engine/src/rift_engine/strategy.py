"""Strategy base class and registry."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, get_args, get_origin, get_type_hints


@dataclass(frozen=True)
class Param:
    """Parameter metadata for strategy config fields.

    Use with Annotated to make config fields self-describing for AI agents
    and auto-sweep:

        @dataclass(frozen=True)
        class MyConfig:
            leverage: Annotated[float, Param("Position leverage", min=1.0, max=10.0, step=0.5)] = 3.0
    """

    desc: str = ""
    min: float | int | None = None
    max: float | int | None = None
    step: float | int | None = None
    choices: list | None = None  # for categoricals


def get_config_metadata(config_cls: type) -> dict[str, dict]:
    """Extract rich parameter metadata from a config dataclass.

    Returns dict of field_name → {type, default, desc, min, max, step, choices}.
    Works with both Annotated[type, Param(...)] fields and plain fields.
    """
    import dataclasses

    if config_cls is None or not dataclasses.is_dataclass(config_cls):
        return {}

    # get_type_hints with include_extras=True preserves Annotated metadata
    try:
        hints = get_type_hints(config_cls, include_extras=True)
    except Exception:
        hints = {}

    defaults = {}
    for f in dataclasses.fields(config_cls):
        if f.default is not dataclasses.MISSING:
            defaults[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:
            defaults[f.name] = f.default_factory()

    meta = {}
    for field_name, hint in hints.items():
        if field_name.startswith("_"):
            continue

        param: Param | None = None
        base_type = hint

        if get_origin(hint) is Annotated:
            args = get_args(hint)
            base_type = args[0]
            for arg in args[1:]:
                if isinstance(arg, Param):
                    param = arg
                    break

        # Resolve type name
        type_name = getattr(base_type, "__name__", str(base_type))

        entry: dict[str, Any] = {
            "type": type_name,
            "default": defaults.get(field_name),
        }
        if param:
            if param.desc:
                entry["desc"] = param.desc
            if param.min is not None:
                entry["min"] = param.min
            if param.max is not None:
                entry["max"] = param.max
            if param.step is not None:
                entry["step"] = param.step
            if param.choices is not None:
                entry["choices"] = param.choices

        meta[field_name] = entry

    return meta


def preflight_check_data(strategy: 'Strategy', interval: str = "1h") -> list[str]:
    """Check if all data required by a strategy's cross-asset indicators is available.

    Scans indicators() for CrossAsset/CrossCorrelation/CrossBeta/CrossLeadLag,
    verifies referenced coin data exists in cache.

    Returns list of error messages. Empty list = all good.
    """
    from rift_data.historical import load_candles_smart

    errors = []
    try:
        indicators = strategy.indicators()
    except Exception:
        return errors

    cross_asset_types = ("cross_asset", "cross_correlation", "cross_beta", "cross_lead_lag")
    for ind_name, ind in indicators.items():
        if ind.name in cross_asset_types:
            ref_coin = ind.params.get("coin", "")
            if not ref_coin:
                continue
            df = load_candles_smart(ref_coin, interval)
            if df is None or len(df) == 0:
                # Strip xyz: for user-friendly fetch command
                fetch_name = ref_coin.split(":")[-1] if ":" in ref_coin else ref_coin
                errors.append(
                    f"Indicator '{ind_name}' requires {ref_coin} data. "
                    f"Run: rift fetch {fetch_name} --tf {interval}"
                )

    return errors


# Global strategy registry
_REGISTRY: dict[str, type[Strategy]] = {}


class Side(Enum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True)
class Signal:
    """A trading signal emitted by a strategy."""

    side: Side
    size: float
    stop_loss: float | None = None
    take_profit: float | None = None
    reduce_only: bool = False
    close_pct: float = 1.0  # 1.0 = close all, 0.5 = close half (for partial exits)

    @classmethod
    def long(cls, size: float = 1.0, sl: float | None = None, tp: float | None = None) -> Signal:
        return cls(side=Side.LONG, size=size, stop_loss=sl, take_profit=tp)

    @classmethod
    def short(cls, size: float = 1.0, sl: float | None = None, tp: float | None = None) -> Signal:
        return cls(side=Side.SHORT, size=size, stop_loss=sl, take_profit=tp)

    @classmethod
    def close(cls, pct: float = 1.0) -> Signal:
        return cls(side=Side.LONG, size=0.0, reduce_only=True, close_pct=pct)


def compute_kelly_risk(trades: list, min_trades: int = 20, max_risk: float = 0.25, kelly_fraction: float = 0.5) -> float:
    """Compute Kelly Criterion position sizing from trade history.

    Returns the risk-per-trade as a decimal (e.g., 0.04 = 4% risk).
    Uses half-Kelly by default (kelly_fraction=0.5) for safety.

    Falls back to 0.02 (flat 2%) if insufficient trade history.
    """
    if len(trades) < min_trades:
        return 0.02  # not enough data, use flat 2%

    # Use last 50 trades (or all if fewer)
    recent = trades[-50:]
    winners = [t for t in recent if (t.pnl if hasattr(t, 'pnl') else t.get('pnl', 0)) > 0]
    losers = [t for t in recent if (t.pnl if hasattr(t, 'pnl') else t.get('pnl', 0)) <= 0]

    if not winners or not losers:
        return 0.02

    def _get_pnl(t):
        return t.pnl if hasattr(t, 'pnl') else t.get('pnl', 0)

    win_rate = len(winners) / len(recent)
    avg_win = abs(sum(_get_pnl(t) for t in winners) / len(winners))
    avg_loss = abs(sum(_get_pnl(t) for t in losers) / len(losers))

    if avg_loss == 0:
        return 0.02

    # Kelly formula: f* = (p * b - q) / b
    # where p = win probability, q = loss probability, b = avg_win / avg_loss
    b = avg_win / avg_loss
    kelly = (win_rate * b - (1 - win_rate)) / b

    # Apply fractional Kelly and cap
    kelly = kelly * kelly_fraction
    kelly = max(0.005, min(kelly, max_risk))  # floor 0.5%, cap at max_risk

    return kelly


@dataclass
class Candle:
    """A single OHLCV candle."""

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class StrategyState:
    """Mutable state passed to strategy on each candle."""

    indicators: dict[str, float] = field(default_factory=dict)
    position: float = 0.0  # positive = long, negative = short
    equity: float = 0.0
    unrealized_pnl: float = 0.0
    funding_rate: float = 0.0  # current hourly funding rate (positive = longs pay shorts)
    funding_rate_zscore: float = 0.0  # z-score of funding rate vs rolling window
    cumulative_funding: float = 0.0  # total funding received/paid
    predicted_funding: float = 0.0  # predicted rate for NEXT settlement (from predictedFundings API)
    # Market context — from metaAndAssetCtxs endpoint
    open_interest: float = 0.0      # total OI in contracts
    oi_roc: float = 0.0             # OI rate of change (% change from prior period)
    oi_delta: float = 0.0           # OI absolute change from prior period
    oi_zscore: float = 0.0          # OI z-score vs 30-day rolling window
    premium: float = 0.0            # mark vs oracle premium (market directional bias)
    oracle_price: float = 0.0       # oracle price (spot reference)
    day_volume: float = 0.0         # 24h notional volume
    # Cross-exchange funding — from predictedFundings endpoint
    funding_divergence: float = 0.0  # HL funding minus CEX average (arb signal)
    # Net positioning — directional OI decomposition (Leviathan method)
    net_longs: float = 0.0           # cumulative longs opened minus longs closed
    net_shorts: float = 0.0          # cumulative shorts opened minus shorts closed
    net_delta: float = 0.0           # net_longs - net_shorts (positive = long-dominant)
    # Volume delta / CVD (Leviathan Volume Suite method)
    volume_delta: float = 0.0        # buy volume - sell volume (per candle)
    cvd: float = 0.0                 # cumulative volume delta (running total)
    relative_volume: float = 0.0     # current volume / rolling average (>1.5 = unusual)
    # Market breadth — cross-asset RSI analysis
    market_breadth_ob: float = 0.0   # % of top coins with RSI > 70 (crowd overbought)
    market_breadth_os: float = 0.0   # % of top coins with RSI < 30 (crowd oversold)
    market_avg_rsi: float = 0.0      # average RSI across top coins
    # Orderbook microstructure (from L2 snapshots, 5-min resolution)
    bid_ask_imbalance: float = 0.0   # (bid_vol - ask_vol) / total, range -1 to +1
    spread_bps: float = 0.0         # bid-ask spread in basis points
    bid_depth: float = 0.0          # total bid volume across 5 levels
    ask_depth: float = 0.0          # total ask volume across 5 levels
    depth_ratio: float = 0.0        # bid_depth / ask_depth (>1 = bid-heavy)
    # Ground-truth order flow (from S3 tick data via rift sync, 0 if not available)
    buy_volume: float = 0.0         # actual buy aggressor volume this candle
    sell_volume: float = 0.0        # actual sell aggressor volume this candle
    taker_ratio: float = 0.0        # % of fills that crossed the spread (aggressive)
    opens_long: float = 0.0         # volume of new long positions opened
    closes_long: float = 0.0        # volume of long positions closed
    opens_short: float = 0.0        # volume of new short positions opened
    closes_short: float = 0.0       # volume of short positions closed
    net_flow: float = 0.0           # net position change (positive = net opening)
    candle_pnl: float = 0.0         # total realized PnL by all traders this candle
    candle_fees: float = 0.0        # total fees paid this candle

    def __getattr__(self, name: str) -> Any:
        """Allow accessing indicators as attributes: state.rsi, state.ema_fast.

        Returns 0.0 for NaN values (indicator not warmed up) for backward
        compatibility — strategies check `if ema == 0: return None`.
        """
        import math
        indicators = object.__getattribute__(self, "indicators")
        if name in indicators:
            val = indicators[name]
            if isinstance(val, float) and math.isnan(val):
                return 0.0  # backward compatible: NaN → 0.0 for warmup checks
            return val
        raise AttributeError(f"No indicator named '{name}'")


class Indicator:
    """Base class for indicator declarations."""

    def __init__(self, name: str, **params: Any):
        self.name = name
        self.params = params


class EMA(Indicator):
    def __init__(self, period: int):
        super().__init__("ema", period=period)


class SMA(Indicator):
    def __init__(self, period: int):
        super().__init__("sma", period=period)


class RSI(Indicator):
    def __init__(self, period: int = 14):
        super().__init__("rsi", period=period)


class MACD(Indicator):
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        super().__init__("macd", fast=fast, slow=slow, signal=signal)


class BollingerBands(Indicator):
    def __init__(self, period: int = 20, std: float = 2.0):
        super().__init__("bbands", period=period, std=std)


class BBUpper(Indicator):
    def __init__(self, period: int = 20, std: float = 2.0):
        super().__init__("bbands_upper", period=period, std=std)


class BBLower(Indicator):
    def __init__(self, period: int = 20, std: float = 2.0):
        super().__init__("bbands_lower", period=period, std=std)


class BBWidth(Indicator):
    def __init__(self, period: int = 20, std: float = 2.0):
        super().__init__("bbands_width", period=period, std=std)


class VolRatio(Indicator):
    def __init__(self, period: int = 20):
        super().__init__("vol_ratio", period=period)


class ATR(Indicator):
    def __init__(self, period: int = 14):
        super().__init__("atr", period=period)


class VWAP(Indicator):
    """Rolling Volume-Weighted Average Price."""

    def __init__(self, period: int = 24):
        super().__init__("vwap", period=period)


class VWAPStd(Indicator):
    """Standard deviation of price from VWAP."""

    def __init__(self, period: int = 24):
        super().__init__("vwap_std", period=period)


class ADX(Indicator):
    """Average Directional Index — measures trend strength (0-100)."""

    def __init__(self, period: int = 14):
        super().__init__("adx", period=period)


class PlusDI(Indicator):
    """+DI — positive directional indicator (uptrend strength)."""

    def __init__(self, period: int = 14):
        super().__init__("plus_di", period=period)


class MinusDI(Indicator):
    """-DI — negative directional indicator (downtrend strength)."""

    def __init__(self, period: int = 14):
        super().__init__("minus_di", period=period)


class SwingHigh(Indicator):
    """Recent swing high (highest high in lookback)."""

    def __init__(self, period: int = 20):
        super().__init__("swing_high", period=period)


class SwingLow(Indicator):
    """Recent swing low (lowest low in lookback)."""

    def __init__(self, period: int = 20):
        super().__init__("swing_low", period=period)


class ATR_SMA(Indicator):
    """Rolling average of ATR — measures 'normal' volatility level."""

    def __init__(self, period: int = 30, atr_period: int = 14):
        super().__init__("atr_sma", period=period, atr_period=atr_period)


# ─── MOMENTUM INDICATORS ─────────────────────────────────────

class MACDSignal(Indicator):
    """MACD signal line — EMA of the MACD line. Crossovers = trade signals."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        super().__init__("macd_signal", fast=fast, slow=slow, signal=signal)


class MACDHistogram(Indicator):
    """MACD histogram — MACD minus signal. Shows momentum acceleration."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        super().__init__("macd_histogram", fast=fast, slow=slow, signal=signal)


class StochK(Indicator):
    """Stochastic %K — fast overbought/oversold oscillator (0-100)."""

    def __init__(self, period: int = 14, smooth: int = 3):
        super().__init__("stoch_k", period=period, smooth=smooth)


class StochD(Indicator):
    """Stochastic %D — SMA of %K. K/D crossovers = signals."""

    def __init__(self, period: int = 14, smooth: int = 3):
        super().__init__("stoch_d", period=period, smooth=smooth)


class WilliamsR(Indicator):
    """Williams %R — fast overbought/oversold (-100 to 0)."""

    def __init__(self, period: int = 14):
        super().__init__("williams_r", period=period)


class CCI(Indicator):
    """Commodity Channel Index — measures price deviation from mean."""

    def __init__(self, period: int = 20):
        super().__init__("cci", period=period)


class ROC(Indicator):
    """Rate of Change — percentage price change over N periods."""

    def __init__(self, period: int = 12):
        super().__init__("roc", period=period)


class MFI(Indicator):
    """Money Flow Index — volume-weighted RSI (0-100)."""

    def __init__(self, period: int = 14):
        super().__init__("mfi", period=period)


# ─── VOLUME INDICATORS ───────────────────────────────────────

class OBV(Indicator):
    """On-Balance Volume — cumulative volume direction. Spots divergences."""

    def __init__(self):
        super().__init__("obv")


class CMF(Indicator):
    """Chaikin Money Flow — buying/selling pressure (-1 to +1)."""

    def __init__(self, period: int = 20):
        super().__init__("cmf", period=period)


# ─── VOLATILITY INDICATORS ───────────────────────────────────

class KeltnerUpper(Indicator):
    """Keltner Channel upper band — EMA + ATR multiplier."""

    def __init__(self, period: int = 20, atr_period: int = 14, mult: float = 2.0):
        super().__init__("keltner_upper", period=period, atr_period=atr_period, mult=mult)


class KeltnerLower(Indicator):
    """Keltner Channel lower band — EMA - ATR multiplier."""

    def __init__(self, period: int = 20, atr_period: int = 14, mult: float = 2.0):
        super().__init__("keltner_lower", period=period, atr_period=atr_period, mult=mult)


class DonchianUpper(Indicator):
    """Donchian Channel upper — highest high in lookback. Breakout level."""

    def __init__(self, period: int = 20):
        super().__init__("donchian_upper", period=period)


class DonchianLower(Indicator):
    """Donchian Channel lower — lowest low in lookback. Breakdown level."""

    def __init__(self, period: int = 20):
        super().__init__("donchian_lower", period=period)


class StdDev(Indicator):
    """Standard deviation of closing prices — raw volatility measure."""

    def __init__(self, period: int = 20):
        super().__init__("stddev", period=period)


class HistVol(Indicator):
    """Historical volatility — annualized standard deviation of log returns."""

    def __init__(self, period: int = 20):
        super().__init__("histvol", period=period)


# ─── TREND INDICATORS ────────────────────────────────────────

class Supertrend(Indicator):
    """Supertrend — ATR-based trend direction. Positive = uptrend, negative = downtrend."""

    def __init__(self, period: int = 10, mult: float = 3.0):
        super().__init__("supertrend", period=period, mult=mult)


class ParabolicSAR(Indicator):
    """Parabolic SAR — trailing stop that flips direction."""

    def __init__(self, af_start: float = 0.02, af_step: float = 0.02, af_max: float = 0.2):
        super().__init__("psar", af_start=af_start, af_step=af_step, af_max=af_max)


class AroonUp(Indicator):
    """Aroon Up — how recently the highest high occurred (0-100)."""

    def __init__(self, period: int = 25):
        super().__init__("aroon_up", period=period)


class AroonDown(Indicator):
    """Aroon Down — how recently the lowest low occurred (0-100)."""

    def __init__(self, period: int = 25):
        super().__init__("aroon_down", period=period)


class HMA(Indicator):
    """Hull Moving Average — fast, smooth, low-lag trend line."""

    def __init__(self, period: int = 20):
        super().__init__("hma", period=period)


class DEMA(Indicator):
    """Double Exponential Moving Average — less lag than EMA."""

    def __init__(self, period: int = 20):
        super().__init__("dema", period=period)


class TEMA(Indicator):
    """Triple Exponential Moving Average — even less lag."""

    def __init__(self, period: int = 20):
        super().__init__("tema", period=period)


class LinRegSlope(Indicator):
    """Linear regression slope — statistical trend direction and strength."""

    def __init__(self, period: int = 20):
        super().__init__("linreg_slope", period=period)


class IchimokuTenkan(Indicator):
    """Ichimoku Tenkan-sen (conversion line) — (highest high + lowest low) / 2."""

    def __init__(self, period: int = 9):
        super().__init__("ichimoku_tenkan", period=period)


class IchimokuKijun(Indicator):
    """Ichimoku Kijun-sen (base line) — longer period midpoint."""

    def __init__(self, period: int = 26):
        super().__init__("ichimoku_kijun", period=period)


class IchimokuSenkouA(Indicator):
    """Ichimoku Senkou Span A — midpoint of Tenkan and Kijun (leading)."""

    def __init__(self, tenkan: int = 9, kijun: int = 26):
        super().__init__("ichimoku_senkou_a", tenkan=tenkan, kijun=kijun)


class IchimokuSenkouB(Indicator):
    """Ichimoku Senkou Span B — highest high + lowest low / 2 over long period (leading)."""

    def __init__(self, period: int = 52):
        super().__init__("ichimoku_senkou_b", period=period)


# ─── STRUCTURE INDICATORS ────────────────────────────────────

class PivotPoint(Indicator):
    """Pivot Point — (high + low + close) / 3. Institutional support/resistance."""

    def __init__(self, period: int = 1):
        super().__init__("pivot_point", period=period)


# ─── ADAPTIVE INDICATORS ────────────────────────────────────

class KAMA(Indicator):
    """Kaufman Adaptive Moving Average — speed adjusts based on trend efficiency.
    Tight in trends, loose in chop. Professional-grade noise filter."""

    def __init__(self, period: int = 10, fast: int = 2, slow: int = 30):
        super().__init__("kama", period=period, fast=fast, slow=slow)


class AdaptiveRSI(Indicator):
    """ATR-scaled RSI — period adjusts with volatility.
    Shorter period in high vol (faster reaction), longer in low vol (less noise)."""

    def __init__(self, base_period: int = 14, atr_period: int = 14, min_period: int = 7, max_period: int = 28):
        super().__init__("adaptive_rsi", base_period=base_period, atr_period=atr_period,
                         min_period=min_period, max_period=max_period)


class AdaptiveEMA(Indicator):
    """Volatility-adaptive EMA — period scales with ATR.
    Faster in high vol, slower in low vol."""

    def __init__(self, base_period: int = 20, atr_period: int = 14, min_period: int = 5, max_period: int = 50):
        super().__init__("adaptive_ema", base_period=base_period, atr_period=atr_period,
                         min_period=min_period, max_period=max_period)


class VolatilityRegime(Indicator):
    """Classifies current volatility as low (0), normal (1), or high (2).
    Based on ATR percentile within rolling lookback window."""

    def __init__(self, atr_period: int = 14, lookback: int = 100):
        super().__init__("vol_regime", atr_period=atr_period, lookback=lookback)


# ─── MULTI-TIMEFRAME INDICATORS ────────────────────────────

class HTF(Indicator):
    """Higher-timeframe indicator wrapper.

    Computes any indicator on a resampled higher timeframe, then maps
    values back to the base timeframe (forward-fill).

    Usage:
        indicators() → {
            "ema_fast": EMA(12),              # Base timeframe EMA
            "ema_4h": HTF(EMA(50), "4h"),     # 4h EMA on base chart
            "rsi_daily": HTF(RSI(14), "1d"),  # Daily RSI
        }
    """

    def __init__(self, indicator: Indicator, timeframe: str):
        super().__init__("htf", inner=indicator, timeframe=timeframe)
        self.inner = indicator
        self.timeframe = timeframe


# ─── CROSS-ASSET INDICATORS ────────────────────────────────

class CrossAsset(Indicator):
    """Reference another asset's price series as an indicator.

    The engine loads the referenced coin's data, aligns timestamps with
    the primary asset, and makes it available as a standard indicator.

    Usage:
        indicators() → {"sp500": CrossAsset("xyz:SP500")}
        on_candle() → state.sp500  # SP500 close price at this candle's time
    """

    def __init__(self, coin: str, field: str = "close"):
        super().__init__("cross_asset", coin=coin, field=field)


class CrossCorrelation(Indicator):
    """Rolling correlation between this asset and another.
    Returns -1 to +1. Useful for pair trading and hedging."""

    def __init__(self, coin: str, period: int = 24):
        super().__init__("cross_correlation", coin=coin, period=period)


class CrossBeta(Indicator):
    """Rolling beta (sensitivity) of this asset vs another.
    Beta > 1 = more volatile than reference. Used for risk-adjusted sizing."""

    def __init__(self, coin: str, period: int = 48):
        super().__init__("cross_beta", coin=coin, period=period)


class CrossLeadLag(Indicator):
    """Lagged return of another asset. Tests if another asset leads this one.
    Positive lag = reference asset's return N candles ago."""

    def __init__(self, coin: str, lag: int = 1):
        super().__init__("cross_lead_lag", coin=coin, lag=lag)


# ─── ORDER FLOW INDICATORS (S3 ground-truth) ───────────────

class TakerRatio(Indicator):
    """Rolling taker ratio — % of volume from aggressive fills.
    High ratio = urgent directional conviction. Requires S3 data."""

    def __init__(self, period: int = 20):
        super().__init__("taker_ratio_ind", period=period)


class BuySellImbalance(Indicator):
    """Rolling buy/sell volume imbalance from ground-truth fill data.
    Range -1 (all selling) to +1 (all buying). Requires S3 data."""

    def __init__(self, period: int = 14):
        super().__init__("buy_sell_imbalance", period=period)


class PositionFlow(Indicator):
    """Net position opening flow — are traders entering or exiting?
    Positive = net opening (conviction), negative = net closing (de-risking). Requires S3 data."""

    def __init__(self, period: int = 20):
        super().__init__("position_flow", period=period)


class PnLFlow(Indicator):
    """Rolling realized PnL of all traders.
    Negative = closing at loss (capitulation). Positive = profit-taking. Requires S3 data."""

    def __init__(self, period: int = 20):
        super().__init__("pnl_flow", period=period)


class TradeIntensity(Indicator):
    """Smoothed taker ratio — EMA of aggressive fill percentage.
    Spikes indicate urgency, breakout confirmation. Requires S3 data."""

    def __init__(self, period: int = 10):
        super().__init__("trade_intensity", period=period)


class Strategy:
    """Base class for all RIFT strategies.

    Subclasses must implement:
        - on_candle(candle, state) -> Signal | None
        - indicators() -> dict[str, Indicator]

    Subclasses should define:
        - config_class: a frozen dataclass with strategy parameters

    Subclasses MAY override (for research-pipeline sizing):
        - recommended_train_months: walk-forward training window length
        - recommended_test_months:  walk-forward test window length

    The defaults (2 train / 1 test) suit most simple/parametric strategies.
    HMM, ML, or other long-warmup strategies should declare longer windows
    explicitly — the research pipeline reads these attributes, no heuristics.
    """

    config_class: type | None = None
    default_interval: str = "1h"  # preferred timeframe for this strategy

    # Walk-forward sizing — strategies declare their own needs, no sniffing.
    recommended_train_months: int = 2
    recommended_test_months: int = 1

    def __init__(self, config: Any | None = None):
        if config is not None:
            self.config = config
        elif self.config_class is not None:
            self.config = self.config_class()
        else:
            self.config = None

    def on_candle(self, candle: Candle, state: StrategyState) -> Signal | None:
        """Called on each new candle. Return a Signal to trade, or None to hold."""
        raise NotImplementedError

    def indicators(self) -> dict[str, Indicator]:
        """Declare indicators needed by this strategy."""
        return {}

    def position_size(self) -> float:
        """Default position sizing. Override for custom sizing."""
        return 1.0


def register(name: str):
    """Decorator to register a strategy class in the global registry."""

    def decorator(cls: type[Strategy]) -> type[Strategy]:
        _REGISTRY[name] = cls
        return cls

    return decorator


def _fuzzy_match(name: str, candidates: list[str], max_distance: int = 3) -> str | None:
    """Find the closest matching strategy name using edit distance."""
    best = None
    best_dist = max_distance + 1

    for candidate in candidates:
        # Simple Levenshtein distance
        n, m = len(name), len(candidate)
        if abs(n - m) > max_distance:
            continue

        dp = list(range(m + 1))
        for i in range(1, n + 1):
            prev = dp[0]
            dp[0] = i
            for j in range(1, m + 1):
                temp = dp[j]
                if name[i - 1] == candidate[j - 1]:
                    dp[j] = prev
                else:
                    dp[j] = 1 + min(prev, dp[j], dp[j - 1])
                prev = temp

        if dp[m] < best_dist:
            best_dist = dp[m]
            best = candidate

    return best


import re as _re
_VALID_STRATEGY_NAME = _re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]{0,63}$')


def get_strategy(name: str) -> type[Strategy]:
    """Look up a registered strategy by name."""
    if not _VALID_STRATEGY_NAME.match(name):
        raise KeyError(f"Invalid strategy name '{name}'. Must be a valid Python identifier (letters, numbers, underscores).")
    if name not in _REGISTRY:
        available = list(_REGISTRY.keys())
        suggestion = _fuzzy_match(name, available)
        msg = f"Strategy '{name}' not found."
        if suggestion:
            msg += f" Did you mean '{suggestion}'?"
        msg += f" Available: {available}"
        raise KeyError(msg)
    return _REGISTRY[name]


def list_strategies() -> dict[str, type[Strategy]]:
    """Return all registered strategies."""
    return dict(_REGISTRY)


def load_strategy_file(path: Path) -> None:
    """Load a Python file to trigger its @register decorators."""
    if not _VALID_STRATEGY_NAME.match(path.stem):
        return  # skip files with invalid names (path traversal protection)
    spec = importlib.util.spec_from_file_location(f"rift.strategies.{path.stem}", path)
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)


def discover_strategies(directories: list[Path]) -> None:
    """Scan directories for .py strategy files and load them."""
    for d in directories:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            load_strategy_file(f)
