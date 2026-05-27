"""Re-export shim — moved to rift_data.historical in Phase 2 of the refactor."""

from rift_data.historical import (
    DEFAULT_DATA_DIR,
    load_candles_smart,
    load_fills,
    load_funding_smart,
    scan_fills,
)

__all__ = [
    "DEFAULT_DATA_DIR",
    "load_candles_smart",
    "load_fills",
    "load_funding_smart",
    "scan_fills",
]
