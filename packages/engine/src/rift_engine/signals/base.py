"""Signal base — registration, execution, and result types."""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
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


# ── User-signal discovery ──────────────────────────────────────────
#
# Built-in signals self-register when their module is imported (the
# `signals/__init__.py` triggers this for the 9 bundled categories).
# User-authored signals live as standalone .py files under
# `<repo>/strategies/signals/` or `~/.rift/signals/`. They register via
# the same `@signal(...)` decorator — we just need to import the files
# so the decorator side-effect fires.
#
# Mirrors the `discover_strategies()` pattern in
# `rift_engine.strategy.discover_strategies` (same path-traversal
# protection, same underscore-prefix skip, same importlib pattern).

_VALID_SIGNAL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def load_signal_file(path: Path) -> None:
    """Import a Python file to trigger its `@signal(...)` decorators.

    Path-traversal protected: only filenames matching `[A-Za-z][\\w]*` load.
    Skips files starting with `_` (underscore prefix = "don't auto-discover").
    Failures are swallowed quietly — a broken signal file shouldn't take down
    every scout command.
    """
    if not _VALID_SIGNAL_NAME.match(path.stem):
        return
    spec = importlib.util.spec_from_file_location(f"rift.user_signals.{path.stem}", path)
    if spec is None or spec.loader is None:
        return
    try:
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    except Exception:
        # Don't crash scout on a broken user signal; just skip it.
        # (When users want to debug, they can `python -c "import their_signal"`.)
        pass


def discover_user_signals(directories: list[Path]) -> None:
    """Scan directories for `.py` signal files and load them.

    Call this once at scout invocation time, before `scan_market()`. Built-in
    signals are already registered via `signals/__init__.py`; this picks up
    user-authored signals from the configured directories.

    Files starting with `_` are skipped (same convention as the strategies
    discovery — useful for partial drafts or shared helpers).
    """
    for d in directories:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            load_signal_file(f)
