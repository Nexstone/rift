"""Re-export shim — moved to rift_engine.strategy in Phase 3 of the refactor."""

from rift_engine.strategy import *  # noqa: F401, F403
import rift_engine.strategy as _mod

# Re-export everything (including private names that consumers might use)
globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
