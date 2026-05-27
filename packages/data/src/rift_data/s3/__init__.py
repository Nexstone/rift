"""Hyperliquid S3 archive sync — streaming, memory-bounded.

Split across submodules by stage:
  download.py   — hourly lz4 fetch + on-disk cache (atomic tmp+rename)
  parse.py      — JSONL → per-coin fill tuples (ProcessPool worker)
  aggregate.py  — fills → OHLCV candles with order-flow columns
  sync.py       — per-day orchestrator (sync_coins, sync_coin)
  funding.py    — funding-rate sync from hyperliquid-archive bucket

Public API re-exported here.
"""

from rift_data.s3.aggregate import fills_to_candles
from rift_data.s3.download import (
    DEFAULT_START,
    RAW_CACHE_DIR,
    S3_ARCHIVE_BUCKET,
    S3_FILLS_BUCKET,
    check_aws_credentials,
)
from rift_data.s3.funding import sync_funding
from rift_data.s3.parse import _extract_coin_fills, _fills_list_to_df
from rift_data.s3.sync import sync_coin, sync_coins

__all__ = [
    "DEFAULT_START",
    "RAW_CACHE_DIR",
    "S3_ARCHIVE_BUCKET",
    "S3_FILLS_BUCKET",
    "check_aws_credentials",
    "fills_to_candles",
    "sync_coin",
    "sync_coins",
    "sync_funding",
]
