"""Re-export shim — moved to rift_core in Phase 1 of the refactor.

This module exists to keep `from rift._internal import _b2, _BUILDER_HASH`
working for code that hasn't been migrated yet. New code should import
from rift_core directly.
"""

from rift_core._internal import _b2, _BUILDER_HASH

__all__ = ["_b2", "_BUILDER_HASH"]
