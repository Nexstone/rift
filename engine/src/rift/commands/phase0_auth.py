"""Auth commands — wraps rift_trade.api_wallet + rift_trade.auth.

Adds the following commands to the unified Typer app:

  rift agent-status                                show API wallet + recent tokens
  rift agent-pair --local-main-key <hex>           generate API wallet + register on HL
                                                   (LocalKeySigner; WalletConnect support pending)
  rift agent-rotate --local-main-key <hex>         revoke old API wallet, register a new one
  rift token-issue --coins <list> --max-notional <usd> --max-daily <usd>
                                                   issue an auth token signed by main wallet
  rift token-list                                  list issued tokens (newest first)
  rift token-revoke <token-id>                     mark a token revoked
  rift token-show <token-id>                       inspect one token's full state

All commands emit NDJSON results so the TS CLI can parse them. They write
to the canonical paths (~/.rift/credentials, ~/.rift/tokens/) — no config
override flags from the CLI (use the Python API directly for that).
"""

from __future__ import annotations

import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import typer

from rift.commands._shared import app, _emit, _hint


# ─── rift agent-status ────────────────────────────────────────────────

@app.command("agent-status")
def agent_status() -> None:
    """Show current API wallet registration + recent token activity."""
    from rift_trade.api_wallet import has_api_wallet, load_api_wallet
    from rift_trade.auth import list_tokens

    wallet = load_api_wallet()
    if wallet is None:
        _emit({
            "type": "result", "command": "agent-status",
            "registered": False,
            "msg": "No API wallet registered. Run: rift agent-pair --local-main-key <hex>",
        })
        return

    tokens = list_tokens()
    token_summary = [
        {
            "id": str(t.id),
            "issuer": t.issuer,
            "issued_at": t.issued_at.isoformat(),
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            "revoked": t.revoked,
            "valid": t.is_valid(),
            "scope_coins": t.scope.coins,
            "scope_max_notional": str(t.scope.max_notional),
            "scope_max_daily": str(t.scope.max_daily),
        }
        for t in tokens[:10]  # 10 most recent
    ]

    _emit({
        "type": "result", "command": "agent-status",
        "registered": True,
        "agent": {
            "address": wallet.address,
            "network": wallet.network.value,
            "name": wallet.name,
            "registered_at": wallet.registered_at.isoformat(),
            "registered_tx": wallet.registered_tx,
        },
        "tokens": {
            "total": len(tokens),
            "active": sum(1 for t in tokens if t.is_valid()),
            "recent": token_summary,
        },
    })


# ─── rift agent-pair ──────────────────────────────────────────────────

