"""Cache inventory — honest reporting of what's on disk + what to fetch.

The first call an OSS user should make. Tells them:
  - Which coins they have cached, in what timeframes
  - Which data sources are present (candles / funding / fills / l2)
  - Which capabilities they can use right now vs which need more data
  - Concrete commands to acquire what's missing

No HL network calls — purely reads from disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from rift_substrate.data import paths


@dataclass(frozen=True)
class TimeframeInfo:
    """What we know about a coin+TF candle file."""
    tf: str
    rows: int
    first_ts_ms: int | None
    last_ts_ms: int | None
    file_size_bytes: int


@dataclass(frozen=True)
class CoinInventory:
    """All cached data for a single coin."""
    coin: str
    timeframes: list[TimeframeInfo]
    has_funding: bool
    funding_rows: int
    fill_days: int           # number of per-day fill files
    fill_first_day: str | None  # YYYY-MM-DD
    fill_last_day: str | None
    l2_days: int

    @property
    def has_candles(self) -> bool:
        return len(self.timeframes) > 0

    @property
    def has_fills(self) -> bool:
        return self.fill_days > 0

    @property
    def has_l2(self) -> bool:
        return self.l2_days > 0


@dataclass(frozen=True)
class InventoryReport:
    """Full inventory across all cached coins."""
    coins: list[CoinInventory]
    data_dir: Path

    @property
    def total_coins(self) -> int:
        return len(self.coins)

    def has_coin(self, coin: str) -> bool:
        c = coin.upper()
        return any(ci.coin == c for ci in self.coins)

    def get(self, coin: str) -> CoinInventory | None:
        c = coin.upper()
        for ci in self.coins:
            if ci.coin == c:
                return ci
        return None

    def coins_with_candles_at(self, tf: str) -> list[str]:
        """List of coins that have <tf> candles cached."""
        return [
            ci.coin for ci in self.coins
            if any(t.tf == tf for t in ci.timeframes)
        ]

    def coins_with_funding(self) -> list[str]:
        return [ci.coin for ci in self.coins if ci.has_funding]

    def coins_with_fills(self) -> list[str]:
        return [ci.coin for ci in self.coins if ci.has_fills]

    def coins_with_l2(self) -> list[str]:
        return [ci.coin for ci in self.coins if ci.has_l2]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation."""
        return {
            "data_dir": str(self.data_dir),
            "total_coins": self.total_coins,
            "coins": [
                {
                    "coin": ci.coin,
                    "timeframes": [
                        {
                            "tf": t.tf,
                            "rows": t.rows,
                            "first_ts_ms": t.first_ts_ms,
                            "last_ts_ms": t.last_ts_ms,
                            "size_bytes": t.file_size_bytes,
                        }
                        for t in ci.timeframes
                    ],
                    "has_funding": ci.has_funding,
                    "funding_rows": ci.funding_rows,
                    "fill_days": ci.fill_days,
                    "fill_first_day": ci.fill_first_day,
                    "fill_last_day": ci.fill_last_day,
                    "l2_days": ci.l2_days,
                }
                for ci in self.coins
            ],
        }

    def summary(self) -> str:
        """Human-readable text report. Suitable for direct CLI display."""
        if self.total_coins == 0:
            return self._empty_summary()
        return self._populated_summary()

    def _empty_summary(self) -> str:
        return "\n".join([
            "",
            f"RIFT data inventory  ({self.data_dir})",
            "─" * 60,
            "",
            "  No cached data yet.",
            "",
            "  Start with:",
            "    rift fetch BTC --tf 1h        (free, ~1 min)",
            "    rift fetch ETH SOL --tf 1h    (multi-coin)",
            "    rift sync --coins BTC --include-fills  (depth, AWS-paid)",
            "",
        ])

    def _populated_summary(self) -> str:
        lines = [
            "",
            f"RIFT data inventory  ({self.data_dir})",
            "─" * 60,
            "",
            f"  Cached coins: {self.total_coins}",
        ]

        # Per-coin lines
        for ci in self.coins:
            tf_summary = ", ".join(
                f"{t.tf}({t.rows:,})" for t in ci.timeframes
            ) or "no candles"
            funding = "✔" if ci.has_funding else "✗"
            fills = f"{ci.fill_days}d" if ci.has_fills else "✗"
            l2 = f"{ci.l2_days}d" if ci.has_l2 else "✗"
            lines.append(
                f"    {ci.coin:8s} {tf_summary:30s}  funding:{funding}  fills:{fills}  l2:{l2}"
            )

        lines.append("")
        lines.append("  What you can do right now:")

        # Capability lines
        candle_coins = [ci for ci in self.coins if ci.has_candles]
        funding_coins = self.coins_with_funding()
        fill_coins = self.coins_with_fills()
        l2_coins = self.coins_with_l2()

        if candle_coins:
            lines.append(f"    ✔ Statistical tests on returns of {len(candle_coins)} coin(s)")
            lines.append(f"    ✔ Time / volume / dollar bars on cached coins")
        else:
            lines.append("    ⚠ No candle data — fetch some to do anything: rift fetch BTC --tf 1h")

        if funding_coins:
            lines.append(f"    ✔ Funding rate analysis on {len(funding_coins)} coin(s)")
        else:
            lines.append("    ⚠ No funding cached — included automatically with `rift fetch`")

        if fill_coins:
            lines.append(f"    ✔ Order flow analysis on {len(fill_coins)} coin(s)")
        else:
            lines.append("    ✗ Order flow: requires fills (rift sync --include-fills, AWS-paid)")

        if l2_coins:
            lines.append(f"    ✔ L2 microstructure on {len(l2_coins)} coin(s)")
        else:
            lines.append("    ✗ L2 slippage walks: requires L2 (rift sync --include-l2, AWS-paid)")

        # Risk model heuristic
        if len(candle_coins) >= 20:
            lines.append("    ✔ Risk model: sufficient coins for style factor decomposition")
        elif len(candle_coins) >= 5:
            lines.append("    ⚠ Risk model: enough for sector factors, not yet style factors (need 20+)")
        else:
            lines.append(f"    ✗ Risk model: need 5+ coins (sector) or 20+ (style); have {len(candle_coins)}")

        lines.append("")
        return "\n".join(lines)


