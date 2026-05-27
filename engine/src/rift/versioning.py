"""Re-export shim — moved to rift_core in Phase 1 of the refactor.

Public surface preserved for backwards compatibility. New code should
import from rift_core.versioning directly.
"""

from rift_core.versioning import (
    VERSIONS_FILE,
    StrategyVersion,
    diff_versions,
    get_current_version,
    get_version_history,
    record_version,
)

__all__ = [
    "VERSIONS_FILE",
    "StrategyVersion",
    "diff_versions",
    "get_current_version",
    "get_version_history",
    "record_version",
]
