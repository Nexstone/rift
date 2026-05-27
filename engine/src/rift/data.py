"""Re-export shim — moved to rift_data in Phase 2 of the refactor.

Schema helpers (normalize_coin, coin_to_path, etc.) come from rift_core.
REST API and parquet I/O come from rift_data.data.
New code should import from rift_core/rift_data directly.
"""

from rift_core.schema import (
    VALID_INTERVALS,
    _KNOWN_TRADFI,
    coin_to_path,
    detect_market,
    normalize_coin,
    normalize_spot,
    path_to_coin,
)
from rift_data.data import (
    DEFAULT_DATA_DIR,
    fetch_borrow_rates,
    fetch_candles,
    fetch_cross_exchange_funding,
    fetch_funding_rates,
    fetch_market_breadth,
    fetch_market_context,
    fetch_oi_cap_assets,
    fetch_predicted_funding,
    get_info_client,
    list_cached_data,
    load_candles,
    load_funding_rates,
    load_market_snapshots,
    load_orderbook_snapshots,
    save_candles,
    save_funding_rates,
    save_market_snapshot,
)

__all__ = [
    # schema (from rift_core)
    "VALID_INTERVALS",
    "_KNOWN_TRADFI",
    "coin_to_path",
    "detect_market",
    "normalize_coin",
    "normalize_spot",
    "path_to_coin",
    # rest + io (from rift_data)
    "DEFAULT_DATA_DIR",
    "fetch_borrow_rates",
    "fetch_candles",
    "fetch_cross_exchange_funding",
    "fetch_funding_rates",
    "fetch_market_breadth",
    "fetch_market_context",
    "fetch_oi_cap_assets",
    "fetch_predicted_funding",
    "get_info_client",
    "list_cached_data",
    "load_candles",
    "load_funding_rates",
    "load_market_snapshots",
    "load_orderbook_snapshots",
    "save_candles",
    "save_funding_rates",
    "save_market_snapshot",
]
