"""Re-export shim — moved to rift_engine.signals.momentum in Phase 3 of the refactor."""

from rift_engine.signals.momentum import *  # noqa: F401, F403
import rift_engine.signals.momentum as _mod

globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
