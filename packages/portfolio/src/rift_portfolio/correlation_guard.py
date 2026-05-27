"""Correlation guard — prevent doubling down on the same sector.

Before Recon executes, checks recent trade logs for positions in
correlated coins. Memecoins move together, L1s move together, etc.
Taking the same directional bet on two correlated coins doubles risk
without doubling edge.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# Coin → sector mapping
SECTOR_MAP = {
    # Memecoins
    "DOGE": "meme", "FARTCOIN": "meme", "PENGU": "meme", "WIF": "meme",
    "PUMP": "meme", "TRUMP": "meme", "kPEPE": "meme", "kSHIB": "meme",
    "ZEREBRO": "meme", "MON": "meme", "MEGA": "meme",
    # Layer 1s
    "BTC": "l1", "ETH": "l1", "SOL": "l1", "AVAX": "l1", "NEAR": "l1",
    "SUI": "l1", "APT": "l1", "ADA": "l1", "TRX": "l1", "BNB": "l1",
    "XRP": "l1", "XMR": "l1", "ZEC": "l1", "DASH": "l1",
    # DeFi
    "AAVE": "defi", "CRV": "defi", "LDO": "defi", "ONDO": "defi",
    "ZRO": "defi", "ENA": "defi", "PENDLE": "defi",
    # Layer 2 / infrastructure
    "ARB": "l2", "POL": "l2",
    # Gaming / NFT
    "AXS": "gaming", "APE": "gaming",
    # Hyperliquid ecosystem
    "HYPE": "hl_eco",
    # Other (uncorrelated)
    "PAXG": "commodity", "SPX": "index",
}

RECON_DIRS = [
    Path.home() / ".rift" / "recon",
    Path.home() / ".rift" / "recon_sim",
]


def get_sector(coin: str) -> str | None:
    """Return sector name for a coin, or None if unknown."""
    return SECTOR_MAP.get(coin.upper())


def check_correlation(
    coin: str,
    direction: str,
    max_age_hours: float = 4.0,
) -> dict:
    """Check recent recon logs for correlated positions.

    Scans ~/.rift/recon/ and ~/.rift/recon_sim/ for trades in the same
    sector and direction that are less than max_age_hours old.

    Returns:
        {
            "blocked": bool,       # True if 2+ existing trades in same sector+direction
            "warning": bool,       # True if 1 existing trade found
            "reduce_size": bool,   # True if should reduce size by 50%
            "sector": str | None,
            "existing_trades": list[dict],
            "msg": str,
        }
    """
    sector = get_sector(coin)
    direction = direction.lower()
    cutoff = time.time() - max_age_hours * 3600

    result = {
        "blocked": False,
        "warning": False,
        "reduce_size": False,
        "sector": sector,
        "existing_trades": [],
        "msg": "",
    }

    if sector is None:
        return result  # unknown sector, no guard

    # Scan recent recon logs
    for d in RECON_DIRS:
        if not d.exists():
            continue
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue

            # Check if recent enough
            started = data.get("started_at", "")
            if not started:
                continue

            # Parse timestamp from filename (YYYYMMDD_HHMMSS)
            try:
                from datetime import datetime
                ts = datetime.strptime(started, "%Y-%m-%d %H:%M:%S").timestamp()
                if ts < cutoff:
                    continue
            except Exception:
                continue

            # Check same sector + same direction
            trade_coin = data.get("coin", "")
            trade_dir = data.get("direction", "").lower()
            trade_sector = get_sector(trade_coin)

            if trade_sector == sector and trade_dir == direction and trade_coin != coin.upper():
                result["existing_trades"].append({
                    "coin": trade_coin,
                    "direction": trade_dir,
                    "started_at": started,
                    "pnl_pct": data.get("pnl_pct", 0),
                    "exit_reason": data.get("exit_reason", "active"),
                })

    n = len(result["existing_trades"])
    coins = [t["coin"] for t in result["existing_trades"]]

    if n >= 2:
        result["blocked"] = True
        result["msg"] = f"Blocked: {n} existing {direction} trades in {sector} sector ({', '.join(coins)})"
    elif n == 1:
        result["warning"] = True
        result["reduce_size"] = True
        result["msg"] = f"Correlated: {coins[0]} already {direction} in {sector} sector — size reduced 50%"

    return result
