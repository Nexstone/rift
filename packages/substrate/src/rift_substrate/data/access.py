"""Data.load() — single entry point for loading HL data as polars frames.

Honest about what's cached. Loud about what's missing.

API:
  Data.load(coins, fields, start, end, freq) → polars LazyFrame
  Data.inventory(coins) → InventoryReport
  Data.available_fields(coins) → dict[coin, list[field]]

Freq spec:
  "1m", "5m", "15m", "1h", "4h", "1d"     — time bars
  "volume:100"                              — volume bars (100 base units)
  "dollar:1000000"                          — dollar bars ($1M notional)
"""

from __future__ import annotations

from typing import Any

import polars as pl

from rift_substrate.data.fields import (
    FIELDS,
    INGESTION_HINTS,
    FieldSpec,
)
from rift_substrate.data.inventory import InventoryReport, inventory
from rift_substrate.data import paths
from rift_substrate.data.resample import (
    parse_time_freq,
    to_dollar_bars,
    to_time_bars,
    to_volume_bars,
)


class DataNotAvailable(RuntimeError):
    """Raised when a requested field/coin/timeframe isn't cached.

    Always carries an actionable `.hint` attribute with the command to
    fix it.
    """
    def __init__(self, msg: str, hint: str | None = None):
        super().__init__(msg)
        self.hint = hint


# ─── Freq parsing ─────────────────────────────────────────────────────


def _parse_freq(freq: str) -> tuple[str, float | None]:
    """Parse a freq string into (kind, threshold).

    Returns:
      ("time", None)                — for time-based freqs like "1h"
      ("volume", threshold_units)   — for "volume:100"
      ("dollar", threshold_usd)     — for "dollar:1000000"
    """
    if ":" in freq:
        kind, _, val = freq.partition(":")
        kind = kind.lower()
        if kind not in ("volume", "dollar"):
            raise ValueError(
                f"Unknown bar type '{kind}'. Supported: time (e.g. '1h'), "
                f"'volume:<N>', 'dollar:<N>'"
            )
        try:
            threshold = float(val)
        except ValueError as e:
            raise ValueError(f"Bar threshold must be a number; got '{val}'") from e
        if threshold <= 0:
            raise ValueError(f"Bar threshold must be > 0; got {threshold}")
        return kind, threshold

    # Time bar
    parse_time_freq(freq)  # validates
    return "time", None


# ─── Field validation ────────────────────────────────────────────────


def _validate_fields(requested: list[str]) -> list[FieldSpec]:
    """Return FieldSpecs for each requested field, or raise."""
    out = []
    unknown = []
    for name in requested:
        spec = FIELDS.get(name)
        if spec is None:
            unknown.append(name)
        else:
            out.append(spec)
    if unknown:
        raise ValueError(
            f"Unknown field(s): {unknown}. "
            f"Available: {sorted(FIELDS.keys())}"
        )
    return out


def _check_field_availability(
    coins: list[str],
    field_specs: list[FieldSpec],
    inv: InventoryReport,
    tf: str,
) -> None:
    """Raise DataNotAvailable if any field is missing for any coin."""
    missing: list[tuple[str, str, str]] = []  # (coin, field, hint)

    for coin in coins:
        ci = inv.get(coin)
        if ci is None:
            for spec in field_specs:
                missing.append((coin, spec.name, INGESTION_HINTS[spec.ingestion]))
            continue
        for spec in field_specs:
            if spec.source == "candles" or spec.source == "funding" or spec.source == "ctx":
                # Candle, funding, ctx fields are in the candles parquet
                if not any(t.tf == tf for t in ci.timeframes):
                    missing.append((coin, spec.name,
                                     f"rift fetch {coin} --tf {tf}"))
            elif spec.source == "fills":
                if not ci.has_fills:
                    missing.append((coin, spec.name,
                                     f"rift sync --coins {coin} --include-fills (AWS-paid)"))
            elif spec.source == "l2":
                if not ci.has_l2:
                    missing.append((coin, spec.name,
                                     f"rift sync --coins {coin} --include-l2 (AWS-paid)"))

    if missing:
        # Group by (field, hint) for concise message
        unique_pairs: dict[tuple[str, str], list[str]] = {}
        for coin, field, hint in missing:
            key = (field, hint)
            unique_pairs.setdefault(key, []).append(coin)

        lines = ["Required data not cached:"]
        for (field, hint), coins_list in unique_pairs.items():
            lines.append(f"  field '{field}' for {','.join(coins_list)}")
            lines.append(f"    → {hint}")
        msg = "\n".join(lines)
        raise DataNotAvailable(msg, hint=msg)


# ─── Loader ───────────────────────────────────────────────────────────


def _load_coin_at_tf(coin: str, tf: str, field_names: list[str]) -> pl.LazyFrame | None:
    """Load one coin's candle parquet at the given tf, select needed fields."""
    p = paths.candles_path(coin, tf)
    if not p.is_file():
        return None
    lf = pl.scan_parquet(p)
    available_cols = pl.read_parquet_schema(p).keys()
    keep = ["timestamp"] + [f for f in field_names if f in available_cols]
    lf = lf.select(keep).with_columns(pl.lit(coin).alias("coin"))
    return lf


def _load_coin_funding(coin: str) -> pl.LazyFrame | None:
    p = paths.funding_path(coin)
    if not p.is_file():
        return None
    return pl.scan_parquet(p).with_columns(pl.lit(coin).alias("coin"))


# ─── Data namespace ──────────────────────────────────────────────────


