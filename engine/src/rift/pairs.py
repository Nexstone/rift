"""Re-export shim — moved to rift_portfolio.pairs in Phase 5 of the refactor."""

from rift_portfolio.pairs import *  # noqa: F401, F403
import rift_portfolio.pairs as _mod

globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
