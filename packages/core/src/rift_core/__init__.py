"""RIFT core — shared types, schema, config, NDJSON output protocol.

This package is the foundation every other rift_* package depends on.
It has no rift-specific dependencies of its own.

Public surface:
- schema: normalize_coin, coin_to_path, path_to_coin, FILL_SCHEMA, detect_market
- config: load_env, get_env_var, set_env_var, load_config, save_config, ...
- output: emit (NDJSON to stdout with sanitization)
- versioning: StrategyVersion, record_version, get_version_history
- keys: Actor, MainWalletRef, APIWalletKey, TokenScope, AuthorizationToken (Phase 0)
- _internal: internal-only utilities
"""

from rift_core.config import (
    CONFIG_DIR,
    CONFIG_PATH,
    ENV_PATH,
    clear_proxy,
    get_env_var,
    get_proxy,
    load_config,
    load_env,
    parse_duration,
    save_config,
    set_env_var,
    set_proxy,
)
from rift_core.keys import (
    ANY,
    Actor,
    ActorKind,
    APIWalletKey,
    AuthorizationToken,
    MainWalletRef,
    Network,
    TokenScope,
    TradeAction,
    TradeSide,
)
from rift_core.output import emit, sanitize_for_json
from rift_core.schema import (
    FILL_SCHEMA,
    coin_to_path,
    detect_market,
    normalize_coin,
    normalize_spot,
    path_to_coin,
)
from rift_core.versioning import (
    StrategyVersion,
    diff_versions,
    get_current_version,
    get_version_history,
    record_version,
)

__all__ = [
    "ANY",
    "APIWalletKey",
    "Actor",
    "ActorKind",
    "AuthorizationToken",
    "CONFIG_DIR",
    "CONFIG_PATH",
    "ENV_PATH",
    "FILL_SCHEMA",
    "MainWalletRef",
    "Network",
    "StrategyVersion",
    "TokenScope",
    "TradeAction",
    "TradeSide",
    "clear_proxy",
    "coin_to_path",
    "detect_market",
    "diff_versions",
    "emit",
    "get_current_version",
    "get_env_var",
    "get_proxy",
    "get_version_history",
    "load_config",
    "load_env",
    "normalize_coin",
    "normalize_spot",
    "parse_duration",
    "path_to_coin",
    "record_version",
    "sanitize_for_json",
    "save_config",
    "set_env_var",
    "set_proxy",
]
