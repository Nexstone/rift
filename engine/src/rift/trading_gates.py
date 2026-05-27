"""Re-export shim — moved to rift_trade.trading_gates in Phase 4 of the refactor."""

from rift_trade.trading_gates import *  # noqa: F401, F403
import rift_trade.trading_gates as _mod

globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
