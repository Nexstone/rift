"""Trading gates — disclaimer, auth, and builder fee checks.

Called before any real trade execution. Gates are persistent:
- Disclaimer: saved to ~/.rift/accepted_disclaimer (once per install)
- Auth: HYPERLIQUID_PRIVATE_KEY environment variable (once per shell/session)
- Builder fee: on-chain approval (once per wallet)

All gates pass silently for returning users.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

RIFT_DIR = Path.home() / ".rift"
DISCLAIMER_FILE = RIFT_DIR / "accepted_disclaimer"
AUTH_FILE = RIFT_DIR / "hl_wallet"


def _emit(data: dict) -> None:
    print(json.dumps(data), flush=True)


def _prompt_stderr(msg: str) -> str:
    """Print to stderr (keeps NDJSON stdout clean), read from stdin."""
    print(msg, end="", file=sys.stderr, flush=True)
    try:
        return input().strip()
    except (EOFError, KeyboardInterrupt):
        return ""


# ──────────────────────────────────────────────────────────
#  Gate 1: Trading Disclaimer
# ──────────────────────────────────────────────────────────
def check_disclaimer() -> bool:
    """Check if user has accepted the trading disclaimer. Returns True if accepted."""
    return DISCLAIMER_FILE.exists()


def require_disclaimer() -> bool:
    """Prompt for disclaimer acceptance if not already accepted. Returns True if accepted."""
    if check_disclaimer():
        return True

    print("\n", file=sys.stderr)
    print("  ══════════════════════════════════════════════════", file=sys.stderr)
    print("  ⚠  TRADING DISCLAIMER", file=sys.stderr)
    print("  ══════════════════════════════════════════════════", file=sys.stderr)
    print("", file=sys.stderr)
    print("  You are about to trade real funds on Hyperliquid.", file=sys.stderr)
    print("  RIFT is experimental open-source software.", file=sys.stderr)
    print("  You can lose your entire position.", file=sys.stderr)
    print("", file=sys.stderr)
    print("  - Signals are probabilistic, not guaranteed.", file=sys.stderr)
    print("  - Past performance does not predict future results.", file=sys.stderr)
    print("  - You are solely responsible for your trades.", file=sys.stderr)
    print("  - A 0.1% builder fee is charged per trade.", file=sys.stderr)
    print("", file=sys.stderr)
    print("  ══════════════════════════════════════════════════", file=sys.stderr)
    print("", file=sys.stderr)

    answer = _prompt_stderr("  Accept and continue? [y/N]: ")

    if answer.lower() in ("y", "yes"):
        RIFT_DIR.mkdir(parents=True, exist_ok=True)
        DISCLAIMER_FILE.write_text(json.dumps({
            "accepted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": "1.0",
        }))
        _emit({"type": "status", "msg": "Disclaimer accepted"})
        return True

    _emit({"type": "status", "msg": "Disclaimer not accepted. Aborting."})
    return False


# ──────────────────────────────────────────────────────────
#  Gate 2: Wallet Auth
# ──────────────────────────────────────────────────────────
def get_api_key() -> str:
    """Get Hyperliquid API wallet private key. Checks in order:
    1. HYPERLIQUID_PRIVATE_KEY env var (explicit)
    2. ~/.rift/.env file
    3. ~/.rift/credentials (canonical — written by agent-pair)
    4. ~/.rift/hl_wallet file (legacy)
    Returns key or empty string.
    """
    from rift_core.config import get_env_var

    # Check env var + .env file
    key = get_env_var("HYPERLIQUID_PRIVATE_KEY")
    if key:
        return key

    # Canonical: ~/.rift/credentials (snake_case, written by agent-pair)
    cred_file = RIFT_DIR / "credentials"
    if cred_file.exists():
        try:
            saved = json.loads(cred_file.read_text())
            key = saved.get("private_key", "")
            if key:
                os.environ["HYPERLIQUID_PRIVATE_KEY"] = key
                return key
        except Exception:
            pass

    # Legacy: ~/.rift/hl_wallet
    if AUTH_FILE.exists():
        try:
            saved = json.loads(AUTH_FILE.read_text())
            key = saved.get("private_key", saved.get("api_key", ""))
            if key:
                os.environ["HYPERLIQUID_PRIVATE_KEY"] = key
                return key
        except Exception:
            pass

    return ""


def setup_auth(key: str = "", account: str = "") -> str:
    """Set up wallet auth non-interactively. Returns key or empty string.

    Args:
        key: Hyperliquid API wallet private key (0x-prefixed)
        account: Main wallet address (optional, derived from key if empty)
    """
    from rift_core.config import set_env_var

    if not key or not key.startswith("0x"):
        _emit({"type": "error", "msg": "Invalid key. Must start with 0x."})
        return ""

    try:
        from eth_account import Account
        wallet = Account.from_key(key)
        address = wallet.address
    except Exception as e:
        _emit({"type": "error", "msg": f"Invalid private key: {e}"})
        return ""

    if not account:
        account = address

    # Save to ~/.rift/.env
    set_env_var("HYPERLIQUID_PRIVATE_KEY", key)
    set_env_var("HYPERLIQUID_ACCOUNT_ADDRESS", account)

    _emit({"type": "status", "msg": f"Wallet configured: {address} (saved to ~/.rift/.env)"})
    return key


def require_auth() -> str:
    """Ensure Hyperliquid private key is available. Guides setup if missing. Returns key or exits."""
    key = get_api_key()
    if key:
        return key

    # Try interactive setup (works in terminal, fails gracefully in MCP)
    print("\n", file=sys.stderr)
    print("  ══════════════════════════════════════════════════", file=sys.stderr)
    print("  WALLET SETUP", file=sys.stderr)
    print("  ══════════════════════════════════════════════════", file=sys.stderr)
    print("", file=sys.stderr)
    print("  No Hyperliquid wallet key found.", file=sys.stderr)
    print("", file=sys.stderr)
    print("  To trade on Hyperliquid, you need an API wallet:", file=sys.stderr)
    print("  1. Go to app.hyperliquid.xyz > API > Create API Wallet", file=sys.stderr)
    print("  2. Copy the private key (starts with 0x)", file=sys.stderr)
    print("", file=sys.stderr)
    print("  Option A: Paste it below (interactive)", file=sys.stderr)
    print("  Option B: Run: rift auth setup --key 0x...", file=sys.stderr)
    print("", file=sys.stderr)
    print("  ══════════════════════════════════════════════════", file=sys.stderr)
    print("", file=sys.stderr)

    try:
        key = _prompt_stderr("  API wallet private key (0x...): ")
    except (EOFError, KeyboardInterrupt):
        _emit({"type": "error", "msg": "No terminal available. Run: rift auth setup --key 0x..."})
        return ""

    return setup_auth(key)


def get_account_address() -> str:
    """Get the main wallet address.

    Lookup order:
      1. HYPERLIQUID_ACCOUNT_ADDRESS env var (explicit override)
      2. ~/.rift/credentials.account_address (the modern canonical location,
         written by agent-pair and the TS auth flow)
      3. ~/.rift/hl_wallet.account_address (legacy)

    Returns "" if no source has it. Callers should NOT fall back to deriving
    from the API wallet's private key — that gives the API wallet address,
    not the main wallet, and breaks builder-fee approval checks.
    """
    from rift_core.config import get_env_var
    addr = get_env_var("HYPERLIQUID_ACCOUNT_ADDRESS")
    if addr:
        return addr

    # Modern: ~/.rift/credentials (snake_case, written by Python agent-pair)
    cred_file = RIFT_DIR / "credentials"
    if cred_file.exists():
        try:
            saved = json.loads(cred_file.read_text())
            if saved.get("account_address"):
                return saved["account_address"]
        except Exception:
            pass

    # Legacy: ~/.rift/hl_wallet
    if AUTH_FILE.exists():
        try:
            saved = json.loads(AUTH_FILE.read_text())
            return saved.get("account_address", "")
        except Exception:
            pass
    return ""


# ──────────────────────────────────────────────────────────
#  Gate 3: Builder Fee Approval
# ──────────────────────────────────────────────────────────
def check_builder_fee(account_address: str) -> bool:
    """Check if builder fee is approved for this wallet."""
    from rift_trade.builder_fee import check_builder_approval
    result = check_builder_approval(account_address)
    return result.get("approved", False)


def require_builder_fee(account_address: str) -> bool:
    """Check builder fee and guide user to approve if needed. Returns True if approved."""
    if check_builder_fee(account_address):
        return True

    from rift_trade.builder_fee import BUILDER_FEE_DISPLAY

    print("\n", file=sys.stderr)
    print("  ══════════════════════════════════════════════════", file=sys.stderr)
    print(f"  💰  BUILDER FEE ({BUILDER_FEE_DISPLAY})", file=sys.stderr)
    print("  ══════════════════════════════════════════════════", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"  RIFT charges a {BUILDER_FEE_DISPLAY} builder fee per trade.", file=sys.stderr)
    print("  This requires a one-time on-chain approval from", file=sys.stderr)
    print("  your MAIN wallet (not the API wallet).", file=sys.stderr)
    print("", file=sys.stderr)
    print("  Run this command with your main wallet key:", file=sys.stderr)
    print("", file=sys.stderr)
    print("  rift approve-builder-fee <main-wallet-private-key>", file=sys.stderr)
    print("", file=sys.stderr)
    print("  ══════════════════════════════════════════════════", file=sys.stderr)
    print("", file=sys.stderr)

    _emit({"type": "error", "msg": f"Builder fee not approved. Run: rift approve-builder-fee <main-wallet-key>"})
    return False


# ──────────────────────────────────────────────────────────
#  Combined gate — runs all checks in order
# ──────────────────────────────────────────────────────────
def require_trading_ready() -> tuple[str, str] | None:
    """Run all trading gates in order. Returns (private_key, account_address) or None.

    Gates:
        1. Disclaimer acceptance (first time only)
        2. Wallet auth (first time only)
        3. Builder fee approval (first time per wallet)

    All gates are persistent — returning users pass through instantly.
    """
    # Pre-check: builder configuration
    from rift_trade.builder_fee import BUILDER_ADDRESS
    if len(BUILDER_ADDRESS) != 42 or BUILDER_ADDRESS[:5] != "0x091":
        return None

    # Gate 1: Disclaimer
    if not require_disclaimer():
        return None

    # Gate 2: Auth
    private_key = require_auth()
    if not private_key:
        return None

    account_address = get_account_address()

    # NEVER derive account_address from the API wallet's private key — that
    # gives the API wallet address, not the main wallet. The main wallet is
    # the one HL's maxBuilderFee check is keyed on, so deriving the wrong
    # address makes builder-fee approval always fail.
    if not account_address:
        print(
            "\n  ✘ Main wallet address not found. Re-pair with:\n"
            "    rift more agent-pair --local-main-key 0x<your main wallet key>\n",
            file=sys.stderr,
        )
        return None

    # Gate 3: Builder fee — always required (mainnet-only).
    if not require_builder_fee(account_address):
        return None

    return private_key, account_address