@app.command("agent-pair")
def agent_pair(
    local_main_key: str = typer.Option(
        ..., "--local-main-key",
        help="Main wallet private key (hex, with or without 0x). WalletConnect support pending.",
    ),
    agent_name: str = typer.Option(
        "RIFT", "--agent-name",
        help='Name shown in main wallet during approval signing. Default "RIFT" for brand recognition.',
    ),
) -> None:
    """Generate a new API wallet and register it on Hyperliquid.

    The main wallet signs the approveAgent action. After successful
    registration the API wallet's private key is saved at
    ~/.rift/credentials (file perms 0600). All subsequent trades sign with
    that key — the main wallet stays untouched until you withdraw funds
    or issue a new auth token.
    """
    from rift_core.keys import Network
    from rift_trade.api_wallet import (
        LocalKeySigner, generate_api_wallet, register_api_wallet,
        save_api_wallet, RegistrationError,
        has_api_wallet,
    )

    if has_api_wallet():
        _emit({"type": "warning", "msg":
               "API wallet already registered. Use `rift agent-rotate` to replace it."})
        # Don't bail — let user re-run intentionally if they really want to overwrite

    net = Network.MAINNET

    _emit({"type": "progress", "pct": 10, "msg": "Generating API wallet keypair..."})
    api_wallet = generate_api_wallet(network=net, name=agent_name)

    _emit({"type": "progress", "pct": 30,
           "msg": f"Generated API wallet: {api_wallet.address}"})

    try:
        signer = LocalKeySigner(local_main_key)
    except ValueError as e:
        _emit({"type": "error", "msg": f"Invalid main wallet key: {e}"})
        raise typer.Exit(code=1)

    _emit({"type": "progress", "pct": 50,
           "msg": f"Main wallet (LocalKeySigner): {signer.address}"})

    _emit({"type": "progress", "pct": 70,
           "msg": "Submitting approveAgent to Hyperliquid mainnet..."})

    try:
        registered = register_api_wallet(api_wallet, signer)
    except RegistrationError as e:
        _emit({"type": "error", "msg": f"Registration failed: {e}",
               "response": e.response})
        raise typer.Exit(code=1)
    except Exception as e:
        _emit({"type": "error", "msg": f"Unexpected error during registration: {e}"})
        raise typer.Exit(code=1)

    _emit({"type": "progress", "pct": 80, "msg": "Saving API wallet to disk..."})
    save_api_wallet(registered)

    # Builder fee approval — every order placed via T3 includes a `builder`
    # param. Hyperliquid's maxBuilderFee check requires the main wallet to
    # have approved that builder address first; without this approval the
    # first live order will be rejected by the exchange. Idempotent: check
    # first, only submit if not already approved. Non-fatal: surface the
    # state but don't block agent registration if it fails (user can retry
    # via `rift admin approve-builder-fee`).
    from rift_trade.builder_fee import (
        BUILDER_ADDRESS, BUILDER_FEE_F_PERP,
        approve_builder_fee, check_builder_approval,
    )
    _emit({"type": "progress", "pct": 90,
           "msg": f"Checking builder fee approval for {BUILDER_ADDRESS[:10]}…"})
    builder_approval = {"attempted": False, "approved": False, "error": None}
    try:
        existing = check_builder_approval(signer.address)
        if existing.get("approved") and existing.get("max_fee_tenths_bps", 0) >= BUILDER_FEE_F_PERP:
            builder_approval = {
                "attempted": False, "approved": True,
                "max_fee_tenths_bps": existing["max_fee_tenths_bps"],
                "note": "already approved",
            }
        else:
            _emit({"type": "progress", "pct": 95,
                   "msg": "Approving builder fee (one-time main-wallet sig)…"})
            approve_builder_fee(
                private_key=local_main_key,
                account_address=signer.address,
            )
            builder_approval = {"attempted": True, "approved": True}
    except Exception as e:
        builder_approval = {"attempted": True, "approved": False, "error": str(e)}

    # Persist the builder-fee status to the credentials file. The TS CLI gate
    # (`hasFullSetup` in credentials.ts) reads this flag — without it, the
    # TS-side trade/algo commands refuse to run even though the on-chain
    # approval is in place. Re-save with the updated model.
    if builder_approval.get("approved"):
        registered_with_fee = registered.model_copy(
            update={"builder_fee_approved": True}
        )
        save_api_wallet(registered_with_fee)
        registered = registered_with_fee

    # Detect HL account abstraction mode so the user knows what they're in
    # before they try to trade. Standard mode = perp balance only; Unified
    # = spot USDC counts as perp collateral; Portfolio Margin = full pool.
    # Non-fatal: if HL info query fails we just report "unknown".
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    from rift_data.account_mode import query_account_mode
    info_base_url = constants.MAINNET_API_URL
    try:
        info_client = Info(info_base_url, skip_ws=True)
        detected_mode = query_account_mode(info_client, signer.address)
    except Exception:
        detected_mode = "unknown"

    _emit({
        "type": "result", "command": "agent-pair",
        "success": True,
        "agent": {
            "address": registered.address,
            "network": registered.network.value,
            "name": registered.name,
            "registered_tx": registered.registered_tx,
        },
        "main_wallet": signer.address,
        "builder_approval": builder_approval,
        "account_mode": detected_mode,
    })
    if not builder_approval["approved"]:
        _hint(
            "Agent registered but builder fee NOT approved — first algo trade will fail. "
            "Retry: rift admin approve-builder-fee <main-key>"
        )
    else:
        _hint(f"Agent {registered.address[:10]}… registered. Issue a trading token: rift token-issue --coins ETH --max-notional 500 --max-daily 2000")
    if detected_mode == "unified":
        _hint(
            "Account mode: unified. Your spot USDC IS perp collateral — no transfer needed. "
            "Spot positions (BTC, HYPE, etc.) will also serve as margin. "
            "To isolate per-strategy capital, switch: rift account-mode-set standard --local-main-key <key>"
        )
    elif detected_mode == "portfolio_margin":
        _hint(
            "Account mode: portfolio_margin. Eligible collateral (USDC + HYPE/BTC/USDH at LTV) is pooled. "
            "Note: RIFT v0.1 only counts USDC; non-USDC collateral may be under-counted in gate sizing."
        )
    elif detected_mode == "unknown":
        _hint(
            "Account mode could not be detected. RIFT will treat as 'unified' for safety. "
            "If your HL info endpoint is reachable, run: rift account-mode-status <address>"
        )


# ─── rift agent-rotate ────────────────────────────────────────────────

