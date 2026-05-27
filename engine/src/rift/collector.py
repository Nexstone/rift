"""Re-export shim — moved to rift_data.collector in Phase 2 of the refactor."""

from rift_data.collector import (
    COLLECTOR_DB,
    COLLECTOR_LOG,
    COLLECTOR_PID,
    DATA_DIR,
    collect_candles_and_funding,
    collect_market_data,
    collect_orderbook,
    get_collector_stats,
    run_collector,
)

__all__ = [
    "COLLECTOR_DB",
    "COLLECTOR_LOG",
    "COLLECTOR_PID",
    "DATA_DIR",
    "collect_candles_and_funding",
    "collect_market_data",
    "collect_orderbook",
    "get_collector_stats",
    "run_collector",
]