# ─── Scanner ───────────────────────────────────────────────────────────


def _scan_timeframe(coin: str, tf: str) -> TimeframeInfo | None:
    p = paths.candles_path(coin, tf)
    if not p.is_file():
        return None
    try:
        df = pl.read_parquet(p)
        rows = len(df)
        size = p.stat().st_size
        if rows == 0:
            return TimeframeInfo(tf=tf, rows=0, first_ts_ms=None, last_ts_ms=None, file_size_bytes=size)
        ts_col = "timestamp" if "timestamp" in df.columns else (
            "time" if "time" in df.columns else None
        )
        if ts_col is None:
            return TimeframeInfo(tf=tf, rows=rows, first_ts_ms=None, last_ts_ms=None, file_size_bytes=size)
        first = int(df[ts_col].min())
        last = int(df[ts_col].max())
        return TimeframeInfo(tf=tf, rows=rows, first_ts_ms=first, last_ts_ms=last, file_size_bytes=size)
    except Exception:
        return None


def _scan_funding(coin: str) -> tuple[bool, int]:
    p = paths.funding_path(coin)
    if not p.is_file():
        return False, 0
    try:
        df = pl.read_parquet(p)
        return True, len(df)
    except Exception:
        return False, 0


def _scan_coin(coin: str) -> CoinInventory:
    tfs_present = paths.cached_timeframes(coin)
    timeframes = [_scan_timeframe(coin, tf) for tf in tfs_present]
    timeframes = [t for t in timeframes if t is not None]

    has_funding, funding_rows = _scan_funding(coin)

    fill_files = paths.fill_files(coin)
    fill_days = len(fill_files)
    fill_first = _file_date(fill_files[0]) if fill_files else None
    fill_last = _file_date(fill_files[-1]) if fill_files else None

    l2_files = paths.l2_files(coin)
    l2_days = len(l2_files)

    return CoinInventory(
        coin=coin.upper(),
        timeframes=timeframes,
        has_funding=has_funding,
        funding_rows=funding_rows,
        fill_days=fill_days,
        fill_first_day=fill_first,
        fill_last_day=fill_last,
        l2_days=l2_days,
    )


def _file_date(p: Path) -> str:
    """Convert YYYYMMDD.parquet → YYYY-MM-DD."""
    stem = p.stem
    if len(stem) == 8 and stem.isdigit():
        return f"{stem[:4]}-{stem[4:6]}-{stem[6:8]}"
    return stem


def inventory(coins: list[str] | None = None) -> InventoryReport:
    """Scan ~/.rift/data/ and return a structured inventory report.

    Args:
      coins: optional filter — only scan these coins. Defaults to all cached.
    """
    if coins is None:
        coins = paths.cached_coins()
    else:
        coins = [c.upper() for c in coins]

    inventories = [_scan_coin(c) for c in coins]
    return InventoryReport(coins=inventories, data_dir=paths.DATA_DIR)