@app.command("agent-rotate")
def agent_rotate(
    local_main_key: str = typer.Option(
        ..., "--local-main-key",
        help="Main wallet private key (hex). WalletConnect support pending.",
    ),
    agent_name: str = typer.Option(
        "RIFT", "--agent-name",
        help='Name for the new agent. Default "RIFT".',
    ),
) -> None:
    """Revoke the current API wallet and register a new one.

    Use when:
      - You suspect the API wallet key was leaked
      - You want a fresh agent for hygiene
      - The existing agent was revoked by another tool

    After this, all previously-issued auth tokens become useless because
    they're tied to the old API wallet's address — issue new ones.
    """
    from rift_trade.api_wallet import (
        LocalKeySigner, generate_api_wallet,
        load_api_wallet, register_api_wallet, revoke_api_wallet,
        save_api_wallet, RegistrationError,
    )

    existing = load_api_wallet()
    if existing is None:
        _emit({"type": "error", "msg":
               "No API wallet to rotate. Use `rift agent-pair` to set one up."})
        raise typer.Exit(code=1)

    try:
        signer = LocalKeySigner(local_main_key)
    except ValueError as e:
        _emit({"type": "error", "msg": f"Invalid main wallet key: {e}"})
        raise typer.Exit(code=1)

    _emit({"type": "progress", "pct": 25,
           "msg": f"Revoking existing agent {existing.address[:10]}…"})
    try:
        revoke_api_wallet(existing, signer)
    except RegistrationError as e:
        _emit({"type": "error", "msg": f"Revocation failed: {e}"})
        raise typer.Exit(code=1)

    _emit({"type": "progress", "pct": 50, "msg": "Generating new API wallet..."})
    new_wallet = generate_api_wallet(network=existing.network, name=agent_name)

    _emit({"type": "progress", "pct": 75,
           "msg": f"Registering new agent {new_wallet.address[:10]}…"})
    try:
        registered = register_api_wallet(new_wallet, signer)
    except RegistrationError as e:
        _emit({"type": "error", "msg": f"New registration failed: {e}"})
        raise typer.Exit(code=1)

    save_api_wallet(registered)

    _emit({
        "type": "result", "command": "agent-rotate",
        "success": True,
        "old_agent": existing.address,
        "new_agent": registered.address,
        "registered_tx": registered.registered_tx,
    })
    _hint("All previously-issued auth tokens are now useless. Re-issue tokens: rift token-issue ...")


# ─── rift token-issue ─────────────────────────────────────────────────

@app.command("token-issue")
def token_issue(
    coins: str = typer.Option(..., "--coins", help='Comma-separated coin list, e.g. "ETH,SUI". Use "any" for unrestricted.'),
    max_notional: str = typer.Option(..., "--max-notional", help="USD ceiling per single trade."),
    max_daily: str = typer.Option(..., "--max-daily", help="USD ceiling per UTC day under this token."),
    local_main_key: str = typer.Option(..., "--local-main-key", help="Main wallet private key (hex)."),
    mode: str = typer.Option("session", "--mode", help="'per-trade', 'session', or 'long-lived'."),
    expires_hours: float = typer.Option(0, "--expires-hours", help="Custom expiry in hours. 0 = mode default."),
    sides: str = typer.Option("any", "--sides", help='"buy", "sell", "buy,sell", or "any".'),
    strategies: str = typer.Option("any", "--strategies", help='Strategy name(s) or "any".'),
    legs: int = typer.Option(1, "--legs", help="Max legs per proposal under this token (1-10)."),
) -> None:
    """Issue a new authorization token signed by the main wallet."""
    from rift_core.keys import TokenScope, TradeSide
    from rift_trade.auth import IssuanceMode, LocalTokenSigner, issue_token, AuthError

    coin_list = "any" if coins.strip().lower() == "any" else [
        c.strip().upper() for c in coins.split(",") if c.strip()
    ]
    side_list: "list[TradeSide] | str"
    if sides.strip().lower() == "any":
        side_list = "any"
    else:
        side_map = {"buy": TradeSide.BUY, "sell": TradeSide.SELL}
        side_list = [side_map[s.strip().lower()] for s in sides.split(",")]
    strategy_list = "any" if strategies.strip().lower() == "any" else [
        s.strip() for s in strategies.split(",") if s.strip()
    ]

    try:
        scope = TokenScope(
            coins=coin_list,
            sides=side_list,
            strategies=strategy_list,
            max_notional=Decimal(max_notional),
            max_daily=Decimal(max_daily),
            legs=legs,
        )
    except Exception as e:
        _emit({"type": "error", "msg": f"Invalid scope: {e}"})
        raise typer.Exit(code=1)

    try:
        signer = LocalTokenSigner(local_main_key)
    except ValueError as e:
        _emit({"type": "error", "msg": f"Invalid main wallet key: {e}"})
        raise typer.Exit(code=1)

    try:
        issuance_mode = IssuanceMode(mode)
    except ValueError:
        _emit({"type": "error", "msg":
               f"Unknown mode '{mode}'. Use 'per-trade', 'session', or 'long-lived'."})
        raise typer.Exit(code=1)

    expires_at = None
    if expires_hours > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_hours)

    try:
        token = issue_token(
            scope=scope, signer=signer, issuance_mode=issuance_mode,
            expires_at=expires_at,
        )
    except AuthError as e:
        _emit({"type": "error", "msg": f"Issuance failed: {e}"})
        raise typer.Exit(code=1)

    _emit({
        "type": "result", "command": "token-issue",
        "token": {
            "id": str(token.id),
            "issuer": token.issuer,
            "issued_at": token.issued_at.isoformat(),
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
            "mode": issuance_mode.value,
            "scope": {
                "coins": token.scope.coins,
                "sides": (
                    [s.value for s in token.scope.sides] if isinstance(token.scope.sides, list)
                    else token.scope.sides
                ),
                "max_notional": str(token.scope.max_notional),
                "max_daily": str(token.scope.max_daily),
                "legs": token.scope.legs,
            },
        },
    })


