"""Re-export shim — moved to rift_research.research in Phase 5 of the refactor."""

from rift_research.research import *  # noqa: F401, F403
import rift_research.research as _mod

globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
