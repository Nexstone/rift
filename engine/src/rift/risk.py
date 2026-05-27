"""Re-export shim — moved to rift_trade.risk in Phase 4 of the refactor."""

from rift_trade.risk import *  # noqa: F401, F403
import rift_trade.risk as _mod

globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
