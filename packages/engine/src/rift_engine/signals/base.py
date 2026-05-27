"""Signal base — registration, execution, and result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Global signal registry
_SIGNAL_REGISTRY: dict[str, 'Signal'] = {}


@dataclass
class SignalResult:
    """Output of a single signal function."""
    name: str           # signal name (e.g., "funding_extreme")
    score: float        # -1.0 (strong short) to +1.0 (strong long), 0.0 = no opinion
    reason: str         # human-readable explanation
    category: str       # funding, momentum, microstructure, volatility, cross_pair, seasonality
    confidence: float   # 0.0 to 1.0 — how reliable this signal has been historically


@dataclass
class Signal:
    """A registered signal with metadata."""
    name: str
    category: str
    description: str
    func: Callable      # (coin: str, state: dict) -> SignalResult
    weight: float = 1.0 # default weight in aggregation


def signal(name: str, category: str, description: str = "", weight: float = 1.0):
    """Decorator to register a signal function.

    Usage:
        @signal("funding_extreme", "funding", "Detects extreme funding rates")
        def funding_extreme(coin: str, state: dict) -> SignalResult:
            ...
    """
    def decorator(func: Callable) -> Callable:
        sig = Signal(
            name=name,
            category=category,
            description=description or func.__doc__ or name,
            func=func,
            weight=weight,
        )
        _SIGNAL_REGISTRY[name] = sig
        return func
    return decorator


def get_all_signals() -> dict[str, Signal]:
    """Return all registered signals."""
    return dict(_SIGNAL_REGISTRY)


def compute_all_signals(coin: str, state: dict) -> list[SignalResult]:
    """Run all registered signals on a coin + state. Returns list of results."""
    results = []
    for sig in _SIGNAL_REGISTRY.values():
        try:
            result = sig.func(coin, state)
            if result and result.score != 0:
                results.append(result)
        except Exception:
            pass  # Signal errors should never crash the system
    return results
