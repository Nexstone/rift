"""Universe — coin selection.

Two construction modes:
  Universe.from_hl()      — queries HL info endpoint (live, no cache needed)
  Universe.from_cache()   — uses what's on disk
  Universe.from_sectors() — by curated sector tags
  Universe.from_list()    — explicit list

Plus set operations: intersection, difference, union.

UniverseSpec carries per-coin metadata so downstream layers can filter
without re-querying HL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rift_substrate.data import paths


_VENDORED_SECTORS_PATH = Path(__file__).parent / "calibrations" / "sector_tags.json"
_USER_SECTORS_PATH = Path.home() / ".rift" / "universe" / "sectors.json"


@dataclass(frozen=True)
class AssetMetadata:
    """Per-coin metadata."""
    coin: str
    sector_tags: list[str]
    size_decimals: int | None = None
    max_leverage: int | None = None
    avg_volume_24h_usd: float | None = None


@dataclass(frozen=True)
class UniverseSpec:
    """A selected universe of coins + metadata."""
    coins: list[str]
    metadata: dict[str, AssetMetadata]
    discovered_at_ms: int
    source: str  # "hl_live" | "cache" | "sectors" | "list" | "set_op"

    def __len__(self) -> int:
        return len(self.coins)

    def __iter__(self):
        return iter(self.coins)

    def __contains__(self, coin: str) -> bool:
        return coin.upper() in self.coins

    def top_by_volume(self, n: int) -> "UniverseSpec":
        """Return a new spec containing the top `n` coins by 24h volume.

        Requires `avg_volume_24h_usd` populated in metadata — i.e. the
        spec was built via `from_hl()` or `from_hl_data()`. For specs
        from `from_list()` / `from_sectors()` / `from_cache()` with no
        volume metadata, coins with missing volume sort last.

        Pure derivative — original spec is unchanged.
        """
        if n <= 0:
            return UniverseSpec(
                coins=[],
                metadata={},
                discovered_at_ms=self.discovered_at_ms,
                source=f"{self.source}+top_by_volume",
            )
        ranked = sorted(
            self.coins,
            key=lambda c: (
                self.metadata[c].avg_volume_24h_usd
                if c in self.metadata and self.metadata[c].avg_volume_24h_usd is not None
                else -1.0
            ),
            reverse=True,
        )
        top = ranked[:n]
        return UniverseSpec(
            coins=sorted(top),
            metadata={c: self.metadata[c] for c in top if c in self.metadata},
            discovered_at_ms=self.discovered_at_ms,
            source=f"{self.source}+top_by_volume",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "coins": self.coins,
            "discovered_at_ms": self.discovered_at_ms,
            "source": self.source,
            "metadata": {
                k: {
                    "sector_tags": v.sector_tags,
                    "size_decimals": v.size_decimals,
                    "max_leverage": v.max_leverage,
                    "avg_volume_24h_usd": v.avg_volume_24h_usd,
                }
                for k, v in self.metadata.items()
            },
        }


# ─── Sector tag loading ──────────────────────────────────────────────


def _load_sector_tags() -> dict[str, list[str]]:
    """Load coin → sector tags map.

    User override at ~/.rift/universe/sectors.json wins over vendored.
    Missing files = empty map (no tags).
    """
    if _USER_SECTORS_PATH.is_file():
        try:
            return json.loads(_USER_SECTORS_PATH.read_text())
        except Exception:
            pass
    if _VENDORED_SECTORS_PATH.is_file():
        try:
            return json.loads(_VENDORED_SECTORS_PATH.read_text())
        except Exception:
            pass
    return {}


# ─── Universe constructors ───────────────────────────────────────────


class Universe:
    """Coin universe selection."""

    @staticmethod
    def from_hl(
        min_volume_24h_usd: float = 0,
        exclude: list[str] | None = None,
        include_only: list[str] | None = None,
    ) -> UniverseSpec:
        """Query HL info endpoint live and build a universe.

        Args:
          min_volume_24h_usd: minimum 24h notional volume
          exclude:            coins to drop
          include_only:       if set, only keep coins from this list

        No data caching required — this is the way to discover what's
        available before fetching any of it.
        """
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        info = Info(constants.MAINNET_API_URL, skip_ws=True)
        meta_ctx = info.meta_and_asset_ctxs()
        meta = meta_ctx[0]
        ctxs = meta_ctx[1]
        universe_meta = meta.get("universe", [])

        sector_tags = _load_sector_tags()
        exclude_set = {c.upper() for c in (exclude or [])}
        include_set = {c.upper() for c in include_only} if include_only else None

        coins: list[str] = []
        metadata: dict[str, AssetMetadata] = {}

        for i, asset in enumerate(universe_meta):
            coin = str(asset["name"]).upper()
            if coin in exclude_set:
                continue
            if include_set is not None and coin not in include_set:
                continue

            ctx = ctxs[i] if i < len(ctxs) else {}
            day_vol = float(ctx.get("dayNtlVlm", 0)) if ctx else 0.0

            if day_vol < min_volume_24h_usd:
                continue

            coins.append(coin)
            metadata[coin] = AssetMetadata(
                coin=coin,
                sector_tags=sector_tags.get(coin, []),
                size_decimals=asset.get("szDecimals"),
                max_leverage=asset.get("maxLeverage"),
                avg_volume_24h_usd=day_vol,
            )

        coins.sort()
        return UniverseSpec(
            coins=coins,
            metadata=metadata,
            discovered_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            source="hl_live",
        )

    @staticmethod
    def from_hl_data(
        meta: dict,
        asset_ctxs: list,
        min_volume_24h_usd: float = 0,
        exclude: list[str] | None = None,
        include_only: list[str] | None = None,
    ) -> UniverseSpec:
        """Build a universe from pre-fetched HL meta + asset_ctxs.

        Same selection logic as `from_hl()` but takes already-fetched data
        so callers (e.g. Scout) don't double-roundtrip to HL when they need
        the raw ctxs for other purposes.

        Args:
          meta:                HL info.meta() response
          asset_ctxs:          HL info.meta_and_asset_ctxs()[1]
          min_volume_24h_usd:  minimum 24h notional volume
          exclude:             coins to drop
          include_only:        if set, only keep coins from this list
        """
        universe_meta = meta.get("universe", [])
        sector_tags = _load_sector_tags()
        exclude_set = {c.upper() for c in (exclude or [])}
        include_set = {c.upper() for c in include_only} if include_only else None

        coins: list[str] = []
        metadata: dict[str, AssetMetadata] = {}

        for i, asset in enumerate(universe_meta):
            coin = str(asset["name"]).upper()
            if coin in exclude_set:
                continue
            if include_set is not None and coin not in include_set:
                continue

            ctx = asset_ctxs[i] if i < len(asset_ctxs) else {}
            day_vol = float(ctx.get("dayNtlVlm", 0)) if ctx else 0.0

            if day_vol < min_volume_24h_usd:
                continue

            coins.append(coin)
            metadata[coin] = AssetMetadata(
                coin=coin,
                sector_tags=sector_tags.get(coin, []),
                size_decimals=asset.get("szDecimals"),
                max_leverage=asset.get("maxLeverage"),
                avg_volume_24h_usd=day_vol,
            )

        coins.sort()
        return UniverseSpec(
            coins=coins,
            metadata=metadata,
            discovered_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            source="hl_data",
        )

    @staticmethod
    def from_cache() -> UniverseSpec:
        """Universe = all coins with any cached data."""
        sector_tags = _load_sector_tags()
        coins = paths.cached_coins()
        metadata = {
            c: AssetMetadata(coin=c, sector_tags=sector_tags.get(c, []))
            for c in coins
        }
        return UniverseSpec(
            coins=coins,
            metadata=metadata,
            discovered_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            source="cache",
        )

    @staticmethod
    def from_sectors(sectors: list[str]) -> UniverseSpec:
        """All coins tagged with any of the given sectors.

        Reads from vendored + user-overridden sector_tags.json.
        """
        sector_tags = _load_sector_tags()
        sector_set = {s.lower() for s in sectors}
        coins: list[str] = []
        metadata: dict[str, AssetMetadata] = {}
        for coin, tags in sector_tags.items():
            if any(t.lower() in sector_set for t in tags):
                coins.append(coin)
                metadata[coin] = AssetMetadata(coin=coin, sector_tags=tags)
        coins.sort()
        return UniverseSpec(
            coins=coins,
            metadata=metadata,
            discovered_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            source="sectors",
        )

    @staticmethod
    def from_list(coins: list[str]) -> UniverseSpec:
        """Explicit list of coins."""
        sector_tags = _load_sector_tags()
        upper = sorted(c.upper() for c in coins)
        metadata = {
            c: AssetMetadata(coin=c, sector_tags=sector_tags.get(c, []))
            for c in upper
        }
        return UniverseSpec(
            coins=upper,
            metadata=metadata,
            discovered_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            source="list",
        )

    @staticmethod
    def intersection(*specs: UniverseSpec) -> UniverseSpec:
        if not specs:
            return Universe.from_list([])
        common = set(specs[0].coins)
        for s in specs[1:]:
            common &= set(s.coins)
        coins = sorted(common)
        metadata: dict[str, AssetMetadata] = {}
        for s in specs:
            for c in coins:
                if c in s.metadata:
                    metadata[c] = s.metadata[c]
                    break
        return UniverseSpec(
            coins=coins,
            metadata=metadata,
            discovered_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            source="set_op",
        )

    @staticmethod
    def difference(a: UniverseSpec, b: UniverseSpec) -> UniverseSpec:
        coins = sorted(set(a.coins) - set(b.coins))
        metadata = {c: a.metadata[c] for c in coins if c in a.metadata}
        return UniverseSpec(
            coins=coins,
            metadata=metadata,
            discovered_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            source="set_op",
        )

    @staticmethod
    def union(*specs: UniverseSpec) -> UniverseSpec:
        if not specs:
            return Universe.from_list([])
        coins_set = set()
        metadata: dict[str, AssetMetadata] = {}
        for s in specs:
            coins_set |= set(s.coins)
            for c, m in s.metadata.items():
                metadata.setdefault(c, m)
        coins = sorted(coins_set)
        return UniverseSpec(
            coins=coins,
            metadata={c: metadata[c] for c in coins if c in metadata},
            discovered_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            source="set_op",
        )
