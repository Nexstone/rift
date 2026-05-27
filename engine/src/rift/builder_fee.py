"""Re-export shim — moved to rift_trade.builder_fee in Phase 4 of the refactor."""

from rift_trade.builder_fee import *  # noqa: F401, F403
import rift_trade.builder_fee as _mod

globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
