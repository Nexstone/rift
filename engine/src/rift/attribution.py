"""Re-export shim — moved to rift_engine.attribution in Phase 3 of the refactor."""

from rift_engine.attribution import *  # noqa: F401, F403
import rift_engine.attribution as _mod

globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
