"""Hyperliquid account abstraction mode detection and collateral reading.

Hyperliquid supports multiple account modes that change how spot vs perp
balances are treated. RIFT must read the user's available collateral
correctly regardless of which mode they're in — otherwise gate checks
reject valid trades or accept oversized ones.

Modes (per HL docs and SDK):
  - 'disabled'        Standard mode. Spot and perp are separate balances.
                      Only perp accountValue is usable for perp trading.
                      Default for sophisticated users / market makers.
  - 'unifiedAccount'  Unified mode. Spot USDC IS perp collateral
                      automatically — no transfer needed. UI default
                      for new HL wallets.
  - 'portfolioMargin' Most capital-efficient. Pooled collateral across
                      USDC + other eligible assets (HYPE, BTC, USDH)
                      valued at oracle * LTV. Requires $10k account
                      value or $5M lifetime volume to enable.

Detection:
  The correct query is `info.query_user_abstraction_state(addr)` which
  returns the abstraction string verbatim. There is a similarly-named
  `query_user_dex_abstraction_state` — that's a DIFFERENT mode (DEX
  Abstraction, being discontinued) and returns a bool. Do not confuse
  them.

Mode-switch behavior to be aware of (verified on testnet):
  - Standard → Unified: HL auto-consolidates perp USDC into spot.
  - Unified → Standard: USDC stays in spot. Manual `usd_class_transfer`
    needed to refund perp.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

# Note: hyperliquid.info.Info is imported lazily inside functions so this
# module stays importable even if the SDK isn't available (matches the
# pattern in rift_trade.execute).


# Friendly mode names exposed to rift code. Maps to HL-native strings via
# _FRIENDLY_TO_HL / _HL_TO_FRIENDLY below.
AccountMode = Literal["standard", "unified", "portfolio_margin", "unknown"]


# HL-native abstraction values (the strings HL's setUserAbstraction action
# expects). Defined in hyperliquid.utils.types.Abstraction.
_HL_TO_FRIENDLY: dict[str, AccountMode] = {
    "disabled": "standard",
    "unifiedAccount": "unified",
    "portfolioMargin": "portfolio_margin",
}

_FRIENDLY_TO_HL: dict[str, str] = {
    "standard": "disabled",
    "unified": "unifiedAccount",
    "portfolio_margin": "portfolioMargin",
}


@dataclass(frozen=True)
class CollateralBreakdown:
    """Decomposition of a wallet's USD-denominated trading collateral.

    `total` is what gates should size against — it's the immediately-usable
    collateral for the wallet's CURRENT mode. Standard mode users do not
    have spot USDC count toward this total; they must transfer first.
    """
    mode: AccountMode
    perp_account_value: Decimal     # marginSummary.accountValue
    perp_margin_used: Decimal       # marginSummary.totalMarginUsed
    perp_available: Decimal         # perp_account_value - perp_margin_used
    spot_usdc: Decimal              # spot balance of USDC token
    total: Decimal                  # what gate checks should use

    @property
    def perp_only(self) -> bool:
        """True if spot USDC is NOT counted as perp collateral (Standard mode)."""
        return self.mode == "standard"


def query_account_mode(info, address: str) -> AccountMode:
    """Return the wallet's current account abstraction mode.

    Always lowercases the address before querying — HL is case-sensitive
    here in practice.

    Unknown / new HL modes are returned as "unknown"; downstream code
    should treat that conservatively (we recommend: treat as "unified"
    so spot USDC IS counted, since that's the most permissive sizing
    and HL itself will reject the order if it's wrong).
    """
    raw = info.query_user_abstraction_state(address.lower())
    if not isinstance(raw, str):
        return "unknown"
    return _HL_TO_FRIENDLY.get(raw, "unknown")


def hl_native_mode(friendly: AccountMode) -> str:
    """Translate a friendly mode name to the HL-native string for
    `exchange.user_set_abstraction(...)`."""
    if friendly not in _FRIENDLY_TO_HL:
        raise ValueError(
            f"Unknown account mode '{friendly}'. "
            f"Valid: {list(_FRIENDLY_TO_HL.keys())}"
        )
    return _FRIENDLY_TO_HL[friendly]


def _spot_usdc(info, address: str) -> Decimal:
    """Sum the wallet's spot USDC balance. Returns 0 if none."""
    spot_state = info.spot_user_state(address.lower())
    for b in spot_state.get("balances", []):
        if b.get("coin") == "USDC":
            return Decimal(str(b.get("total", "0")))
    return Decimal("0")


def read_collateral(info, address: str, *, perp_state: dict | None = None) -> CollateralBreakdown:
    """Read the wallet's available collateral, mode-aware.

    Single source of truth for "how much can this wallet trade with."
    Used by Phase 0 gate checks, algo loop equity reads, balance
    displays, etc.

    Args:
      info:        Hyperliquid Info client.
      address:     Wallet address (case-insensitive).
      perp_state:  Optional pre-fetched response from `info.user_state(addr)`.
                   Pass this in if the caller already has it to skip a
                   redundant HL call (e.g. algo loop's _sync_position).

    Standard mode:        total = perp_available (spot USDC ignored —
                          user must usd_class_transfer to fund perp)
    Unified mode:         total = perp_available + spot_usdc. HL docs say
                          "perp dex user states are not meaningful" here.
    Portfolio margin:     total = perp_available + spot_usdc. Same as
                          unified per HL docs. NOTE: PM also lets users
                          post non-USDC collateral (HYPE/BTC/USDH at
                          oracle*LTV). v0.1 counts USDC only because HL
                          doesn't expose the LTV table via info endpoints.
                          PM users with non-USDC collateral are
                          under-counted; flagged in RELEASE.md.
    Unknown mode:         treated as unified (safest default — HL will
                          reject the order if sizing is wrong).
    """
    mode = query_account_mode(info, address)
    state = perp_state if perp_state is not None else info.user_state(address.lower())
    summary = state.get("marginSummary", {})
    perp_value = Decimal(str(summary.get("accountValue", "0")))
    margin_used = Decimal(str(summary.get("totalMarginUsed", "0")))
    perp_available = perp_value - margin_used
    spot_usdc = _spot_usdc(info, address)  # always read for display

    if mode == "standard":
        # Spot is a literally separate account under Standard. To use it
        # for perp, the user must transfer (rift trade transfer).
        total = perp_available

    elif mode in ("unified", "portfolio_margin"):
        # HL docs (account-abstraction-modes): "For API users, unified
        # account and portfolio margin shows all balances and holds in
        # the spot clearinghouse state. Individual perp dex user states
        # are not meaningful."
        #
        # So under both modes, the real trading collateral lives in spot.
        # Perp accountValue typically reads as $0 here — we still subtract
        # margin_used in case there's a perp position open.
        #
        # PM specifically allows borrowing against non-USDC collateral
        # (HYPE/BTC/USDH) at oracle * LTV. RIFT v0.1 counts USDC only,
        # because HL doesn't expose the LTV table via any documented info
        # endpoint and the published LTVs are pre-alpha + changeable. A
        # PM user with significant non-USDC collateral will see less
        # available margin in RIFT than HL actually grants — flagged in
        # RELEASE.md and the agent-pair hint.
        total = perp_available + spot_usdc

    else:
        # Unknown future HL mode. Treat as unified (sum) so we don't
        # under-size and reject valid trades. HL will reject the order
        # at the chain if our model is wrong.
        total = perp_available + spot_usdc

    return CollateralBreakdown(
        mode=mode,
        perp_account_value=perp_value,
        perp_margin_used=margin_used,
        perp_available=perp_available,
        spot_usdc=spot_usdc,
        total=total,
    )
