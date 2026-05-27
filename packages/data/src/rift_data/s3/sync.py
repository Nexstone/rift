"""Per-day sync orchestrator: download → parse → aggregate → write → drop.

See rift_data.s3 package docstring for the streaming, memory-bounded design.
"""

from __future__ import annotations

import json
import time as _time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import polars as pl

from rift_data.s3.aggregate import fills_to_candles, flush_candle_buffers
from rift_data.s3.download import DEFAULT_START, get_s3_client
from rift_data.s3.parse import _fills_list_to_df, parse_hour_file


def sync_coins(
    coins: list[str],
    timeframes: list[str],
    start_date: str = DEFAULT_START,
    end_date: str = "",
    include_funding: bool = True,
    incremental: bool = True,
    on_progress: Callable | None = None,
    max_download_workers: int = 6,
    max_parse_workers: int = 4,
    checkpoint_every_days: int = 30,
) -> dict[str, dict]:
    """Bulk sync multiple coins from Hyperliquid S3 with streaming-to-disk fills."""
    from rift_core.schema import coin_to_path, normalize_coin
    from rift_data.data import DEFAULT_DATA_DIR, save_funding_rates
    from rift_data.s3.download import download_hour
    from rift_data.s3.funding import sync_funding

    # Normalize coins → S3-side names (uppercase, no xyz: prefix)
    coin_map: dict[str, str] = {}
    for c in coins:
        norm = normalize_coin(c)
        s3_name = norm.split(":")[-1] if ":" in norm else norm
        coin_map[norm] = s3_name.upper()
    s3_coins: set[str] = set(coin_map.values())
    s3_to_norm = {v: k for k, v in coin_map.items()}
    s3_coins_list = sorted(s3_coins)  # picklable for ProcessPool

    if not end_date:
        end_date = datetime.utcnow().strftime("%Y-%m-%d")

    # Per-coin effective start (incremental)
    effective_starts: dict[str, str] = {}
    for norm_coin in coin_map:
        meta_path = DEFAULT_DATA_DIR / coin_to_path(norm_coin) / "_sync_meta.json"
        if incremental and meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                effective_starts[norm_coin] = meta.get("last_sync_date", start_date)
            except Exception:
                effective_starts[norm_coin] = start_date
        else:
            effective_starts[norm_coin] = start_date

    global_start = min(effective_starts.values())
    start_dt = datetime.strptime(global_start, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    total_days = (end_dt - start_dt).days + 1

    if on_progress:
        on_progress(
            f"Syncing {len(coins)} coins from {global_start} to {end_date} "
            f"({total_days} days, {max_download_workers} dl / {max_parse_workers} parse workers)"
        )

    s3 = get_s3_client()

    results: dict[str, dict] = {
        norm: {"candles": {}, "funding": 0, "fills": 0} for norm in coin_map
    }
    candle_buffers: dict[tuple[str, str], list[pl.DataFrame]] = {}
    days_processed = 0
    total_fills_seen = 0

    dl_pool = ThreadPoolExecutor(max_workers=max_download_workers)
    parse_pool = ProcessPoolExecutor(max_workers=max_parse_workers)

    try:
        current = start_dt
        while current <= end_dt:
            date_str = current.strftime("%Y%m%d")
            day_start_t = _time.time()

            dl_futures = {
                dl_pool.submit(download_hour, s3, date_str, h): h
                for h in range(24)
            }
            cache_paths: list[Path] = []
            for fut in as_completed(dl_futures):
                p = fut.result()
                if p is not None:
                    cache_paths.append(p)

            day_fills: dict[str, list] = {c: [] for c in s3_coins}
            if cache_paths:
                parse_futures = [
                    parse_pool.submit(parse_hour_file, str(p), s3_coins_list)
                    for p in cache_paths
                ]
                for pf in as_completed(parse_futures):
                    try:
                        extracted = pf.result()
                    except Exception as e:
                        if on_progress:
                            on_progress(f"  parse error: {e}")
                        continue
                    for s3_coin, fills_list in extracted.items():
                        if fills_list:
                            day_fills[s3_coin].extend(fills_list)

            day_fills_count = sum(len(v) for v in day_fills.values())
            total_fills_seen += day_fills_count

            for s3_coin, fills_list in day_fills.items():
                if not fills_list:
                    continue
                norm_coin = s3_to_norm[s3_coin]
                fills_df = _fills_list_to_df(fills_list).sort("timestamp")

                fills_dir = DEFAULT_DATA_DIR / coin_to_path(norm_coin) / "fills"
                fills_dir.mkdir(parents=True, exist_ok=True)
                day_path = fills_dir / f"{date_str}.parquet"
                fills_df.write_parquet(day_path)

                results[norm_coin]["fills"] += len(fills_df)

                for tf in timeframes:
                    day_candles = fills_to_candles(fills_df, tf)
                    if len(day_candles) > 0:
                        candle_buffers.setdefault((norm_coin, tf), []).append(day_candles)

                del fills_df

            day_fills.clear()

            days_processed += 1
            elapsed = _time.time() - day_start_t
            if on_progress:
                on_progress(
                    f"Day {current.strftime('%Y-%m-%d')}: "
                    f"{day_fills_count:,} fills ({elapsed:.1f}s) — "
                    f"total {total_fills_seen:,} "
                    f"({days_processed}/{total_days} days)"
                )

            if days_processed % checkpoint_every_days == 0:
                if on_progress:
                    on_progress(f"Checkpointing candles after {days_processed} days...")
                flush_candle_buffers(candle_buffers, on_progress)

            current += timedelta(days=1)

        if on_progress:
            on_progress("Final candle flush...")
        flush_candle_buffers(candle_buffers, on_progress)

    finally:
        parse_pool.shutdown(wait=True)
        dl_pool.shutdown(wait=True)

    # Tally candle counts for the result (read back from disk — small).
    from rift_data.data import load_candles
    for norm_coin in coin_map:
        for tf in timeframes:
            df = load_candles(norm_coin, tf)
            if df is not None:
                results[norm_coin]["candles"][tf] = len(df)

    if include_funding:
        for norm_coin, s3_name in coin_map.items():
            if on_progress:
                on_progress(f"{norm_coin}: syncing funding rates...")
            funding = sync_funding(s3_name, global_start, end_date, on_progress)
            if len(funding) > 0:
                save_funding_rates(funding, norm_coin)
                results[norm_coin]["funding"] = len(funding)

    for norm_coin in coin_map:
        meta_path = DEFAULT_DATA_DIR / coin_to_path(norm_coin) / "_sync_meta.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps({
            "last_sync_date": end_date,
            "last_sync_timestamp": int(_time.time() * 1000),
            "total_fills": results[norm_coin]["fills"],
            "source": "s3",
            "layout": "fills_partitioned_daily",
        }))

    return results


def sync_coin(
    coin: str,
    timeframes: list[str],
    start_date: str = DEFAULT_START,
    end_date: str = "",
    include_funding: bool = True,
    incremental: bool = True,
    on_progress: Callable | None = None,
) -> dict:
    """Single-coin wrapper around sync_coins."""
    results = sync_coins(
        [coin], timeframes, start_date, end_date,
        include_funding, incremental, on_progress,
    )
    for v in results.values():
        return v
    return {"candles": {}, "funding": 0, "fills": 0}
