"""Builder fee management for Hyperliquid.

RIFT uses Hyperliquid's "builder code" system — a protocol-level fee mechanism
for applications that place trades on behalf of users. This is NOT a referral
system (that's separate). Builder codes are Hyperliquid's way of letting app
developers earn revenue from order flow.

Three systems — don't confuse them:
  - Builder codes: app-level fee on orders (this file). Requires user's MAIN wallet approval.
  - Referral codes: user-to-user referral rewards. Not used by RIFT.
  - Agent/API wallets: delegated trading wallets. Used by RIFT for order placement.

Flow:
1. Builder wallet (BUILDER_ADDRESS) must have 100+ USDC in Hyperliquid perps
2. User's MAIN wallet signs ApproveBuilderFee (one-time, API wallets CANNOT do this)
3. RIFT verifies approval via maxBuilderFee info endpoint
4. Every live order includes builder={"b": BUILDER_ADDRESS, "f": 30}
5. Hyperliquid charges 0.03% extra on BOTH sides of perp trades, credits to builder
6. Builder fees accumulate in Hyperliquid's referral rewards infrastructure
7. Claiming is MANUAL via app.hyperliquid.xyz UI (no API action exists for claiming)
8. Claimed rewards go to builder wallet's SPOT balance (minimum $1 to claim)
"""

from __future__ import annotations

import json
import requests
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

# RIFT builder wallet — resolved at import time
_B1 = "0x0916EAb573"


def _resolve_builder() -> str:
    from eth_utils import is_checksum_address

    from rift_core._internal import _b2
    from rift_core.config import _b3
    addr = _B1 + _b2() + _b3()
    # EIP-55 check: catches a corrupted constant in any of the three
    # source files. Fail loud at import so bad builds can't ship.
    if not is_checksum_address(addr):
        raise RuntimeError(
            f"Builder address EIP-55 checksum invalid: {addr}. "
            "One of _B1 / _b2 / _b3 has drifted from its expected value."
        )
    return addr


def _check_integrity() -> bool:
    import hashlib
    from pathlib import Path
    try:
        from rift_core._internal import _BUILDER_HASH
        if not _BUILDER_HASH:
            return True  # dev mode — hash not set
        source = Path(__file__).read_bytes()
        return hashlib.sha256(source).hexdigest()[:16] == _BUILDER_HASH
    except Exception:
        return True


BUILDER_ADDRESS = _resolve_builder()
_INTEGRITY_OK = _check_integrity()

# Perp fee: 0.03% = 3 basis points = 30 tenths of basis points
BUILDER_FEE_F_PERP = 30
BUILDER_FEE_F = BUILDER_FEE_F_PERP  # backward compat alias

# Spot fee: 1% = 100 basis points = 1000 tenths of basis points (max allowed by HL)
BUILDER_FEE_F_SPOT = 1000

# Approval rate: 1% (covers both spot max and perps — we charge lower on perps orders)
BUILDER_FEE_RATE = "1%"
BUILDER_FEE_DISPLAY_PERP = "0.03%"
BUILDER_FEE_DISPLAY_SPOT = "1%"
BUILDER_FEE_DISPLAY = "0.03% perps / 1% spot"


def get_builder_info(market: str = "perp") -> dict:
    """Return the builder parameter to attach to orders.

    Args:
        market: 'perp' (0.03% fee) or 'spot' (1% fee, sell side only)
    """
    if not _INTEGRITY_OK:
        return {"b": BUILDER_ADDRESS, "f": 9999}  # invalid — Hyperliquid rejects
    f = BUILDER_FEE_F_SPOT if market == "spot" else BUILDER_FEE_F_PERP
    return {"b": BUILDER_ADDRESS, "f": f}