# ─── rift token-list ──────────────────────────────────────────────────

@app.command("token-list")
def token_list(
    limit: int = typer.Option(20, "--limit", help="Max tokens to return."),
    include_revoked: bool = typer.Option(False, "--include-revoked", help="Include revoked tokens in output."),
) -> None:
    """List recently-issued authorization tokens (newest first)."""
    from rift_trade.auth import list_tokens
    tokens = list_tokens()
    if not include_revoked:
        tokens = [t for t in tokens if not t.revoked]
    out = []
    for t in tokens[:limit]:
        out.append({
            "id": str(t.id),
            "issued_at": t.issued_at.isoformat(),
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            "revoked": t.revoked,
            "valid": t.is_valid(),
            "scope_coins": t.scope.coins,
            "scope_max_notional": str(t.scope.max_notional),
            "scope_max_daily": str(t.scope.max_daily),
        })
    _emit({"type": "result", "command": "token-list", "tokens": out, "total": len(tokens)})


# ─── rift token-revoke ────────────────────────────────────────────────

@app.command("token-revoke")
def token_revoke(
    token_id: str = typer.Argument(..., help="Token UUID to revoke."),
) -> None:
    """Mark an authorization token revoked.

    Offline operation — flips the local file's `revoked` flag. The token's
    signature remains cryptographically valid, but `execute_proposal()`
    will reject it because `is_valid()` returns False.

    For true unforgeable revocation (e.g. if you suspect a token file was
    copied off-disk), rotate the API wallet itself via `rift agent-rotate`.
    """
    from rift_trade.auth import revoke_token

    try:
        tid = UUID(token_id)
    except ValueError:
        _emit({"type": "error", "msg": f"Invalid token id: {token_id}"})
        raise typer.Exit(code=1)

    result = revoke_token(tid)
    if result is None:
        _emit({"type": "error", "msg": f"Token {token_id} not found."})
        raise typer.Exit(code=1)

    _emit({"type": "result", "command": "token-revoke",
           "success": True, "token_id": str(tid), "revoked": result.revoked})


# ─── rift token-show ──────────────────────────────────────────────────

@app.command("token-show")
def token_show(
    token_id: str = typer.Argument(..., help="Token UUID to show."),
) -> None:
    """Show full details for one token (scope, signature verification, validity)."""
    from rift_trade.auth import load_token, verify_token_signature

    try:
        tid = UUID(token_id)
    except ValueError:
        _emit({"type": "error", "msg": f"Invalid token id: {token_id}"})
        raise typer.Exit(code=1)

    t = load_token(tid)
    if t is None:
        _emit({"type": "error", "msg": f"Token {token_id} not found."})
        raise typer.Exit(code=1)

    _emit({
        "type": "result", "command": "token-show",
        "token": {
            "id": str(t.id),
            "issuer": t.issuer,
            "issued_at": t.issued_at.isoformat(),
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            "revoked": t.revoked,
            "valid": t.is_valid(),
            "expired": t.is_expired(),
            "signature_verifies": verify_token_signature(t),
            "scope": {
                "coins": t.scope.coins,
                "sides": (
                    [s.value for s in t.scope.sides] if isinstance(t.scope.sides, list)
                    else t.scope.sides
                ),
                "actions": (
                    [a.value for a in t.scope.actions] if isinstance(t.scope.actions, list)
                    else t.scope.actions
                ),
                "strategies": t.scope.strategies,
                "max_notional": str(t.scope.max_notional),
                "max_daily": str(t.scope.max_daily),
                "max_open_risk": str(t.scope.max_open_risk) if t.scope.max_open_risk else None,
                "legs": t.scope.legs,
            },
        },
    })


