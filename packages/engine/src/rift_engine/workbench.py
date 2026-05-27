"""Strategy Workbench — config-as-data format + code generator.

Strategies are defined as structured JSON configs, not raw Python.
The workbench edits the config. The code generator produces the class.
The backtest engine runs it. Users never need to write Python.

Config format:
{
    "name": "my_strategy",
    "description": "Short description",
    "timeframe": "1h",
    "entry": {
        "conditions": [...],
        "direction": "both"  # "both", "long_only", "short_only"
    },
    "exit": {
        "conditions": [...],
        "max_hold": 48
    },
    "risk": {
        "stop_loss": 0.02,
        "risk_per_trade": 0.02,
        "leverage": 2.0
    },
    "filters": {
        "hmm_filter": false,
        "rsi_confirmation": false,
        "volume_filter": false,
        "adx_trend": false
    }
}

Conditions use a simple expression format:
    {"indicator": "funding_rate", "op": ">", "value": 0.000015}
    {"indicator": "price", "op": "<", "ref": "ema_trend"}
    {"indicator": "rsi", "op": "<", "value": 40.0}
    {"indicator": "vwap_zscore", "op": "<", "value": -2.5}
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Where custom strategy configs + generated code live
WORKBENCH_DIR = Path.home() / ".rift" / "workbench"
EXPERIMENTS_DB = WORKBENCH_DIR / "experiments.db"
CONFIGS_DIR = WORKBENCH_DIR / "configs"
GENERATED_DIR = WORKBENCH_DIR / "strategies"


# ---------------------------------------------------------------------------
# Available indicators and their required params
# ---------------------------------------------------------------------------

INDICATOR_CATALOG = {
    # Moving Averages
    "ema": {"class": "EMA", "params": {"period": 100}, "description": "Exponential Moving Average"},
    "sma": {"class": "SMA", "params": {"period": 20}, "description": "Simple Moving Average"},
    "dema": {"class": "DEMA", "params": {"period": 20}, "description": "Double EMA (less lag)"},
    "tema": {"class": "TEMA", "params": {"period": 20}, "description": "Triple EMA (even less lag)"},
    "hma": {"class": "HMA", "params": {"period": 20}, "description": "Hull Moving Average (fast, smooth)"},
    # Momentum
    "rsi": {"class": "RSI", "params": {"period": 14}, "description": "Relative Strength Index"},
    "macd": {"class": "MACD", "params": {"fast": 12, "slow": 26, "signal": 9}, "description": "MACD Line"},
    "macd_signal": {"class": "MACDSignal", "params": {"fast": 12, "slow": 26, "signal": 9}, "description": "MACD Signal Line"},
    "macd_histogram": {"class": "MACDHistogram", "params": {"fast": 12, "slow": 26, "signal": 9}, "description": "MACD Histogram"},
    "stoch_k": {"class": "StochK", "params": {"period": 14, "smooth": 3}, "description": "Stochastic %K"},
    "stoch_d": {"class": "StochD", "params": {"period": 14, "smooth": 3}, "description": "Stochastic %D"},
    "williams_r": {"class": "WilliamsR", "params": {"period": 14}, "description": "Williams %R (-100 to 0)"},
    "cci": {"class": "CCI", "params": {"period": 20}, "description": "Commodity Channel Index"},
    "roc": {"class": "ROC", "params": {"period": 12}, "description": "Rate of Change %"},
    "mfi": {"class": "MFI", "params": {"period": 14}, "description": "Money Flow Index (volume RSI)"},
    # Volume
    "vol_ratio": {"class": "VolRatio", "params": {"period": 20}, "description": "Volume Ratio (current / avg)"},
    "obv": {"class": "OBV", "params": {}, "description": "On-Balance Volume"},
    "cmf": {"class": "CMF", "params": {"period": 20}, "description": "Chaikin Money Flow (-1 to +1)"},
    # Volatility
    "atr": {"class": "ATR", "params": {"period": 14}, "description": "Average True Range"},
    "atr_sma": {"class": "ATR_SMA", "params": {"period": 30, "atr_period": 14}, "description": "Rolling ATR Average"},
    "bbands_upper": {"class": "BBUpper", "params": {"period": 20, "std": 2.0}, "description": "Bollinger Upper Band"},
    "bbands_lower": {"class": "BBLower", "params": {"period": 20, "std": 2.0}, "description": "Bollinger Lower Band"},
    "bbands_width": {"class": "BBWidth", "params": {"period": 20, "std": 2.0}, "description": "Bollinger Band Width"},
    "keltner_upper": {"class": "KeltnerUpper", "params": {"period": 20, "atr_period": 14, "mult": 2.0}, "description": "Keltner Upper Band"},
    "keltner_lower": {"class": "KeltnerLower", "params": {"period": 20, "atr_period": 14, "mult": 2.0}, "description": "Keltner Lower Band"},
    "donchian_upper": {"class": "DonchianUpper", "params": {"period": 20}, "description": "Donchian Upper (highest high)"},
    "donchian_lower": {"class": "DonchianLower", "params": {"period": 20}, "description": "Donchian Lower (lowest low)"},
    "stddev": {"class": "StdDev", "params": {"period": 20}, "description": "Standard Deviation"},
    "histvol": {"class": "HistVol", "params": {"period": 20}, "description": "Historical Volatility (annualized)"},
    "vwap": {"class": "VWAP", "params": {"period": 144}, "description": "Volume-Weighted Average Price"},
    "vwap_std": {"class": "VWAPStd", "params": {"period": 144}, "description": "VWAP Standard Deviation"},
    # Trend
    "adx": {"class": "ADX", "params": {"period": 14}, "description": "Average Directional Index (0-100)"},
    "plus_di": {"class": "PlusDI", "params": {"period": 14}, "description": "+DI (uptrend strength)"},
    "minus_di": {"class": "MinusDI", "params": {"period": 14}, "description": "-DI (downtrend strength)"},
    "supertrend": {"class": "Supertrend", "params": {"period": 10, "mult": 3.0}, "description": "Supertrend direction (+1/-1)"},
    "psar": {"class": "ParabolicSAR", "params": {"af_start": 0.02, "af_step": 0.02, "af_max": 0.2}, "description": "Parabolic SAR (trailing stop)"},
    "aroon_up": {"class": "AroonUp", "params": {"period": 25}, "description": "Aroon Up (0-100)"},
    "aroon_down": {"class": "AroonDown", "params": {"period": 25}, "description": "Aroon Down (0-100)"},
    "linreg_slope": {"class": "LinRegSlope", "params": {"period": 20}, "description": "Linear Regression Slope"},
    "ichimoku_tenkan": {"class": "IchimokuTenkan", "params": {"period": 9}, "description": "Ichimoku Tenkan-sen (conversion)"},
    "ichimoku_kijun": {"class": "IchimokuKijun", "params": {"period": 26}, "description": "Ichimoku Kijun-sen (base)"},
    "ichimoku_senkou_a": {"class": "IchimokuSenkouA", "params": {"tenkan": 9, "kijun": 26}, "description": "Ichimoku Cloud upper edge"},
    "ichimoku_senkou_b": {"class": "IchimokuSenkouB", "params": {"period": 52}, "description": "Ichimoku Cloud lower edge"},
    # Structure
    "swing_high": {"class": "SwingHigh", "params": {"period": 20}, "description": "Recent Swing High"},
    "swing_low": {"class": "SwingLow", "params": {"period": 20}, "description": "Recent Swing Low"},
    "pivot_point": {"class": "PivotPoint", "params": {"period": 1}, "description": "Pivot Point (S/R level)"},
}

# Built-in state fields that don't need indicator declarations
STATE_FIELDS = {
    "funding_rate": "Current hourly funding rate",
    "predicted_funding": "Predicted next funding rate",
    "funding_rate_zscore": "Z-score of funding rate",
    "funding_divergence": "HL funding minus CEX average (arb signal)",
    "cumulative_funding": "Total funding received/paid",
    "open_interest": "Total open interest (contracts)",
    "oi_roc": "OI rate of change (% change from prior period)",
    "oi_delta": "OI absolute change from prior period",
    "oi_zscore": "OI z-score vs 30-day rolling window",
    "premium": "Mark vs oracle premium (market bias)",
    "oracle_price": "Oracle price (spot reference)",
    "day_volume": "24h notional volume",
    "volume_delta": "Buy volume minus sell volume (per candle)",
    "cvd": "Cumulative Volume Delta (running buy-sell total)",
    "relative_volume": "Current volume / 20-period average (>1.5 = unusual)",
    "net_longs": "Cumulative net longs (longs opened - longs closed)",
    "net_shorts": "Cumulative net shorts (shorts opened - shorts closed)",
    "net_delta": "Net longs minus net shorts (positive = long-dominant)",
    "market_breadth_ob": "% of top coins overbought (RSI > 70)",
    "market_breadth_os": "% of top coins oversold (RSI < 30)",
    "market_avg_rsi": "Average RSI across top coins",
    "position": "Current position (+ long, - short)",
    "equity": "Current equity",
    "unrealized_pnl": "Unrealized P&L",
}

# Operators
OPERATORS = {
    ">": "greater than",
    "<": "less than",
    ">=": "greater than or equal",
    "<=": "less than or equal",
    "==": "equal to",
    "!=": "not equal to",
    "crosses_above": "crosses above",
    "crosses_below": "crosses below",
}


# ---------------------------------------------------------------------------
# Validated strategy component library — for the mixer
# ---------------------------------------------------------------------------

VALIDATED_COMPONENTS = {
    "entry_signals": {
        "funding_rate_extreme": {
            "source": "workbench-builtin",
            "description": "Enter when funding rate is extreme",
            "conditions": [
                {"indicator": "funding_rate", "op": ">", "value": 0.000015, "side": "short"},
                {"indicator": "funding_rate", "op": "<", "value": -0.000015, "side": "long"},
            ],
            "indicators": {"ema_trend": {"class": "EMA", "params": {"period": 100}}},
            "extra_conditions": [
                {"indicator": "price", "op": ">", "ref": "ema_trend", "side": "short"},
                {"indicator": "price", "op": "<", "ref": "ema_trend", "side": "long"},
            ],
        },
        "vwap_deviation": {
            "source": "vwap_dev",
            "description": "Enter at extreme VWAP deviation (mean reversion)",
            "conditions": [
                {"indicator": "vwap_zscore", "op": "<", "value": -2.5, "side": "long"},
                {"indicator": "vwap_zscore", "op": ">", "value": 2.5, "side": "short"},
            ],
            "indicators": {
                "vwap": {"class": "VWAP", "params": {"period": 144}},
                "vwap_std": {"class": "VWAPStd", "params": {"period": 144}},
            },
            "computed": {"vwap_zscore": "(candle.close - vwap) / vwap_std"},
        },
    },
    "filters": {
        "hmm_filter": {
            "source": "workbench-builtin",
            "description": "Skip trades during crisis regimes (HMM-detected, self-contained)",
            "type": "hmm",
        },
        "rsi_confirmation": {
            "source": "funding_momentum",
            "description": "Require RSI confirmation (< 40 for longs, > 60 for shorts)",
            "indicators": {"rsi": {"class": "RSI", "params": {"period": 14}}},
            "long_condition": {"indicator": "rsi", "op": "<", "value": 40.0},
            "short_condition": {"indicator": "rsi", "op": ">", "value": 60.0},
        },
        "volume_filter": {
            "source": "custom",
            "description": "Only trade when volume is 1.5x above average",
            "indicators": {"vol_ratio": {"class": "VolRatio", "params": {"period": 20}}},
            "condition": {"indicator": "vol_ratio", "op": ">", "value": 1.5},
        },
        "adx_trend": {
            "source": "custom",
            "description": "Only trade when ADX > 25 (trending market)",
            "indicators": {"adx": {"class": "ADX", "params": {"period": 14}}},
            "condition": {"indicator": "adx", "op": ">", "value": 25.0},
        },
    },
    "exit_signals": {
        "funding_normalization": {
            "source": "workbench-builtin",
            "description": "Exit when funding rate normalizes",
            "conditions": [
                {"indicator": "funding_rate", "op": "abs_below", "value": 0.000003},
            ],
        },
        "vwap_reversion": {
            "source": "vwap_dev",
            "description": "Exit when price reverts toward VWAP",
            "conditions": [
                {"indicator": "vwap_zscore", "op": "abs_below", "value": 1.5},
            ],
        },
        "time_based": {
            "source": "common",
            "description": "Exit after N candles (max hold)",
            "max_hold": 48,
        },
    },
}


# ---------------------------------------------------------------------------
# Strategy config schema
# ---------------------------------------------------------------------------

@dataclass
class Condition:
    """A single entry/exit condition."""
    indicator: str          # e.g. "funding_rate", "rsi", "price"
    op: str                 # e.g. ">", "<", "crosses_above"
    value: float | None = None   # literal threshold
    ref: str | None = None       # reference to another indicator name
    side: str | None = None      # "long", "short", or None (both)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"indicator": self.indicator, "op": self.op}
        if self.value is not None:
            d["value"] = self.value
        if self.ref is not None:
            d["ref"] = self.ref
        if self.side is not None:
            d["side"] = self.side
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Condition":
        return cls(
            indicator=d["indicator"],
            op=d["op"],
            value=d.get("value"),
            ref=d.get("ref"),
            side=d.get("side"),
        )


@dataclass
class SizingConfig:
    """Position sizing configuration — optional, opt-in to substrate.risk.

    When attached to a `StrategyConfig.sizing`, the workbench generator emits
    `rift_substrate.risk` imports and uses `size_position()` for runtime sizing.
    When `sizing` is None on the StrategyConfig, the legacy fixed-leverage
    behavior (using `leverage` + `risk_per_trade`) is preserved.

    Methods:
      "vol_target"     — scale exposure to hit `target_vol_annualized`
      "kelly"          — half-Kelly (or `kelly_fraction`) on per-period μ/σ²
                         from the strategy's recent returns
      "fixed_fraction" — constant fraction of capital per trade

    Optional position limits and drawdown control compose on top of any method.
    """

    method: str = "vol_target"

    # vol_target params
    target_vol_annualized: float = 0.15
    vol_lookback_periods: int = 60

    # kelly params
    kelly_fraction: float = 0.5
    max_base_fraction: float = 2.0

    # fixed_fraction params
    fixed_fraction: float = 0.02

    # Position limits — applied AFTER the method's base size
    max_single_position_pct: float = 0.20
    max_gross_leverage: float = 3.0
    max_net_leverage: float = 1.0

    # Drawdown control — if True, generated code wires a DrawdownController
    drawdown_control: bool = True

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "target_vol_annualized": self.target_vol_annualized,
            "vol_lookback_periods": self.vol_lookback_periods,
            "kelly_fraction": self.kelly_fraction,
            "max_base_fraction": self.max_base_fraction,
            "fixed_fraction": self.fixed_fraction,
            "max_single_position_pct": self.max_single_position_pct,
            "max_gross_leverage": self.max_gross_leverage,
            "max_net_leverage": self.max_net_leverage,
            "drawdown_control": self.drawdown_control,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SizingConfig":
        return cls(
            method=d.get("method", "vol_target"),
            target_vol_annualized=d.get("target_vol_annualized", 0.15),
            vol_lookback_periods=d.get("vol_lookback_periods", 60),
            kelly_fraction=d.get("kelly_fraction", 0.5),
            max_base_fraction=d.get("max_base_fraction", 2.0),
            fixed_fraction=d.get("fixed_fraction", 0.02),
            max_single_position_pct=d.get("max_single_position_pct", 0.20),
            max_gross_leverage=d.get("max_gross_leverage", 3.0),
            max_net_leverage=d.get("max_net_leverage", 1.0),
            drawdown_control=d.get("drawdown_control", True),
        )


@dataclass
class StrategyConfig:
    """Complete strategy definition as structured data."""
    name: str
    description: str = ""
    timeframe: str = "1h"
    entry_conditions: list[Condition] = field(default_factory=list)
    exit_conditions: list[Condition] = field(default_factory=list)
    direction: str = "both"  # "both", "long_only", "short_only"
    max_hold: int = 48
    stop_loss: float = 0.02
    risk_per_trade: float = 0.02
    leverage: float = 2.0
    filters: dict[str, bool] = field(default_factory=dict)
    # Custom indicator overrides (name → {class, params})
    indicator_overrides: dict[str, dict] = field(default_factory=dict)
    # Optional substrate-based sizing. When None, legacy fixed-leverage is used.
    sizing: SizingConfig | None = None
    # Version tracking
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        risk_dict: dict[str, Any] = {
            "stop_loss": self.stop_loss,
            "risk_per_trade": self.risk_per_trade,
            "leverage": self.leverage,
        }
        if self.sizing is not None:
            risk_dict["sizing"] = self.sizing.to_dict()

        return {
            "name": self.name,
            "description": self.description,
            "timeframe": self.timeframe,
            "entry": {
                "conditions": [c.to_dict() for c in self.entry_conditions],
                "direction": self.direction,
            },
            "exit": {
                "conditions": [c.to_dict() for c in self.exit_conditions],
                "max_hold": self.max_hold,
            },
            "risk": risk_dict,
            "filters": self.filters,
            "indicator_overrides": self.indicator_overrides,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyConfig":
        entry = d.get("entry", {})
        exit_ = d.get("exit", {})
        risk = d.get("risk", {})
        sizing_data = risk.get("sizing")
        sizing = SizingConfig.from_dict(sizing_data) if sizing_data else None

        return cls(
            name=d["name"],
            description=d.get("description", ""),
            timeframe=d.get("timeframe", "1h"),
            entry_conditions=[Condition.from_dict(c) for c in entry.get("conditions", [])],
            exit_conditions=[Condition.from_dict(c) for c in exit_.get("conditions", [])],
            direction=entry.get("direction", "both"),
            max_hold=exit_.get("max_hold", 48),
            stop_loss=risk.get("stop_loss", 0.02),
            risk_per_trade=risk.get("risk_per_trade", 0.02),
            leverage=risk.get("leverage", 2.0),
            filters=d.get("filters", {}),
            indicator_overrides=d.get("indicator_overrides", {}),
            sizing=sizing,
            version=d.get("version", 1),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
        )

    def save(self) -> Path:
        """Save config to disk."""
        CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
        path = CONFIGS_DIR / f"{self.name}.json"
        self.updated_at = time.time()
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path

    @classmethod
    def load(cls, name: str) -> "StrategyConfig":
        """Load config from disk."""
        path = CONFIGS_DIR / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"No workbench strategy named '{name}'. Saved configs: {list_configs()}")
        return cls.from_dict(json.loads(path.read_text()))

    def bump_version(self) -> None:
        """Increment version for experiment tracking."""
        self.version += 1
        self.updated_at = time.time()


def list_configs() -> list[str]:
    """List all saved strategy config names."""
    if not CONFIGS_DIR.exists():
        return []
    return sorted(p.stem for p in CONFIGS_DIR.glob("*.json"))


def delete_config(name: str) -> bool:
    """Delete a strategy config and its generated code."""
    config_path = CONFIGS_DIR / f"{name}.json"
    gen_path = GENERATED_DIR / f"{name}.py"
    deleted = False
    if config_path.exists():
        config_path.unlink()
        deleted = True
    if gen_path.exists():
        gen_path.unlink()
        deleted = True
    return deleted


# ---------------------------------------------------------------------------
# Code generator — config → valid Python strategy class
# ---------------------------------------------------------------------------

def _collect_indicators(config: StrategyConfig) -> dict[str, dict]:
    """Determine which indicators the strategy needs based on all conditions."""
    indicators: dict[str, dict] = {}

    all_conditions = config.entry_conditions + config.exit_conditions

    for cond in all_conditions:
        ind_name = cond.indicator
        ref_name = cond.ref

        # Skip internal markers (e.g., _max_hold)
        if ind_name.startswith("_"):
            continue

        # Check if indicator is a built-in state field (no declaration needed)
        if ind_name in STATE_FIELDS or ind_name in ("price", "candle_close", "candle_high", "candle_low", "candle_open"):
            pass
        elif ind_name == "vwap_zscore":
            # Computed field — needs vwap + vwap_std
            if "vwap" not in indicators:
                indicators["vwap"] = {"class": "VWAP", "params": {"period": 144}}
            if "vwap_std" not in indicators:
                indicators["vwap_std"] = {"class": "VWAPStd", "params": {"period": 144}}
        elif ind_name in INDICATOR_CATALOG:
            if ind_name not in indicators:
                indicators[ind_name] = {
                    "class": INDICATOR_CATALOG[ind_name]["class"],
                    "params": dict(INDICATOR_CATALOG[ind_name]["params"]),
                }

        # Handle reference indicators
        if ref_name and ref_name not in STATE_FIELDS:
            if ref_name in INDICATOR_CATALOG and ref_name not in indicators:
                indicators[ref_name] = {
                    "class": INDICATOR_CATALOG[ref_name]["class"],
                    "params": dict(INDICATOR_CATALOG[ref_name]["params"]),
                }

    # Add filter indicators
    if config.filters.get("rsi_confirmation") and "rsi" not in indicators:
        indicators["rsi"] = {"class": "RSI", "params": {"period": 14}}
    if config.filters.get("volume_filter") and "vol_ratio" not in indicators:
        indicators["vol_ratio"] = {"class": "VolRatio", "params": {"period": 20}}
    if config.filters.get("adx_trend") and "adx" not in indicators:
        indicators["adx"] = {"class": "ADX", "params": {"period": 14}}
    if config.filters.get("hmm_filter"):
        if "ema_trend" not in indicators and "ema" not in indicators:
            indicators["ema_trend"] = {"class": "EMA", "params": {"period": 100}}

    # Apply overrides
    for name, override in config.indicator_overrides.items():
        if name in indicators:
            indicators[name]["params"].update(override.get("params", {}))
        else:
            indicators[name] = override

    return indicators


def _condition_to_python(cond: Condition, var_prefix: str = "") -> str:
    """Convert a condition to a Python expression string."""
    ind = cond.indicator
    op = cond.op

    # Map indicator names to how they're accessed
    if ind == "price":
        left = "candle.close"
    elif ind == "candle_high":
        left = "candle.high"
    elif ind == "candle_low":
        left = "candle.low"
    elif ind == "candle_open":
        left = "candle.open"
    elif ind == "vwap_zscore":
        left = "zscore"
    elif ind in STATE_FIELDS:
        left = f"state.{ind}"
    else:
        left = f"state.{ind}"

    # Handle reference-based comparisons
    if cond.ref:
        ref = cond.ref
        if ref in STATE_FIELDS:
            right = f"state.{ref}"
        else:
            right = f"state.{ref}"
    elif cond.value is not None:
        right = repr(cond.value)
    else:
        right = "0"

    # Special operators
    if op == "abs_below":
        return f"abs({left}) < {right}"
    elif op == "crosses_above":
        return f"{left} > {right}  # crosses_above approximation"
    elif op == "crosses_below":
        return f"{left} < {right}  # crosses_below approximation"
    else:
        return f"{left} {op} {right}"


def generate_strategy_code(config: StrategyConfig) -> str:
    """Generate a complete Python strategy file from a config."""
    indicators = _collect_indicators(config)
    name = config.name
    class_name = "".join(word.capitalize() for word in name.split("_"))
    config_class_name = f"{class_name}Config"

    # Collect unique indicator classes needed for imports
    indicator_classes = set()
    for ind_info in indicators.values():
        indicator_classes.add(ind_info["class"])

    # Build imports
    imports = ["Candle", "Indicator", "Signal", "Strategy", "StrategyState", "register"]
    imports.extend(sorted(indicator_classes))

    # Determine if we need computed fields (like vwap_zscore)
    needs_zscore = any(c.indicator == "vwap_zscore" for c in config.entry_conditions + config.exit_conditions)
    needs_hmm = config.filters.get("hmm_filter", False)
    needs_substrate_sizing = config.sizing is not None

    # --- Build the strategy file ---
    lines: list[str] = []

    # Docstring
    lines.append(f'"""Auto-generated strategy: {name}')
    if config.description:
        lines.append(f"")
        lines.append(f"{config.description}")
    lines.append(f'"""')
    lines.append(f"")
    lines.append(f"from dataclasses import dataclass")
    lines.append(f"")
    lines.append(f"from rift_engine.strategy import (")
    lines.append(f"    {', '.join(sorted(imports))},")
    lines.append(f")")
    if needs_hmm:
        lines.append(f"from rift_substrate.regime import HMMRegimeDetector")
    if needs_substrate_sizing:
        lines.append(f"from rift_substrate.risk import PositionLimits, size_position")
        lines.append(f"from rift_substrate import periods_per_year_for_interval")
    lines.append(f"")

    # Config dataclass
    lines.append(f"")
    lines.append(f"@dataclass(frozen=True)")
    lines.append(f"class {config_class_name}:")

    # Risk params
    lines.append(f"    leverage: float = {config.leverage}")
    lines.append(f"    stop_loss_pct: float = {config.stop_loss}")
    lines.append(f"    max_hold_candles: int = {config.max_hold}")
    lines.append(f"    risk_per_trade: float = {config.risk_per_trade}")

    # Substrate sizing params — only emitted when config.sizing is provided
    if needs_substrate_sizing:
        s = config.sizing
        lines.append(f"    # substrate.risk sizing")
        lines.append(f"    sizing_method: str = {s.method!r}")
        lines.append(f"    target_vol_annualized: float = {s.target_vol_annualized}")
        lines.append(f"    vol_lookback_periods: int = {s.vol_lookback_periods}")
        lines.append(f"    kelly_fraction: float = {s.kelly_fraction}")
        lines.append(f"    max_base_fraction: float = {s.max_base_fraction}")
        lines.append(f"    fixed_fraction: float = {s.fixed_fraction}")
        lines.append(f"    max_single_position_pct: float = {s.max_single_position_pct}")
        lines.append(f"    max_gross_leverage: float = {s.max_gross_leverage}")
        lines.append(f"    max_net_leverage: float = {s.max_net_leverage}")

    # Extract config params from entry/exit conditions
    for cond in config.entry_conditions:
        if cond.value is not None and cond.indicator not in ("price", "candle_close", "candle_high", "candle_low", "candle_open"):
            param_name = f"{cond.indicator}_threshold"
            if cond.side:
                param_name = f"{cond.indicator}_{cond.side}_threshold"
            lines.append(f"    {param_name}: float = {cond.value}")

    for cond in config.exit_conditions:
        if cond.value is not None and cond.indicator not in ("price",):
            param_name = f"{cond.indicator}_exit"
            lines.append(f"    {param_name}: float = {cond.value}")

    # Indicator period params
    for ind_name, ind_info in indicators.items():
        for param_name, param_val in ind_info["params"].items():
            lines.append(f"    {ind_name}_{param_name}: int = {param_val}" if isinstance(param_val, int) else f"    {ind_name}_{param_name}: float = {param_val}")

    # Filter thresholds
    if config.filters.get("rsi_confirmation"):
        lines.append(f"    rsi_oversold: float = 40.0")
        lines.append(f"    rsi_overbought: float = 60.0")
    if config.filters.get("volume_filter"):
        lines.append(f"    volume_threshold: float = 1.5")
    if config.filters.get("adx_trend"):
        lines.append(f"    adx_threshold: float = 25.0")

    if needs_hmm:
        lines.append(f"    n_states: int = 3")
        lines.append(f"    train_window: int = 720")
        lines.append(f"    retrain_interval: int = 168")
        lines.append(f"    n_restarts: int = 10")
        lines.append(f"    vol_window: int = 24")

    lines.append(f"")
    lines.append(f"")

    # Strategy class
    lines.append(f'@register("{name}")')
    lines.append(f"class {class_name}(Strategy):")
    lines.append(f'    """{config.description or f"Custom strategy: {name}"}"""')
    lines.append(f"")
    lines.append(f"    config_class = {config_class_name}")
    lines.append(f'    default_interval = "{config.timeframe}"')
    if needs_hmm:
        # HMM strategies need a longer walk-forward training window so the
        # regime model has enough data to converge. Declarative override of
        # the base Strategy default (2 train / 1 test).
        lines.append(f"    recommended_train_months = 4")
        lines.append(f"    recommended_test_months = 2")
    lines.append(f"")
    lines.append(f"    def __init__(self, config=None):")
    lines.append(f"        super().__init__(config)")
    lines.append(f"        self._hold_count = 0")
    if needs_hmm:
        lines.append(f"        self._closes = []")
        lines.append(f"        self._funding = []")
        lines.append(f"        self._candles_since_train = 0")
        lines.append(f"        self._hmm = HMMRegimeDetector(")
        lines.append(f"            n_states=self.config.n_states,")
        lines.append(f"            n_restarts=self.config.n_restarts,")
        lines.append(f"            vol_window=self.config.vol_window,")
        lines.append(f"        )")
    if needs_substrate_sizing:
        lines.append(f"        # Rolling buffer for substrate-aware sizing (close-to-close returns)")
        lines.append(f"        self._sizing_prev_close: float = 0.0")
        lines.append(f"        self._sizing_returns: list[float] = []")
        lines.append(f"        self._sizing_periods_per_year: float = periods_per_year_for_interval({config.timeframe!r})")
    lines.append(f"")

    # on_candle method
    lines.append(f"    def on_candle(self, candle: Candle, state: StrategyState) -> Signal | None:")

    # Warmup guards
    warmup_vars = []
    for ind_name in indicators:
        warmup_vars.append(ind_name)
    if warmup_vars:
        lines.append(f"        import math as _math")
        for v in warmup_vars:
            lines.append(f"        {v} = state.{v}")
        lines.append(f"")
        # Check raw indicator dict for NaN (state.__getattr__ converts NaN→0.0,
        # but we need to distinguish real zeros from unwarmed indicators)
        warmup_checks = " or ".join(f'_math.isnan(state.indicators.get("{v}", float("nan")))' for v in warmup_vars)
        lines.append(f"        if {warmup_checks}:")
        lines.append(f"            return None")
        lines.append(f"")

    # Computed fields
    if needs_zscore:
        lines.append(f"        # Compute VWAP z-score")
        lines.append(f"        vwap = state.vwap")
        lines.append(f"        vwap_std = state.vwap_std")
        lines.append(f"        if vwap_std > 0:")
        lines.append(f"            zscore = (candle.close - vwap) / vwap_std")
        lines.append(f"        else:")
        lines.append(f"            return None")
        lines.append(f"")

    # HMM regime detection — composed via substrate primitive
    if needs_hmm:
        lines.append(f"        # HMM regime detection (delegated to rift_substrate.regime.HMMRegimeDetector)")
        lines.append(f"        self._closes.append(candle.close)")
        lines.append(f"        self._funding.append(state.funding_rate)")
        lines.append(f"        max_history = self.config.train_window * 2")
        lines.append(f"        if len(self._closes) > max_history:")
        lines.append(f"            self._closes = self._closes[-max_history:]")
        lines.append(f"            self._funding = self._funding[-max_history:]")
        lines.append(f"        self._candles_since_train += 1")
        lines.append(f"")
        lines.append(f"        if len(self._closes) >= self.config.train_window:")
        lines.append(f"            window_closes = self._closes[-self.config.train_window:]")
        lines.append(f"            window_funding = self._funding[-self.config.train_window:]")
        lines.append(f"            if not self._hmm.trained or self._candles_since_train >= self.config.retrain_interval:")
        lines.append(f"                if self._hmm.fit(window_closes, window_funding):")
        lines.append(f"                    self._candles_since_train = 0")
        lines.append(f"            regime = self._hmm.predict_regime(window_closes, window_funding)")
        lines.append(f"            if regime == 'crisis':")
        lines.append(f"                if state.position != 0:")
        lines.append(f"                    return Signal.close()")
        lines.append(f"                return None")
        lines.append(f"")

    # Hold counter
    lines.append(f"        # Track hold duration")
    lines.append(f"        if state.position != 0:")
    lines.append(f"            self._hold_count += 1")
    lines.append(f"        else:")
    lines.append(f"            self._hold_count = 0")
    lines.append(f"")

    # Substrate sizing — track recent close-to-close returns for vol-target / Kelly
    if needs_substrate_sizing:
        lines.append(f"        # Track recent returns for substrate-aware position_size()")
        lines.append(f"        if self._sizing_prev_close > 0 and candle.close > 0:")
        lines.append(f"            r = (candle.close - self._sizing_prev_close) / self._sizing_prev_close")
        lines.append(f"            self._sizing_returns.append(r)")
        lines.append(f"            if len(self._sizing_returns) > 500:")
        lines.append(f"                self._sizing_returns = self._sizing_returns[-500:]")
        lines.append(f"        self._sizing_prev_close = candle.close")
        lines.append(f"")

    # Exit conditions
    lines.append(f"        # Exit conditions")
    lines.append(f"        if state.position != 0:")
    lines.append(f"            if self._hold_count >= self.config.max_hold_candles:")
    lines.append(f"                return Signal.close()")

    for cond in config.exit_conditions:
        if cond.indicator.startswith("_"):
            continue  # Skip internal markers
        expr = _condition_to_python(cond)
        if cond.side == "long":
            lines.append(f"            if state.position > 0 and {expr}:")
        elif cond.side == "short":
            lines.append(f"            if state.position < 0 and {expr}:")
        else:
            lines.append(f"            if {expr}:")
        lines.append(f"                return Signal.close()")

    lines.append(f"")

    # Entry conditions
    lines.append(f"        # Entry conditions")
    lines.append(f"        if state.position == 0:")

    # Build filter checks
    filter_checks: list[str] = []
    if config.filters.get("rsi_confirmation"):
        # RSI filter is side-dependent, handled below
        pass
    if config.filters.get("volume_filter"):
        filter_checks.append(f"state.vol_ratio > self.config.volume_threshold")
    if config.filters.get("adx_trend"):
        filter_checks.append(f"state.adx > self.config.adx_threshold")

    # Separate long and short conditions
    long_conds = [c for c in config.entry_conditions if c.side in (None, "long")]
    short_conds = [c for c in config.entry_conditions if c.side in (None, "short")]

    if config.direction in ("both", "long_only"):
        long_exprs = [_condition_to_python(c) for c in long_conds]
        if config.filters.get("rsi_confirmation"):
            long_exprs.append("state.rsi < self.config.rsi_oversold")
        long_exprs.extend(filter_checks)

        if long_exprs:
            combined = " and ".join(long_exprs)
            lines.append(f"            # Long entry")
            lines.append(f"            if {combined}:")
            lines.append(f"                return Signal.long(size=self.position_size(), sl=self.config.stop_loss_pct)")
        lines.append(f"")

    if config.direction in ("both", "short_only"):
        short_exprs = [_condition_to_python(c) for c in short_conds]
        if config.filters.get("rsi_confirmation"):
            short_exprs.append("state.rsi > self.config.rsi_overbought")
        short_exprs.extend(filter_checks)

        if short_exprs:
            combined = " and ".join(short_exprs)
            lines.append(f"            # Short entry")
            lines.append(f"            if {combined}:")
            lines.append(f"                return Signal.short(size=self.position_size(), sl=self.config.stop_loss_pct)")
        lines.append(f"")

    lines.append(f"        return None")
    lines.append(f"")

    # Override position_size() when substrate sizing is enabled
    if needs_substrate_sizing:
        lines.append(f"    def position_size(self) -> float:")
        lines.append(f"        \"\"\"Substrate-aware sizing. Returns a fraction of capital in [0, max_base_fraction].")
        lines.append(f"")
        lines.append(f"        Methods (config.sizing_method):")
        lines.append(f"          'vol_target'     — scale to target_vol_annualized using rolling realized vol")
        lines.append(f"          'kelly'          — half-Kelly (or kelly_fraction) on rolling μ, σ² of asset returns")
        lines.append(f"          'fixed_fraction' — constant fixed_fraction of capital")
        lines.append(f"        \"\"\"")
        lines.append(f"        c = self.config")
        lines.append(f"        if len(self._sizing_returns) < max(10, c.vol_lookback_periods // 4):")
        lines.append(f"            return min(c.fixed_fraction, c.max_single_position_pct)  # warmup")
        lines.append(f"        kelly_mu = sum(self._sizing_returns) / len(self._sizing_returns) if c.sizing_method == 'kelly' else 0.0")
        lines.append(f"        kelly_var = (sum((r - kelly_mu) ** 2 for r in self._sizing_returns) / max(1, len(self._sizing_returns) - 1)) if c.sizing_method == 'kelly' else 0.0")
        lines.append(f"        result = size_position(")
        lines.append(f"            side=1,  # positive size; engine handles long/short via Signal.long/short")
        lines.append(f"            capital_usd=1.0,  # return a FRACTION; engine multiplies by equity")
        lines.append(f"            method=c.sizing_method,")
        lines.append(f"            returns=self._sizing_returns,")
        lines.append(f"            target_vol_annualized=c.target_vol_annualized,")
        lines.append(f"            periods_per_year=self._sizing_periods_per_year,")
        lines.append(f"            vol_lookback_periods=c.vol_lookback_periods,")
        lines.append(f"            expected_return_per_period=kelly_mu,")
        lines.append(f"            variance_per_period=kelly_var,")
        lines.append(f"            kelly_fraction=c.kelly_fraction,")
        lines.append(f"            fixed_fraction=c.fixed_fraction,")
        lines.append(f"            max_base_fraction=c.max_base_fraction,")
        lines.append(f"            limits=PositionLimits(")
        lines.append(f"                max_single_position_pct=c.max_single_position_pct,")
        lines.append(f"                max_gross_leverage=c.max_gross_leverage,")
        lines.append(f"                max_net_leverage=c.max_net_leverage,")
        lines.append(f"            ),")
        lines.append(f"        )")
        lines.append(f"        return abs(result.position_fraction)")
        lines.append(f"")

    # indicators() method
    lines.append(f"    def indicators(self) -> dict[str, Indicator]:")
    lines.append(f"        return {{")
    for ind_name, ind_info in indicators.items():
        cls_name = ind_info["class"]
        params = ind_info["params"]
        param_str = ", ".join(
            f"{k}=self.config.{ind_name}_{k}" for k in params
        )
        lines.append(f'            "{ind_name}": {cls_name}({param_str}),')
    lines.append(f"        }}")
    lines.append(f"")

    return "\n".join(lines)


