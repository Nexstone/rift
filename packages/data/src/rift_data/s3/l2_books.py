"""L2 order-book history aggregation from `hyperliquid-archive` S3 bucket.

The fills bucket (`hl-mainnet-node-data`) is a 10-month rolling window — not
enough history for hourly-and-coarser backtests. The archive bucket has a
parallel feed at `market_data/{YYYYMMDD}/{HH}/l2Book/{COIN}.lz4` going back
to **2023-04-15** (3 years). Files are per-coin per-hour, ~500 KB compressed
/ ~10 MB decompressed, containing JSON snapshots of the L2 book at ~600 ms
cadence.

This module turns those snapshots into candle parquets compatible with the
existing `~/.rift/data/{COIN}/{interval}/candles.parquet` schema.

What you get vs the fills-based sync:

  - Open / High / Low / Close — derived from the snapshot mid-prices
  - `volume`, `num_trades` — NaN (L2 shows resting orders, not executed trades)
  - PLUS extra depth-feature columns the fills-based sync doesn't produce:
      bid_depth_top10_usd, ask_depth_top10_usd  — total resting size in top
                                                  10 levels (USD-notional)
      order_book_imbalance                      — (bid - ask) / (bid + ask)
      mean_spread_bps                           — intra-bar average spread
      max_bid_wall_usd, max_ask_wall_usd        — largest single-level
                                                  resting size (USD-notional)
      n_snapshots                               — how many book snapshots
                                                  contributed to this bar

The depth columns turn book-state into a first-class signal input — order-
book imbalance is one of the strongest published microstructure signals.

Two public entry points:

  parse_l2_jsonl(path) → iter[L2Snapshot]
      Streaming parse of one decompressed hour file. Bounded memory.

  aggregate_l2_to_candles(snapshots, interval_ms, depth_levels=10) → list[dict]
      Bin snapshots into target timeframe windows, compute the per-bar
      OHLC + depth feature columns. Output dicts match the candle parquet
      schema with the new depth columns added.

`download_l2_hour()` and `sync_l2_candles()` live in the orchestrator and
wrap these primitives with S3 plumbing.

Reference:
  https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data
  Cont, Stoikov, Talreja (2010). "A stochastic model for order book
    dynamics." — foundational microstructure paper on book-state signals.
"""

from __future__ import annotations

import json
import time as _time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator


L2_ARCHIVE_BUCKET = "hyperliquid-archive"
L2_ARCHIVE_START = "2023-04-15"  # earliest date with market_data/l2Book/ files
DEFAULT_SYNC_DAYS = 90           # first-run default: last N days, not full archive

RAW_L2_CACHE_DIR = Path.home() / ".rift" / "raw_l2"