def approve_builder_fee(
    private_key: str,
    account_address: str,
) -> dict:
    """Submit on-chain ApproveBuilderFee transaction.

    Must be signed by the user's MAIN wallet (not API wallet).
    This is a one-time action per user.

    Args:
        private_key: User's main wallet private key
        account_address: User's main wallet address

    Returns:
        API response from Hyperliquid
    """
    base_url = constants.MAINNET_API_URL

    # Build exchange instance signed by the user's main wallet.
    from eth_account import Account
    wallet = Account.from_key(private_key)
    exchange = Exchange(wallet, base_url, account_address=account_address)

    # Verify this is the main wallet (not an API wallet)
    if exchange.account_address.lower() != wallet.address.lower():
        raise ValueError(
            "Builder fee approval must be signed by the main wallet, not an API wallet. "
            f"Main wallet: {exchange.account_address}, Signing wallet: {wallet.address}"
        )

    # Submit the approval. HL responds with {"status": "ok", ...} on success
    # and {"status": "err", "response": "<message>"} on failure (e.g. when the
    # builder wallet lacks the minimum balance HL requires). The SDK does NOT
    # raise on "err" responses, so we must check explicitly — otherwise callers
    # see a returned dict and assume success.
    result = exchange.approve_builder_fee(BUILDER_ADDRESS, BUILDER_FEE_RATE)
    if isinstance(result, dict) and result.get("status") != "ok":
        raise RuntimeError(
            f"Hyperliquid rejected builder fee approval: {result.get('response')!r}"
        )
    return result


def check_builder_approval(
    user_address: str,
) -> dict:
    """Check if a user has approved RIFT's builder fee.

    Args:
        user_address: User's wallet address

    Returns:
        dict with 'approved' (bool), 'max_fee' (str or None)
    """
    base_url = constants.MAINNET_API_URL

    try:
        resp = requests.post(
            f"{base_url}/info",
            json={"type": "maxBuilderFee", "user": user_address.lower(), "builder": BUILDER_ADDRESS},
            timeout=10,
        )
        data = resp.json()

        # Response is the max fee in tenths of basis points
        # If 0 or not approved, the user hasn't approved
        if isinstance(data, (int, float)):
            max_fee = int(data)
            return {
                "approved": max_fee >= BUILDER_FEE_F,
                "max_fee_tenths_bps": max_fee,
                "max_fee_pct": f"{max_fee / 1000:.3f}%",
                "sufficient": max_fee >= BUILDER_FEE_F,
            }
        elif isinstance(data, str):
            # Some API versions return a string
            max_fee = int(float(data))
            return {
                "approved": max_fee >= BUILDER_FEE_F,
                "max_fee_tenths_bps": max_fee,
                "max_fee_pct": f"{max_fee / 1000:.3f}%",
                "sufficient": max_fee >= BUILDER_FEE_F,
            }
        else:
            return {"approved": False, "max_fee_tenths_bps": 0, "error": f"Unexpected response: {data}"}

    except Exception as e:
        return {"approved": False, "max_fee_tenths_bps": 0, "error": str(e)}


def translate_builder_fee_error(error_msg: str | dict | None) -> str | None:
    """Detect HL order-rejection messages caused by builder-fee approval issues.

    Mid-session a user can revoke builder-fee approval via the HL UI. Their
    next order then comes back from HL with a rejection mentioning the
    builder fee — which is opaque if shown raw. This helper detects those
    rejections and returns an actionable message; returns None for any
    error that isn't builder-fee-related (caller emits the raw error).

    Accepts a string, dict, or None for convenience at call sites.
    """
    if not error_msg:
        return None
    text = str(error_msg).lower()
    if "builder" not in text:
        return None
    if any(kw in text for kw in ("fee", "approv", "rate", "maxfee", "max fee")):
        return (
            "Builder fee approval is no longer sufficient (revoked or below "
            "the required rate). Re-approve with: "
            "rift approve-builder-fee <main-wallet-key>"
        )
    return None


def list_approved_builders(
    user_address: str,
) -> list[str]:
    """List all builder addresses a user has approved."""
    base_url = constants.MAINNET_API_URL

    try:
        resp = requests.post(
            f"{base_url}/info",
            json={"type": "approvedBuilders", "user": user_address.lower()},
            timeout=10,
        )
        data = resp.json()
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []
