"""Re-export shim — moved to rift_engine.output in Phase 3 of the refactor."""

from rift_engine.output import *  # noqa: F401, F403
import rift_engine.output as _mod

globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