class Data:
    """Entry point for loading cached HL data.

    Static methods only — Data has no per-instance state.
    """

    @staticmethod
    def load(
        coins: list[str] | str,
        fields: list[str],
        start: str | int | None = None,
        end: str | int | None = None,
        freq: str = "1h",
    ) -> pl.LazyFrame:
        """Load data for one or more coins at the given freq.

        Args:
          coins:  single coin "BTC" or list ["BTC", "ETH"]
          fields: list of field names (see FIELDS catalog)
          start:  start time — ISO date "2024-01-01" or epoch ms
          end:    end time — same format as start
          freq:   "1h" / "5m" / "1d" for time bars
                  "volume:100" for volume bars
                  "dollar:1000000" for dollar bars

        Returns:
          polars LazyFrame with columns: timestamp, coin, <fields>

        Raises:
          DataNotAvailable: if any requested field isn't cached for any coin.
            The exception carries a `.hint` with the command to fix it.
          ValueError: if a field name is unknown or freq is malformed.
        """
        # Normalize inputs
        if isinstance(coins, str):
            coins = [coins]
        coins = [c.upper() for c in coins]
        field_specs = _validate_fields(fields)

        kind, threshold = _parse_freq(freq)

        # For time bars, use the cached TF that matches `freq`.
        # For volume/dollar bars, load the finest cached TF and aggregate.
        inv = inventory(coins)

        if kind == "time":
            _check_field_availability(coins, field_specs, inv, tf=freq)
            return _load_time_bars(coins, fields, freq, start, end, inv)
        else:
            # Use finest cached TF that has volume + close
            base_tf = _pick_finest_tf(coins, inv)
            if base_tf is None:
                raise DataNotAvailable(
                    f"No candle data cached for {coins} — can't build {kind} bars.",
                    hint=f"rift fetch {coins[0]} --tf 1m  (finest practical resolution)",
                )
            _check_field_availability(coins, field_specs, inv, tf=base_tf)
            return _load_accumulator_bars(coins, fields, base_tf, kind, threshold, start, end)

    @staticmethod
    def inventory(coins: list[str] | None = None) -> InventoryReport:
        """Scan ~/.rift/data/ and return what's cached."""
        return inventory(coins)

    @staticmethod
    def available_fields(coins: list[str] | None = None) -> dict[str, list[str]]:
        """For each coin, list the fields actually loadable right now."""
        if coins is None:
            coins = paths.cached_coins()
        coins = [c.upper() for c in coins]
        inv = inventory(coins)

        result: dict[str, list[str]] = {}
        for coin in coins:
            ci = inv.get(coin)
            available = []
            if ci is None:
                result[coin] = []
                continue
            for spec in FIELDS.values():
                if spec.source in ("candles", "funding", "ctx"):
                    if ci.has_candles:
                        available.append(spec.name)
                elif spec.source == "fills":
                    if ci.has_fills:
                        available.append(spec.name)
                elif spec.source == "l2":
                    if ci.has_l2:
                        available.append(spec.name)
            result[coin] = sorted(available)
        return result


# ─── Internal loaders ────────────────────────────────────────────────


def _pick_finest_tf(coins: list[str], inv: InventoryReport) -> str | None:
    """Choose the smallest cached time-bar TF common to all coins.

    For volume/dollar bars we want the finest resolution available.
    """
    common: set[str] | None = None
    for coin in coins:
        ci = inv.get(coin)
        if ci is None or not ci.has_candles:
            return None
        coin_tfs = {t.tf for t in ci.timeframes}
        common = coin_tfs if common is None else (common & coin_tfs)

    if not common:
        return None

    return min(common, key=paths._tf_sort_key)


def _epoch_ms(t: str | int | None) -> int | None:
    if t is None:
        return None
    if isinstance(t, int):
        return t
    # ISO date string
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _load_time_bars(
    coins: list[str],
    fields: list[str],
    tf: str,
    start: str | int | None,
    end: str | int | None,
    inv: InventoryReport,
) -> pl.LazyFrame:
    """Load time-bar data for multiple coins, stack vertically."""
    start_ms = _epoch_ms(start)
    end_ms = _epoch_ms(end)

    lazy_frames: list[pl.LazyFrame] = []
    for coin in coins:
        lf = _load_coin_at_tf(coin, tf, fields)
        if lf is None:
            continue  # already validated, but skip defensively
        if start_ms is not None:
            lf = lf.filter(pl.col("timestamp") >= start_ms)
        if end_ms is not None:
            lf = lf.filter(pl.col("timestamp") <= end_ms)
        lazy_frames.append(lf)

    if not lazy_frames:
        # Should not happen given _check_field_availability ran first
        return pl.LazyFrame()

    return pl.concat(lazy_frames, how="diagonal_relaxed")


def _load_accumulator_bars(
    coins: list[str],
    fields: list[str],
    base_tf: str,
    kind: str,
    threshold: float,
    start: str | int | None,
    end: str | int | None,
) -> pl.LazyFrame:
    """Load volume/dollar bars per coin and stack."""
    start_ms = _epoch_ms(start)
    end_ms = _epoch_ms(end)

    frames: list[pl.DataFrame] = []
    for coin in coins:
        lf = _load_coin_at_tf(coin, base_tf, fields)
        if lf is None:
            continue
        if start_ms is not None:
            lf = lf.filter(pl.col("timestamp") >= start_ms)
        if end_ms is not None:
            lf = lf.filter(pl.col("timestamp") <= end_ms)
        df = lf.collect()
        if len(df) == 0:
            continue

        if kind == "volume":
            agg = to_volume_bars(df, threshold_units=threshold)
        else:  # dollar
            agg = to_dollar_bars(df, threshold_usd=threshold)

        agg = agg.with_columns(pl.lit(coin).alias("coin"))
        frames.append(agg)

    if not frames:
        return pl.LazyFrame()

    return pl.concat(frames, how="diagonal_relaxed").lazy()
