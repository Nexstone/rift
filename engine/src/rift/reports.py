"""Re-export shim — moved to rift_research.reports in Phase 5 of the refactor."""

from rift_research.reports import *  # noqa: F401, F403
import rift_research.reports as _mod

globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