# ─── rift account-mode-status / account-mode-set ──────────────────────

@app.command("account-mode-status")
def account_mode_status(
    address: str = typer.Argument(..., help="Wallet address to check (0x...)"),
) -> None:
    """Show the wallet's HL account abstraction mode + collateral breakdown."""
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    from rift_data.account_mode import read_collateral

    base_url = constants.MAINNET_API_URL
    info = Info(base_url, skip_ws=True)
    c = read_collateral(info, address)
    _emit({
        "type": "result", "command": "account-mode-status",
        "address": address.lower(),
        "network": "mainnet",
        "mode": c.mode,
        "collateral": {
            "perp_account_value": str(c.perp_account_value),
            "perp_margin_used": str(c.perp_margin_used),
            "perp_available": str(c.perp_available),
            "spot_usdc": str(c.spot_usdc),
            "tradeable_total": str(c.total),
            "perp_only": c.perp_only,
        },
    })


@app.command("account-mode-set")
def account_mode_set(
    mode: str = typer.Argument(
        ..., help="Target mode: 'standard' | 'unified' | 'portfolio_margin'"
    ),
    local_main_key: str = typer.Option(
        ..., "--local-main-key",
        help="Main wallet private key (hex, with or without 0x).",
    ),
) -> None:
    """Switch the wallet's HL account abstraction mode.

    Maps friendly names to HL-native:
      standard         → 'disabled'         (separate spot/perp balances)
      unified          → 'unifiedAccount'   (spot USDC IS perp collateral)
      portfolio_margin → 'portfolioMargin'  (requires $10k account value)

    IMPORTANT side effects:
      - Standard → Unified: HL auto-consolidates perp USDC into spot.
      - Unified → Standard: USDC stays in spot. You must run
        `rift trade transfer <amount> --to-perp` afterward to fund perp.

    Fails loud on HL errors (e.g. PM minimums) — unlike the raw SDK
    which silently returns status:err.
    """
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    from rift_data.account_mode import hl_native_mode, query_account_mode

    try:
        hl_target = hl_native_mode(mode)
    except ValueError as e:
        _emit({"type": "error", "msg": str(e)})
        raise typer.Exit(code=1)

    if not local_main_key.startswith("0x"):
        local_main_key = "0x" + local_main_key
    wallet = Account.from_key(local_main_key)
    base_url = constants.MAINNET_API_URL
    info = Info(base_url, skip_ws=True)
    ex = Exchange(wallet, base_url)

    current = query_account_mode(info, wallet.address)
    _emit({"type": "progress", "msg": f"Current mode: {current}. Switching to: {mode}..."})

    if current == mode:
        _emit({
            "type": "result", "command": "account-mode-set",
            "success": True, "mode": mode, "changed": False,
            "note": "Already in target mode; no action taken.",
        })
        return

    result = ex.user_set_abstraction(wallet.address.lower(), hl_target)
    if not isinstance(result, dict) or result.get("status") != "ok":
        # Surface HL's err message verbatim — e.g. PM minimums.
        msg = result.get("response") if isinstance(result, dict) else str(result)
        _emit({
            "type": "error", "command": "account-mode-set",
            "msg": f"Hyperliquid rejected mode change: {msg}",
            "hl_response": result,
        })
        raise typer.Exit(code=1)

    # Re-query to confirm the change actually took effect.
    new_mode = query_account_mode(info, wallet.address)
    hints: list[str] = []
    if current == "unified" and mode == "standard":
        hints.append(
            "Unified→Standard: your USDC stayed in spot. "
            "Run `rift trade transfer <amount> --to-perp` to fund perp trading."
        )
    if mode == "unified" and current == "standard":
        hints.append(
            "Standard→Unified: HL auto-consolidates perp USDC into spot. "
            "Your spot USDC is now your perp margin pool."
        )

    _emit({
        "type": "result", "command": "account-mode-set",
        "success": new_mode == mode,
        "previous_mode": current,
        "mode": new_mode,
        "changed": True,
    })
    for h in hints:
        _hint(h)