def generate_and_save(config: StrategyConfig) -> Path:
    """Generate strategy code from config and save both to disk."""
    # Save config
    config.save()

    # Generate code
    code = generate_strategy_code(config)

    # Save generated strategy file
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    path = GENERATED_DIR / f"{config.name}.py"
    path.write_text(code)

    return path


# ---------------------------------------------------------------------------
# Experiment log — SQLite
# ---------------------------------------------------------------------------

def _init_db() -> sqlite3.Connection:
    """Initialize the experiments database."""
    WORKBENCH_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(EXPERIMENTS_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_name TEXT NOT NULL,
            version INTEGER NOT NULL,
            pair TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            config_json TEXT NOT NULL,
            return_pct REAL,
            sharpe REAL,
            num_trades INTEGER,
            win_rate REAL,
            max_drawdown REAL,
            profit_factor REAL,
            total_funding REAL,
            change_description TEXT,
            timestamp REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def log_experiment(
    strategy_name: str,
    version: int,
    pair: str,
    timeframe: str,
    config: dict,
    results: dict,
    change_description: str = "",
) -> int:
    """Log an experiment result. Returns the experiment ID."""
    conn = _init_db()
    cursor = conn.execute(
        """INSERT INTO experiments
           (strategy_name, version, pair, timeframe, config_json,
            return_pct, sharpe, num_trades, win_rate, max_drawdown,
            profit_factor, total_funding, change_description, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            strategy_name,
            version,
            pair,
            timeframe,
            json.dumps(config),
            results.get("total_return_pct"),
            results.get("sharpe_ratio"),
            results.get("num_trades"),
            results.get("win_rate"),
            results.get("max_drawdown_pct"),
            results.get("profit_factor"),
            results.get("total_funding", 0),
            change_description,
            time.time(),
        ),
    )
    conn.commit()
    exp_id = cursor.lastrowid
    conn.close()
    return exp_id or 0


def get_experiments(strategy_name: str, limit: int = 20) -> list[dict]:
    """Get recent experiments for a strategy."""
    conn = _init_db()
    rows = conn.execute(
        """SELECT id, version, pair, timeframe, return_pct, sharpe,
                  num_trades, win_rate, max_drawdown, profit_factor,
                  total_funding, change_description, timestamp
           FROM experiments
           WHERE strategy_name = ?
           ORDER BY id DESC LIMIT ?""",
        (strategy_name, limit),
    ).fetchall()
    conn.close()

    return [
        {
            "id": r[0], "version": r[1], "pair": r[2], "timeframe": r[3],
            "return_pct": r[4], "sharpe": r[5], "num_trades": r[6],
            "win_rate": r[7], "max_drawdown": r[8], "profit_factor": r[9],
            "total_funding": r[10], "change_description": r[11],
            "timestamp": r[12],
        }
        for r in rows
    ]


def get_last_experiment(strategy_name: str, pair: str | None = None) -> dict | None:
    """Get the most recent experiment for comparison."""
    conn = _init_db()
    if pair:
        row = conn.execute(
            """SELECT return_pct, sharpe, num_trades, win_rate, max_drawdown, profit_factor
               FROM experiments
               WHERE strategy_name = ? AND pair = ?
               ORDER BY id DESC LIMIT 1""",
            (strategy_name, pair),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT return_pct, sharpe, num_trades, win_rate, max_drawdown, profit_factor
               FROM experiments
               WHERE strategy_name = ?
               ORDER BY id DESC LIMIT 1""",
            (strategy_name,),
        ).fetchone()
    conn.close()

    if row is None:
        return None

    return {
        "return_pct": row[0], "sharpe": row[1], "num_trades": row[2],
        "win_rate": row[3], "max_drawdown": row[4], "profit_factor": row[5],
    }


def get_experiment_config(experiment_id: int) -> dict | None:
    """Load a specific experiment's config (for reverting)."""
    conn = _init_db()
    row = conn.execute(
        "SELECT config_json FROM experiments WHERE id = ?",
        (experiment_id,),
    ).fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return None


def get_experiment_by_id(experiment_id: int) -> dict | None:
    """Load full experiment data by ID."""
    conn = _init_db()
    row = conn.execute(
        "SELECT id, strategy_name, pair, timeframe, config_json, "
        "return_pct, sharpe, num_trades, win_rate, max_drawdown, profit_factor, "
        "total_funding, change_description, timestamp FROM experiments WHERE id = ?",
        (experiment_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0], "strategy_name": row[1], "pair": row[2], "timeframe": row[3],
        "config_json": row[4], "return_pct": row[5], "sharpe": row[6],
        "num_trades": row[7], "win_rate": row[8], "max_drawdown": row[9],
        "profit_factor": row[10], "total_funding": row[11],
        "change_description": row[12], "timestamp": row[13],
    }


# ---------------------------------------------------------------------------
# Template configs — starting points for new strategies
# ---------------------------------------------------------------------------

# Generic scaffolds only — no strategy-style opinions ship in the engine.
# For worked example strategies, see `rift_strategies_sdk/examples/` (e.g. trend_follow).
TEMPLATES = {
    "blank": StrategyConfig(
        name="",
        description="Empty template — build from scratch",
        timeframe="1h",
        entry_conditions=[],
        exit_conditions=[],
        max_hold=48,
        stop_loss=0.02,
        leverage=2.0,
    ),
    "single_signal_example": StrategyConfig(
        name="",
        description=(
            "Minimal single-signal scaffold — RSI mean reversion as a learning template. "
            "Replace conditions with your own; not a deployable strategy."
        ),
        timeframe="1h",
        entry_conditions=[
            Condition(indicator="rsi", op="<", value=30.0, side="long"),
            Condition(indicator="rsi", op=">", value=70.0, side="short"),
        ],
        exit_conditions=[
            Condition(indicator="rsi", op=">", value=50.0, side="long"),
            Condition(indicator="rsi", op="<", value=50.0, side="short"),
        ],
        max_hold=24,
        stop_loss=0.02,
        leverage=1.0,
    ),
}


def create_from_template(template_name: str, strategy_name: str) -> StrategyConfig:
    """Create a new strategy config from a template."""
    import re
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]{0,63}$', strategy_name):
        raise ValueError(f"Invalid strategy name '{strategy_name}'. Use letters, numbers, and underscores only.")
    if template_name not in TEMPLATES:
        raise ValueError(f"Unknown template: {template_name}. Available: {list(TEMPLATES.keys())}")

    template = TEMPLATES[template_name]
    config = StrategyConfig.from_dict(template.to_dict())
    config.name = strategy_name
    config.created_at = time.time()
    config.updated_at = time.time()
    config.version = 1
    return config
