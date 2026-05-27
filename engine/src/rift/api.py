"""Re-export shim — moved to rift_api.server in Phase 5 of the refactor."""

from rift_api.server import *  # noqa: F401, F403
import rift_api.server as _mod

globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
