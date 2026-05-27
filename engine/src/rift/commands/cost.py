"""Pre-trade cost estimator command.

Surfaces `substrate.frictions.cost.estimate_trade_cost()` as a CLI command:

    rift cost BTC 50000          # 50K of BTC, taker, no holding window
    rift cost BTC 50000 --side sell --tf 1h --hold 24
    rift cost ETH 10000 --maker  # post-only

Returns a JSON result with the fee + funding + impact + slippage breakdown
in both bps and USD, plus an ADV-utilization warning if the size is large
relative to the asset's 24h volume.
"""

from __future__ import annotations

import math
from typing import Any

import typer

from rift.commands._shared import app, _emit


@app.command("cost")
def cost(
    pair: str = typer.Argument(..., help="Trading pair (e.g. BTC, ETH-PERP)"),
    notional_usd: float = typer.Argument(..., help="Trade size in USD notional"),
    side: str = typer.Option("buy", "--side", help="buy / sell / long / short"),
    interval: str = typer.Option("1h", "--tf", "--interval", help="Candle interval for ADV / vol calc"),
    hold_hours: float = typer.Option(0.0, "--hold", help="Holding period in hours for funding accrual"),
    maker: bool = typer.Option(False, "--maker", help="Treat as maker (post-only) instead of taker"),
    spot: bool = typer.Option(False, "--spot", help="Treat as spot trade instead of perp"),
    no_builder_fee: bool = typer.Option(False, "--no-builder-fee", help="Exclude RIFT builder fee"),
    tier_volume: float = typer.Option(0.0, "--tier-vol-14d", help="Your 14d HL volume (for fee-tier lookup)"),
) -> None:
    """Estimate pre-trade cost: fees + funding + impact + slippage."""
    import numpy as np
    from rift_substrate.frictions.cost import estimate_trade_cost

    from rift_data.data import load_candles, load_funding_rates
    from rift_core.schema import normalize_coin

    coin = normalize_coin(pair)
    df = load_candles(coin, interval)
    funding_df = load_funding_rates(coin)

    warnings: list[str] = []

    if df is None or len(df) < 30:
        warnings.append(
            f"No candle cache for {coin} {interval}; impact + ADV omitted. "
            f"Run `rift sync` to enable full pre-trade cost analysis."
        )
        mid_price = 0.0
        adv_usd = None
        daily_vol = 0.03
        current_funding = 0.0
    else:
        closes = df["close"].to_numpy().astype(float)
        volumes = df["volume"].to_numpy().astype(float)
        mid_price = float(closes[-1])

        ppd = _periods_per_day(interval)
        notional_per_bar = volumes * closes
        # Use up to last 30 days of bars
        lookback_bars = min(30 * ppd, len(notional_per_bar))
        adv_usd = float(np.nanmean(notional_per_bar[-lookback_bars:]) * ppd)

        log_rets = np.diff(np.log(closes[closes > 0]))
        sample_n = min(30 * ppd, len(log_rets))
        period_vol = float(np.nanstd(log_rets[-sample_n:], ddof=1))
        daily_vol_raw = period_vol * math.sqrt(ppd) if ppd > 0 else period_vol

        # Cap daily vol at 50% — anything higher is almost always a data artifact
        # (e.g., very few bars spanning an extreme regime). The impact model is
        # sqrt(notional/ADV) × vol, and an over-estimated vol balloons the impact.
        if daily_vol_raw > 0.5:
            warnings.append(
                f"Daily vol estimate from cache is {daily_vol_raw * 100:.0f}% — "
                f"unusually high, likely an artifact of limited sample "
                f"({len(closes)} candles). Capping at 50% for impact calc; "
                f"run `rift sync` for longer history."
            )
            daily_vol = 0.5
        else:
            daily_vol = daily_vol_raw

        if len(closes) < 30 * ppd:
            warnings.append(
                f"Only {len(closes)} candles cached for {coin} {interval} "
                f"(< 30 days). Vol + ADV estimates are based on limited sample."
            )

        current_funding = 0.0
        if funding_df is not None and len(funding_df) > 0:
            current_funding = float(funding_df["funding_rate"][-1])

    result = estimate_trade_cost(
        side=side,
        notional_usd=notional_usd,
        mid_price=mid_price if mid_price > 0 else 100.0,
        adv_usd=adv_usd,
        daily_vol=daily_vol,
        is_taker=not maker,
        instrument="spot" if spot else "perp",
        tier_volume_14d_usd=tier_volume,
        include_builder_fee=not no_builder_fee,
        holding_period_hours=hold_hours,
        current_funding_rate=current_funding,
    )

    adv_pct = (notional_usd / adv_usd * 100.0) if adv_usd and adv_usd > 0 else None
    if adv_pct is not None:
        if adv_pct > 5.0:
            warnings.append(
                f"Trade is {adv_pct:.2f}% of ADV — institutional desks consider >5% "
                f"a meaningful market-mover. Consider splitting the order."
            )
        elif adv_pct > 1.0:
            warnings.append(
                f"Trade is {adv_pct:.2f}% of ADV — within typical retail/MM range "
                f"but not negligible."
            )

    if mid_price == 0:
        warnings.append(
            "No price data — used $100 placeholder for ratio math; absolute USD may be off."
        )

    _emit({
        "type": "result",
        "pair": coin,
        "side": side,
        "notional_usd": notional_usd,
        "mid_price": mid_price,
        "adv_usd": adv_usd,
        "adv_pct": adv_pct,
        "daily_vol_pct": daily_vol * 100.0,
        "holding_hours": hold_hours,
        "current_funding_rate": current_funding,
        "cost": _trade_cost_to_dict(result),
        "warnings": warnings,
    })


def _trade_cost_to_dict(tc: Any) -> dict:
    """Serialize a TradeCost dataclass to a plain dict for JSON emission."""
    return {
        "fee_bps": round(tc.fee_bps, 4),
        "fee_usd": round(tc.fee_usd, 4),
        "funding_bps": round(tc.funding_bps, 4),
        "funding_usd": round(tc.funding_usd, 4),
        "impact_bps": round(tc.impact_bps, 4),
        "impact_usd": round(tc.impact_usd, 4),
        "slippage_bps": round(tc.slippage_bps, 4),
        "slippage_usd": round(tc.slippage_usd, 4),
        "total_bps": round(tc.total_bps, 4),
        "total_usd": round(tc.total_usd, 4),
        "impact_model": tc.impact_model_name,
    }


def _periods_per_day(interval: str) -> int:
    """Convert an interval like "1h", "4h", "15m", "1d" to bars-per-day."""
    s = interval.strip().lower()
    mapping = {
        "1m": 1440, "5m": 288, "15m": 96, "30m": 48,
        "1h": 24, "4h": 6, "1d": 1, "1w": 1,
    }
    return mapping.get(s, 24)