# ─── Dataclass ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class L2Snapshot:
    """One L2 book snapshot at a moment in time.

    Bids are sorted descending by price (best first). Asks ascending.
    Each level is a (price, size) tuple. Size is in BASE units; multiply
    by price to get USD-notional.
    """

    timestamp_ms: int
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0][0] + self.asks[0][0]) / 2.0

    @property
    def spread_bps(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        mid = self.mid
        if mid is None or mid <= 0:
            return None
        return (self.asks[0][0] - self.bids[0][0]) / mid * 10_000.0


# ─── Parser ──────────────────────────────────────────────────────────


def parse_l2_jsonl(path: Path | str) -> Iterator[L2Snapshot]:
    """Stream-parse an HL L2 jsonl file.

    File format (one JSON object per line):

        {"time": "<iso>", "ver_num": 1, "raw": {"channel": "l2Book",
         "data": {"coin": "BTC", "time": <ms>,
                  "levels": [[{"px": "...", "sz": "...", "n": ...}, ...],
                             [{"px": "...", "sz": "...", "n": ...}, ...]]
         }}}

    levels[0] = bids (descending by px), levels[1] = asks (ascending by px).

    Yields L2Snapshot objects one at a time — never holds the whole file in
    memory. Malformed lines are skipped silently.
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                data = obj["raw"]["data"]
                ts = int(data["time"])
                levels = data["levels"]
                bids = [(float(lvl["px"]), float(lvl["sz"])) for lvl in levels[0]]
                asks = [(float(lvl["px"]), float(lvl["sz"])) for lvl in levels[1]]
                yield L2Snapshot(timestamp_ms=ts, bids=bids, asks=asks)
            except (json.JSONDecodeError, KeyError, ValueError, IndexError, TypeError):
                continue


# ─── Aggregator ──────────────────────────────────────────────────────


def _snapshot_depth_features(
    snap: L2Snapshot, depth_levels: int
) -> tuple[float, float, float, float, float, float] | None:
    """Per-snapshot depth metrics.

    Returns (bid_depth_usd, ask_depth_usd, imbalance, max_bid_wall_usd,
              max_ask_wall_usd, mid_price), or None if the book is one-sided.
    """
    if not snap.bids or not snap.asks:
        return None
    mid = snap.mid
    if mid is None or mid <= 0:
        return None
    bid_slice = snap.bids[:depth_levels]
    ask_slice = snap.asks[:depth_levels]
    # USD-notional depth = sum(px * sz)
    bid_depth = sum(px * sz for px, sz in bid_slice)
    ask_depth = sum(px * sz for px, sz in ask_slice)
    total = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / total if total > 0 else 0.0
    # Largest single-level USD size (a "wall")
    max_bid_wall = max((px * sz for px, sz in bid_slice), default=0.0)
    max_ask_wall = max((px * sz for px, sz in ask_slice), default=0.0)
    return bid_depth, ask_depth, imbalance, max_bid_wall, max_ask_wall, mid


def aggregate_l2_to_candles(
    snapshots: Iterable[L2Snapshot],
    interval_ms: int,
    depth_levels: int = 10,
) -> list[dict]:
    """Bin L2 snapshots into target timeframe → candle rows with depth features.

    Per-bar columns:

      timestamp:                bar START in ms (floor of timestamp / interval)
      open, high, low, close:   from snapshot mid-prices
      volume, num_trades:       None (NaN downstream)
      bid_depth_top10_usd:      mean across snapshots in the bar
      ask_depth_top10_usd:      mean across snapshots in the bar
      order_book_imbalance:     mean across snapshots
      mean_spread_bps:          mean spread (mid-relative) across snapshots
      max_bid_wall_usd,
      max_ask_wall_usd:         MAX across snapshots — the biggest wall ever
                                seen in the bar window
      n_snapshots:              number of valid snapshots in the bar

    Snapshots that are one-sided or have a non-positive mid are skipped.
    Returned list is sorted by timestamp ascending.

    `interval_ms` must be > 0. Common values:
      60_000          1m
      300_000         5m
      900_000         15m
      3_600_000       1h
      14_400_000      4h
      86_400_000      1d
    """
    if interval_ms <= 0:
        raise ValueError(f"interval_ms must be > 0; got {interval_ms}")
    if depth_levels < 1:
        raise ValueError(f"depth_levels must be >= 1; got {depth_levels}")

    # Per-bar accumulators (one entry per bin)
    bars: dict[int, dict] = {}

    for snap in snapshots:
        feats = _snapshot_depth_features(snap, depth_levels)
        if feats is None:
            continue
        bid_depth, ask_depth, imbalance, max_bid_wall, max_ask_wall, mid = feats
        spread = snap.spread_bps  # already None-safe above (feats was not None)
        if spread is None:
            continue

        bar_ts = (snap.timestamp_ms // interval_ms) * interval_ms
        bar = bars.get(bar_ts)
        if bar is None:
            bars[bar_ts] = {
                "timestamp": bar_ts,
                "open": mid,
                "high": mid,
                "low": mid,
                "close": mid,
                # Running sums for mean computation; finalize at end
                "_bid_depth_sum": bid_depth,
                "_ask_depth_sum": ask_depth,
                "_imbalance_sum": imbalance,
                "_spread_sum": spread,
                "max_bid_wall_usd": max_bid_wall,
                "max_ask_wall_usd": max_ask_wall,
                "n_snapshots": 1,
            }
        else:
            if mid > bar["high"]:
                bar["high"] = mid
            if mid < bar["low"]:
                bar["low"] = mid
            bar["close"] = mid  # last seen is close; snapshots arrive in order
            bar["_bid_depth_sum"] += bid_depth
            bar["_ask_depth_sum"] += ask_depth
            bar["_imbalance_sum"] += imbalance
            bar["_spread_sum"] += spread
            if max_bid_wall > bar["max_bid_wall_usd"]:
                bar["max_bid_wall_usd"] = max_bid_wall
            if max_ask_wall > bar["max_ask_wall_usd"]:
                bar["max_ask_wall_usd"] = max_ask_wall
            bar["n_snapshots"] += 1

    # Finalize: convert running sums → means, add NaN volume/num_trades
    out: list[dict] = []
    for ts in sorted(bars.keys()):
        b = bars[ts]
        n = b["n_snapshots"]
        out.append({
            "timestamp": b["timestamp"],
            "open": b["open"],
            "high": b["high"],
            "low": b["low"],
            "close": b["close"],
            "volume": None,
            "num_trades": None,
            "bid_depth_top10_usd": b["_bid_depth_sum"] / n,
            "ask_depth_top10_usd": b["_ask_depth_sum"] / n,
            "order_book_imbalance": b["_imbalance_sum"] / n,
            "mean_spread_bps": b["_spread_sum"] / n,
            "max_bid_wall_usd": b["max_bid_wall_usd"],
            "max_ask_wall_usd": b["max_ask_wall_usd"],
            "n_snapshots": n,
        })
    return out


# ─── Interval helpers ────────────────────────────────────────────────


_INTERVAL_TO_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def interval_to_ms(interval: str) -> int:
    """Map common interval names to milliseconds."""
    try:
        return _INTERVAL_TO_MS[interval]
    except KeyError:
        raise ValueError(
            f"unknown interval {interval!r}; "
            f"supported: {sorted(_INTERVAL_TO_MS.keys())}"
        )


# ─── Process-pool helpers ───────────────────────────────────────────


def _parse_aggregate_hour_for_pool(
    args: tuple[str, tuple[tuple[str, int], ...], int]
) -> dict[str, list[dict]]:
    """Top-level ProcessPool worker — parse + aggregate one hour file.

    Runs in a separate process to bypass the Python GIL on JSON parsing.
    Returns a dict mapping timeframe name → list of bars produced from
    this hour's snapshots. For sub-hourly timeframes (1m, 5m, 15m, 30m,
    1h) bars are complete. For multi-hour timeframes (4h, 1d) bars are
    PARTIAL — the parent must merge them with bars from adjacent hours
    sharing the same bar-start timestamp.

    Args layout:
      (path_str, ((tf_name, interval_ms), ...), depth_levels)

    Tuples (not lists/dicts) for the interval specs because they're
    pickled per task; tuples pickle slightly cheaper.
    """
    path_str, interval_specs, depth_levels = args
    snapshots = list(parse_l2_jsonl(path_str))
    return {
        tf: aggregate_l2_to_candles(
            snapshots, interval_ms=interval_ms, depth_levels=depth_levels,
        )
        for tf, interval_ms in interval_specs
    }


def _merge_partial_bars(bars_lists: list[list[dict]]) -> list[dict]:
    """Combine partial bars from multiple sources into final bars.

    `bars_lists` is a LIST of bar-lists (each from a single hour worker),
    in SOURCE ORDER (hour 0 first, hour 1 second, ...).

    Bars sharing the same `timestamp` (bar-start) are merged:
      - `open` ← from the FIRST source (earliest hour)
      - `close` ← from the LAST source (latest hour)
      - `high` / `low` ← max / min across sources
      - depth features ← weighted by `n_snapshots`
      - `max_*_wall_usd` ← max across sources
      - `n_snapshots` ← sum
    """
    by_ts: dict[int, list[dict]] = {}
    for bars in bars_lists:
        for bar in bars:
            by_ts.setdefault(bar["timestamp"], []).append(bar)
    out: list[dict] = []
    for ts in sorted(by_ts.keys()):
        parts = by_ts[ts]
        if len(parts) == 1:
            out.append(parts[0])
        else:
            out.append(_merge_bars(parts))
    return out


def _merge_bars(parts: list[dict]) -> dict:
    """Merge ≥2 partial bars sharing one timestamp.

    Parts must be in source order (earliest contributor first). The
    n_snapshots-weighted mean for depth features approximates what
    single-pass aggregation would produce.
    """
    first = parts[0]
    last = parts[-1]
    total_n = sum(p["n_snapshots"] for p in parts)

    def _wmean(field: str) -> float:
        return sum(p[field] * p["n_snapshots"] for p in parts) / total_n

    return {
        "timestamp": first["timestamp"],
        "open": first["open"],
        "high": max(p["high"] for p in parts),
        "low": min(p["low"] for p in parts),
        "close": last["close"],
        "volume": None,
        "num_trades": None,
        "bid_depth_top10_usd": _wmean("bid_depth_top10_usd"),
        "ask_depth_top10_usd": _wmean("ask_depth_top10_usd"),
        "order_book_imbalance": _wmean("order_book_imbalance"),
        "mean_spread_bps": _wmean("mean_spread_bps"),
        "max_bid_wall_usd": max(p["max_bid_wall_usd"] for p in parts),
        "max_ask_wall_usd": max(p["max_ask_wall_usd"] for p in parts),
        "n_snapshots": total_n,
    }


# ─── S3 download ─────────────────────────────────────────────────────


def download_l2_hour(
    s3_client,
    coin: str,
    date_str: str,
    hour: int,
    cache_dir: Path | None = None,
) -> Path | None:
    """Download one BTC L2 hour file from `hyperliquid-archive`.

    Args:
      s3_client:  boto3 S3 client (caller-supplied so it can be mocked)
      coin:       coin name (e.g. "BTC")
      date_str:   "YYYYMMDD"
      hour:       0-23
      cache_dir:  optional override (default ~/.rift/raw_l2/)

    Returns:
      Path to the cached .jsonl file, or None if the file is missing /
      download fails. The cache path is `{cache_dir}/{date_str}/{hour}/{coin}.jsonl`.

    Uses RequestPayer="requester" — caller's AWS account pays egress.
    Atomic write (tmp + rename) so an interrupted run never leaves 0-byte files.
    """
    import lz4.frame

    base = cache_dir if cache_dir is not None else RAW_L2_CACHE_DIR
    cache_path = base / date_str / f"{hour:02d}" / f"{coin.upper()}.jsonl"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path

    # Remove any stale 0-byte placeholder
    if cache_path.exists():
        try:
            cache_path.unlink()
        except OSError:
            pass

    key = f"market_data/{date_str}/{hour}/l2Book/{coin.upper()}.lz4"
    try:
        resp = s3_client.get_object(
            Bucket=L2_ARCHIVE_BUCKET,
            Key=key,
            RequestPayer="requester",
        )
        compressed = resp["Body"].read()
        raw = lz4.frame.decompress(compressed)
        del compressed

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_bytes(raw)
        del raw
        tmp.replace(cache_path)
        return cache_path
    except Exception:
        return None


# ─── Orchestrator ────────────────────────────────────────────────────


def _parse_date(date_str: str) -> datetime:
    """Parse 'YYYY-MM-DD' OR 'YYYYMMDD' to a datetime."""
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"date_str must be YYYY-MM-DD or YYYYMMDD; got {date_str!r}")


def _merge_into_candles_parquet(
    new_bars: list[dict],
    coin: str,
    interval: str,
    data_dir: Path,
) -> int:
    """Append new bars to `{data_dir}/{COIN}/{interval}/candles.parquet`.

    Where (timestamp, coin, interval) collides with existing rows, prefer
    the EXISTING row (it likely has volume/num_trades from the fills-based
    sync; L2-derived rows have NaN there). Schema-union on diagonal so
    L2-only depth columns are preserved alongside fills-only volume.

    Returns number of new rows actually added (after dedup).

    Note on cost: each call re-reads the entire existing parquet. Callers
    syncing many days should buffer and flush in batches (see
    `sync_l2_candles`'s `flush_batch_size`) rather than calling this once
    per day — that turns the total cost from O(N²) into O(N²/batch_size).
    """
    import polars as pl

    if not new_bars:
        return 0

    out_dir = data_dir / coin.upper() / interval
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "candles.parquet"

    new_df = pl.DataFrame(new_bars)
    if out_path.exists():
        existing = pl.read_parquet(out_path)
        existing_height = existing.height
        # Diagonal concat handles new depth columns absent from existing.
        # Order matters: existing FIRST so dedup keeps existing volume.
        combined = pl.concat([existing, new_df], how="diagonal")
        combined = combined.unique(subset=["timestamp"], keep="first").sort("timestamp")
    else:
        existing_height = 0
        combined = new_df.sort("timestamp")

    combined.write_parquet(out_path)
    return int(combined.height - existing_height)


def _load_sync_meta(coin: str, data_dir: Path) -> dict:
    p = data_dir / coin.upper() / "_l2_sync_meta.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _save_sync_meta(coin: str, data_dir: Path, meta: dict) -> None:
    p = data_dir / coin.upper() / "_l2_sync_meta.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2))


def sync_l2_candles(
    coins: list[str],
    timeframes: list[str],
    start_date: str | None = None,
    end_date: str = "",
    incremental: bool = True,
    on_progress: Callable | None = None,
    max_download_workers: int = 24,
    max_parse_workers: int = 4,
    flush_batch_size: int = 30,
    drop_raw_after_aggregate: bool = True,
    data_dir: Path | None = None,
    s3_client=None,
) -> dict[str, dict[str, int]]:
    """Sync L2-derived candles from `hyperliquid-archive` for each coin × timeframe.

    Per-day work:
      1. Parallel-download all 24 hours of {coin}.lz4 → ~/.rift/raw_l2/
      2. Stream-parse each hour into L2Snapshots
      3. Aggregate into candle rows for every requested timeframe
      4. **Buffer** the aggregated bars (in memory) across days
      5. Drop the raw .jsonl files (configurable)

    Per-batch work (every `flush_batch_size` days, or at end of sync):
      6. Merge the buffered bars into ~/.rift/data/{COIN}/{interval}/candles.parquet
      7. Write checkpoint to ~/.rift/data/{COIN}/_l2_sync_meta.json

    Why batch-merge: each parquet merge re-reads the entire existing file.
    Doing this per-day makes the total cost O(N²). Batching ~30 days per
    flush makes it O(N²/30) — roughly 30× faster on the merge step for a
    multi-year sync. Memory cost is bounded (~12 MB for 30 days of bars
    across 7 timeframes) and well below the Mac Mini ceiling.

    Crash-recovery: if interrupted mid-batch, the checkpoint reflects the
    last *completed* batch. Resume re-downloads the unfinished batch (raw
    files were dropped per day for disk safety). Bandwidth waste is bounded
    by `flush_batch_size` days.

    Args:
      coins:                 e.g. ["BTC"]
      timeframes:            e.g. ["1m", "5m", "15m", "1h", "4h", "1d"]
      start_date:            "YYYY-MM-DD" or None. **None means "last 90 days"** —
                             the right default for first-run OSS UX. Pass
                             `L2_ARCHIVE_START` (or any earlier date string)
                             to backfill full history.
      end_date:              "YYYY-MM-DD" (default = today)
      incremental:           skip dates already in `_l2_sync_meta.json`
      on_progress:           optional `callable(msg: str)` for status lines
      max_download_workers:  parallel S3 fetches per day (default 24 — uses
                             one worker per hour file; minimal idle workers)
      max_parse_workers:     ProcessPool workers for JSON parse + aggregation
                             (default 4 — bypasses Python's GIL). Set to 1
                             for serial parsing (useful in tests with mocked
                             objects that don't pickle cleanly).
                             **macOS caveat:** when `max_parse_workers > 1`,
                             callers MUST wrap their entry-point in
                             `if __name__ == "__main__":` because macOS uses
                             spawn-mode multiprocessing (workers re-import
                             the launcher module — without the guard they
                             re-execute it and infinitely respawn). The
                             orchestrator will detect a BrokenProcessPool
                             and fall back to serial parsing with a warning,
                             so the sync still completes either way.
      flush_batch_size:      days to buffer before each parquet merge
                             (default 30). Smaller = lower recovery cost;
                             larger = faster total. 30 is a good middle.
      drop_raw_after_aggregate: delete the raw jsonl files (default True)
      data_dir:              cache root (default ~/.rift/data/)
      s3_client:             pre-built boto3 client (default: constructed
                             via rift_data.s3.download.get_s3_client)

    Returns:
      `{coin: {interval: rows_added}}`
    """
    from rift_data.data import DEFAULT_DATA_DIR
    from rift_data.s3.download import get_s3_client

    if flush_batch_size < 1:
        raise ValueError(f"flush_batch_size must be >= 1; got {flush_batch_size}")
    if max_parse_workers < 1:
        raise ValueError(f"max_parse_workers must be >= 1; got {max_parse_workers}")

    out_dir = data_dir if data_dir is not None else DEFAULT_DATA_DIR
    if s3_client is None:
        s3_client = get_s3_client()

    interval_ms_map = {tf: interval_to_ms(tf) for tf in timeframes}
    interval_specs_tuple = tuple(interval_ms_map.items())  # picklable

    if not end_date:
        end_date = datetime.utcnow().strftime("%Y-%m-%d")

    # Apply 90-day default when start_date is None
    if start_date is None:
        ninety_days_ago = (datetime.utcnow() - timedelta(days=DEFAULT_SYNC_DAYS))
        start_date = ninety_days_ago.strftime("%Y-%m-%d")

    end_dt = _parse_date(end_date)
    results: dict[str, dict[str, int]] = {
        coin: {tf: 0 for tf in timeframes} for coin in coins
    }

    def _emit(msg: str) -> None:
        if on_progress is not None:
            try:
                on_progress(msg)
            except Exception:
                pass

    def _flush_batch(
        coin_u: str,
        batch_bars: dict[str, list[dict]],
        last_date_in_batch: datetime,
    ) -> None:
        """Merge buffered bars into parquets and advance the checkpoint."""
        for tf, bars in batch_bars.items():
            if not bars:
                continue
            added = _merge_into_candles_parquet(bars, coin_u, tf, out_dir)
            results[coin_u][tf] += added
        meta = {
            "last_completed_date": last_date_in_batch.strftime("%Y-%m-%d"),
            "updated_at": int(_time.time() * 1000),
        }
        _save_sync_meta(coin_u, out_dir, meta)

    # Process pool reused across all coins/days (spawn cost amortized).
    # max_parse_workers=1 → no pool; serial parsing in the main process.
    # Useful for tests with mock objects that don't pickle cleanly.
    parse_pool: ProcessPoolExecutor | None = None
    if max_parse_workers > 1:
        try:
            parse_pool = ProcessPoolExecutor(max_workers=max_parse_workers)
        except Exception:
            parse_pool = None  # fall back to serial on any spawn failure

    for coin in coins:
        coin_u = coin.upper()
        meta = _load_sync_meta(coin_u, out_dir) if incremental else {}
        last_done = meta.get("last_completed_date", "")
        # Effective per-coin start: max(start_date, last_completed+1)
        if last_done:
            try:
                start_eff = (_parse_date(last_done) + timedelta(days=1)).strftime("%Y-%m-%d")
                if start_eff > start_date:
                    start_date_used = start_eff
                else:
                    start_date_used = start_date
            except ValueError:
                start_date_used = start_date
        else:
            start_date_used = start_date

        start_dt = _parse_date(start_date_used)
        total_days = max(0, (end_dt - start_dt).days + 1)
        _emit(
            f"{coin_u}: syncing L2 candles {start_date_used} → {end_date} "
            f"({total_days} days, batch={flush_batch_size})"
        )

        # In-memory buffer of aggregated bars per timeframe
        batch_bars: dict[str, list[dict]] = {tf: [] for tf in timeframes}
        days_in_batch = 0
        last_date_in_batch: datetime | None = None

        for day_offset in range(total_days):
            date_dt = start_dt + timedelta(days=day_offset)
            date_str = date_dt.strftime("%Y%m%d")

            # Parallel-download all 24 hours
            with ThreadPoolExecutor(max_workers=max_download_workers) as pool:
                future_to_hour = {
                    pool.submit(download_l2_hour, s3_client, coin_u, date_str, h): h
                    for h in range(24)
                }
                hour_paths: dict[int, Path | None] = {}
                for fut in as_completed(future_to_hour):
                    h = future_to_hour[fut]
                    try:
                        hour_paths[h] = fut.result()
                    except Exception:
                        hour_paths[h] = None

            # Parse + aggregate per hour (parallel via ProcessPool, or serial).
            # Hours are processed in order; per-hour outputs are partial bars
            # for multi-hour timeframes (4h, 1d) and complete bars otherwise.
            ordered_paths = [
                hour_paths[h] for h in range(24) if hour_paths.get(h) is not None
            ]
            worker_args = [
                (str(p), interval_specs_tuple, 10)  # 10 = depth_levels
                for p in ordered_paths
            ]
            if parse_pool is not None and len(worker_args) > 1:
                try:
                    # `map` preserves input order — critical for partial-bar merge
                    per_hour_bars = list(
                        parse_pool.map(_parse_aggregate_hour_for_pool, worker_args)
                    )
                except Exception as exc:
                    # Pool died (e.g., caller forgot `if __name__ == "__main__":`
                    # guard on macOS spawn mode). Fall back to serial parsing for
                    # the rest of the run rather than failing the whole sync.
                    _emit(
                        f"WARN: ProcessPool failed ({type(exc).__name__}: {exc}); "
                        f"falling back to serial parsing. If you launched from a "
                        f"top-level script, wrap it in `if __name__ == '__main__':`"
                    )
                    parse_pool.shutdown(wait=False, cancel_futures=True)
                    parse_pool = None
                    per_hour_bars = [
                        _parse_aggregate_hour_for_pool(args) for args in worker_args
                    ]
            else:
                per_hour_bars = [
                    _parse_aggregate_hour_for_pool(args) for args in worker_args
                ]

            # Merge partial bars across hours, per timeframe
            for tf in timeframes:
                bars_lists_for_tf = [d[tf] for d in per_hour_bars]
                merged = _merge_partial_bars(bars_lists_for_tf)
                batch_bars[tf].extend(merged)

            # Drop raw files for this date (disk peak control)
            if drop_raw_after_aggregate:
                for h in range(24):
                    p = hour_paths.get(h)
                    if p is not None:
                        try:
                            p.unlink()
                            p.parent.rmdir()  # remove empty hour dir
                        except OSError:
                            pass
                try:
                    (RAW_L2_CACHE_DIR / date_str).rmdir()
                except OSError:
                    pass

            days_in_batch += 1
            last_date_in_batch = date_dt
            is_final_day = (day_offset == total_days - 1)

            # Flush when batch full OR final day
            if days_in_batch >= flush_batch_size or is_final_day:
                _flush_batch(coin_u, batch_bars, last_date_in_batch)
                _emit(
                    f"{coin_u}: batch flushed through "
                    f"{last_date_in_batch.strftime('%Y-%m-%d')} "
                    f"({day_offset + 1}/{total_days})"
                )
                # Reset
                batch_bars = {tf: [] for tf in timeframes}
                days_in_batch = 0
                last_date_in_batch = None

    # Shut down the process pool cleanly
    if parse_pool is not None:
        parse_pool.shutdown(wait=True)

    return results
