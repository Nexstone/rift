"""Re-export shim — moved to rift_data.s3 in Phase 2 of the refactor.

The old s3_data.py monolith was split into rift_data/s3/{download,parse,
aggregate,sync,funding}.py. Public surface preserved for backwards
compatibility; new code should import from rift_data.s3 directly.
"""

from rift_data.s3 import (
    DEFAULT_START,
    RAW_CACHE_DIR,
    S3_ARCHIVE_BUCKET,
    S3_FILLS_BUCKET,
    check_aws_credentials,
    fills_to_candles,
    sync_coin,
    sync_coins,
    sync_funding,
)
from rift_data.s3.parse import _extract_coin_fills, _fills_list_to_df
from rift_core.schema import FILL_SCHEMA

__all__ = [
    "DEFAULT_START",
    "FILL_SCHEMA",
    "RAW_CACHE_DIR",
    "S3_ARCHIVE_BUCKET",
    "S3_FILLS_BUCKET",
    "_extract_coin_fills",
    "_fills_list_to_df",
    "check_aws_credentials",
    "fills_to_candles",
    "sync_coin",
    "sync_coins",
    "sync_funding",
]
