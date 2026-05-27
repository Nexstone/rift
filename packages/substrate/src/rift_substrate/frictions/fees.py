"""Hyperliquid fee schedule — tiered base fees + builder fee composition.

A trade on Hyperliquid pays two fees:

  base_fee:    paid to Hyperliquid. Tiered by trailing 14-day trading volume.
               Maker vs taker matters; perp vs spot matters.
  builder_fee: paid to the builder code wallet (RIFT's, set in calibrations).
               0.03% on perps, 1% on spot.

Total fee = base_fee + builder_fee.

The schedule lives in `calibrations/hl_fees.json` and is reloaded each
time `load_default_schedule()` is called. Users should verify the JSON
against Hyperliquid's published schedule before relying on these numbers
for live PnL — HL's fee schedule changes over time.

Usage:

    from rift_substrate.frictions import estimate_fee

    quote = estimate_fee(
        notional_usd=10_000,
        is_taker=True,
        instrument="perp",
        tier_volume_14d_usd=0,        # tier 0 (default)
        include_builder_fee=True,
    )
    print(f"total fee: {quote.total_bps:.2f} bps = ${quote.total_usd:.2f}")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


_DEFAULT_SCHEDULE_PATH = Path(__file__).parent / "calibrations" / "hl_fees.json"


@dataclass(frozen=True)
class FeeTier:
    """One row of the fee tier table."""

    min_14d_volume_usd: float
    maker_bps: float
    taker_bps: float


@dataclass(frozen=True)
class FeeSchedule:
    """Loaded fee schedule (base tiers + builder fee).

    Construct via `load_default_schedule()` to read the calibrations JSON,
    or pass an explicit dict to the constructor for custom schedules.
    """

    perp_tiers: list[FeeTier] = field(default_factory=list)
    spot_tiers: list[FeeTier] = field(default_factory=list)
    builder_fee_bps_perp: float = 3.0
    builder_fee_bps_spot: float = 100.0
    builder_address: str = ""

    def tier_for(self, instrument: str, volume_14d_usd: float) -> FeeTier:
        """Return the tier matching the trader's 14-day volume."""
        tiers = self.perp_tiers if instrument == "perp" else self.spot_tiers
        if not tiers:
            raise ValueError(f"no tiers loaded for instrument={instrument!r}")
        # Tiers are in ascending order — walk and pick the highest threshold ≤ volume.
        chosen = tiers[0]
        for t in tiers:
            if volume_14d_usd >= t.min_14d_volume_usd:
                chosen = t
            else:
                break
        return chosen

    def builder_fee_bps(self, instrument: str) -> float:
        return self.builder_fee_bps_perp if instrument == "perp" else self.builder_fee_bps_spot


def load_default_schedule() -> FeeSchedule:
    """Load the schedule from `calibrations/hl_fees.json`.

    Re-reads on each call so a user can drop in their own JSON without
    restarting the Python process.
    """
    if not _DEFAULT_SCHEDULE_PATH.is_file():
        raise FileNotFoundError(f"missing fee schedule: {_DEFAULT_SCHEDULE_PATH}")
    data = json.loads(_DEFAULT_SCHEDULE_PATH.read_text())
    return _schedule_from_dict(data)


def _schedule_from_dict(data: dict) -> FeeSchedule:
    perp_tiers = [
        FeeTier(
            min_14d_volume_usd=float(t["min_14d_volume_usd"]),
            maker_bps=float(t["maker_bps"]),
            taker_bps=float(t["taker_bps"]),
        )
        for t in data.get("perp_tiers", [])
    ]
    spot_tiers = [
        FeeTier(
            min_14d_volume_usd=float(t["min_14d_volume_usd"]),
            maker_bps=float(t["maker_bps"]),
            taker_bps=float(t["taker_bps"]),
        )
        for t in data.get("spot_tiers", [])
    ]
    builder = data.get("builder_fee_bps", {})
    return FeeSchedule(
        perp_tiers=perp_tiers,
        spot_tiers=spot_tiers,
        builder_fee_bps_perp=float(builder.get("perp", 3.0)),
        builder_fee_bps_spot=float(builder.get("spot", 100.0)),
        builder_address=str(data.get("builder_address", "")),
    )


@dataclass(frozen=True)
class FeeQuote:
    """Result of `estimate_fee()`. All bps are of notional."""

    base_bps: float          # HL base fee (maker or taker)
    builder_bps: float       # RIFT builder fee component
    total_bps: float         # base + builder
    total_usd: float         # bps × notional
    is_taker: bool
    instrument: str          # "perp" or "spot"
    tier_min_volume_usd: float
    notional_usd: float

    def __str__(self) -> str:
        side = "taker" if self.is_taker else "maker"
        return (
            f"FeeQuote(instrument={self.instrument}, side={side}, "
            f"base={self.base_bps:+.2f}bps, builder={self.builder_bps:.2f}bps, "
            f"total={self.total_bps:+.2f}bps, ${self.total_usd:+.4f})"
        )


def estimate_fee(
    notional_usd: float,
    is_taker: bool,
    *,
    instrument: str = "perp",
    tier_volume_14d_usd: float = 0.0,
    include_builder_fee: bool = True,
    schedule: FeeSchedule | None = None,
) -> FeeQuote:
    """Compute total fee for a single fill.

    Args:
      notional_usd:        $ size of the fill (price × quantity)
      is_taker:            True for taker, False for maker (passive limit)
      instrument:          "perp" (default) or "spot"
      tier_volume_14d_usd: trailing 14d $ volume (drives tier selection).
                           Default 0 = bottom tier.
      include_builder_fee: include the RIFT builder fee in the total (default True)
      schedule:            override the default schedule (else loads from calibrations JSON)

    Returns:
      `FeeQuote` with bps breakdown + $ total.
    """
    if instrument not in ("perp", "spot"):
        raise ValueError(f"instrument must be 'perp' or 'spot'; got {instrument!r}")
    if notional_usd < 0:
        raise ValueError(f"notional must be >= 0; got {notional_usd}")

    if schedule is None:
        schedule = load_default_schedule()

    tier = schedule.tier_for(instrument, tier_volume_14d_usd)
    base_bps = tier.taker_bps if is_taker else tier.maker_bps
    builder_bps = schedule.builder_fee_bps(instrument) if include_builder_fee else 0.0
    total_bps = base_bps + builder_bps
    total_usd = notional_usd * (total_bps / 10_000.0)

    return FeeQuote(
        base_bps=base_bps,
        builder_bps=builder_bps,
        total_bps=total_bps,
        total_usd=total_usd,
        is_taker=is_taker,
        instrument=instrument,
        tier_min_volume_usd=tier.min_14d_volume_usd,
        notional_usd=notional_usd,
    )
