"""Re-export shim — moved to rift_core in Phase 1 of the refactor.

Public surface preserved for backwards compatibility. New code should
import from rift_core.config directly.
"""

from rift_core.config import (
    CONFIG_DIR,
    CONFIG_PATH,
    ENV_PATH,
    _b3,
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

__all__ = [
    "CONFIG_DIR",
    "CONFIG_PATH",
    "ENV_PATH",
    "_b3",
    "clear_proxy",
    "get_env_var",
    "get_proxy",
    "load_config",
    "load_env",
    "parse_duration",
    "save_config",
    "set_env_var",
    "set_proxy",
]
