"""Re-export shim — moved to rift_portfolio.correlation_guard in Phase 5 of the refactor."""

from rift_portfolio.correlation_guard import *  # noqa: F401, F403
import rift_portfolio.correlation_guard as _mod

globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
