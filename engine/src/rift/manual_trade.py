"""Re-export shim — moved to rift_trade.manual_trade in Phase 4 of the refactor."""

from rift_trade.manual_trade import *  # noqa: F401, F403
import rift_trade.manual_trade as _mod

globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
