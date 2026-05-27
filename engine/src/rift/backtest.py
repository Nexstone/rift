"""Re-export shim — moved to rift_engine.backtest in Phase 3 of the refactor."""

from rift_engine.backtest import *  # noqa: F401, F403
import rift_engine.backtest as _mod

globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
