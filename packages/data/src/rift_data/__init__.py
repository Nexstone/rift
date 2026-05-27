"""RIFT data — ingestion and cache for Hyperliquid archive, REST API, websocket.

Storage layout under ~/.rift/data/{coin}/:
  fills/{YYYYMMDD}.parquet      ← raw tick-level fills, partitioned daily
  {tf}/candles.parquet          ← aggregated OHLCV per timeframe
  funding/rates.parquet         ← funding rate history
  historical/*.parquet          ← optional derivatives history (OI, L/S ratio,
                                  liquidations) populated by an upstream
                                  ingestion process; read-only here
  _sync_meta.json                ← last-sync timestamp, layout version

Public API:
  s3.sync_coins / sync_coin / sync_funding   — bulk S3 archive sync
  rest.fetch_candles / fetch_funding_rates   — Hyperliquid REST API
  historical.load_candles / load_fills / scan_fills — read from local cache
  data.save_candles / save_funding_rates     — write to local cache
  coinalyze.load_hl_historical_*             — read derivatives parquet cache
"""

from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / ".rift" / "data"
