"""Operator-side token issuance + verification.

Issues `AuthorizationToken`s signed by the operator's main wallet via WC.
Once issued, a token sits on disk at `~/.rift/tokens/{id}.json` and gates
which trades `execute_proposal()` will pass through to the API wallet
for signing.

Signing model:
  - Tokens are signed with EIP-191 "personal_sign" over a canonical
    JSON representation of the token data (sorted keys, no whitespace).
    The signing function prepends a domain-separator header so this
    signature cannot be replayed in any other RIFT context.
  - EIP-191 chosen over EIP-712 for simplicity — wallets all support it.
    EIP-712 typed-data signing (which would show structured fields in
    the wallet) is a planned upgrade.

Same signer-agnostic pattern as `api_wallet.py`:
  - `TokenSigner` Protocol defines the contract (`address` + `sign_token_bytes`)
  - `LocalTokenSigner` — dev/test only, signs with a local key
  - `WCTokenSigner` — production, bridges to WC via the TS CLI (step 8)

Verification (used by execute.py at T3 time):
  - `verify_token_signature(token)` recovers the signer from the signature
    and checks it matches `token.issuer`. Returns bool.
  - Caller (execute.py) combines this with `token.is_valid()` (revoked +
    expired check) for the full "is this token usable right now" answer.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from eth_account import Account
from eth_account.messages import encode_defunct

from rift_core.keys import (
    TOKEN_SIGNING_HEADER,
    AuthorizationToken,
    TokenScope,
)


# ─── Constants ────────────────────────────────────────────────────────

DEFAULT_TOKENS_DIR = Path.home() / ".rift" / "tokens"


# Default expiries for each issuance mode
DEFAULT_SESSION_DURATION = timedelta(hours=4)
DEFAULT_PER_TRADE_DURATION = timedelta(minutes=5)
# Long-lived tokens have no time expiry by design.


# ─── Issuance modes ───────────────────────────────────────────────────

class IssuanceMode(str, Enum):
    """Token issuance modes — trade-off between friction and safety.

    PER_TRADE   — single-use, short expiry. Highest friction, highest safety.
                  Operator approves each trade individually.
    SESSION     — time-bounded (default 4h), scoped. Mid-friction.
                  Suitable for active research sessions with operator at keyboard.
    LONG_LIVED  — no time expiry; scope-bounded; revocable. Lowest friction.
                  For validated strategies operator explicitly trusts.
    """
    PER_TRADE = "per-trade"
    SESSION = "session"
    LONG_LIVED = "long-lived"


# ─── TokenSigner Protocol ─────────────────────────────────────────────

class TokenSigner(Protocol):
    """Signs RIFT auth tokens with the operator's main wallet.

    Two implementations:
      LocalTokenSigner — uses a local private key. Dev/test only.
      WCTokenSigner    — bridges to WalletConnect via the TS CLI (step 8).

    Same `address` + `sign_*` shape as `api_wallet.MainWalletSigner`, but
    distinct method (`sign_token_bytes`) because token signing is EIP-191
    personal_sign over arbitrary bytes, not an HL L1 action.
    """

    @property
    def address(self) -> str: ...

    def sign_token_bytes(self, message_bytes: bytes) -> str:
        """Return a hex signature string (with or without 0x prefix).
        Will be normalized to 130 hex chars (no prefix) by AuthorizationToken.
        """
        ...


# ─── Local signer (dev / testing) ─────────────────────────────────────

class LocalTokenSigner:
    """Signs tokens with a local main wallet private key. NOT for production.

    Production operators use WCTokenSigner — the whole goal is to
    avoid storing main wallet keys on disk. This class exists
    for unit tests, integration testnet runs, and the rare operator who
    explicitly opts in via `rift init --local-main-key`.
    """

    def __init__(self, private_key: str):
        if private_key.startswith("0x"):
            private_key = private_key[2:]
        if len(private_key) != 64:
            raise ValueError("Main wallet private key must be 32 bytes (64 hex chars)")
        self._account = Account.from_key("0x" + private_key)

    @property
    def address(self) -> str:
        return self._account.address.lower()

    def sign_token_bytes(self, message_bytes: bytes) -> str:
        encoded = encode_defunct(message_bytes)
        signed = self._account.sign_message(encoded)
        # eth_account returns .signature as bytes; convert to hex without 0x
        return signed.signature.hex()


# ─── Canonical message construction ───────────────────────────────────

def canonical_token_bytes(
    *,
    token_id: UUID,
    issuer: str,
    issued_at_ms: int,
    expires_at_ms: int | None,
    scope: TokenScope,
) -> bytes:
    """Deterministic byte representation of a token for signing.

    Construction:
      TOKEN_SIGNING_HEADER  ←  domain separator (16 bytes, includes version)
      + canonical_json     ←  sorted-keys, no-whitespace JSON of all fields

    The header prevents this signature from being valid in any other
    RIFT context (or any other Ethereum context — EIP-191 prepends its
    own \\x19 separator on top of this).

    Why JSON: humans can verify what they're signing by looking at the
    raw bytes the wallet displays. EIP-712 may eventually replace this for
    nicer structured display in wallets.
    """
    # Normalize scope to plain JSON-able dict with deterministic ordering
    scope_dict = {
        "coins": scope.coins if scope.coins == "any" else sorted(scope.coins),
        "sides": (
            [s.value for s in scope.sides] if isinstance(scope.sides, list)
            else scope.sides
        ),
        "actions": (
            [a.value for a in scope.actions] if isinstance(scope.actions, list)
            else scope.actions
        ),
        "strategies": (
            sorted(scope.strategies) if isinstance(scope.strategies, list)
            else scope.strategies
        ),
        "max_notional": str(scope.max_notional),
        "max_daily": str(scope.max_daily),
        "max_open_risk": None if scope.max_open_risk is None else str(scope.max_open_risk),
        "legs": scope.legs,
    }

    payload = {
        "id": str(token_id),
        "issuer": issuer.lower(),
        "issued_at_ms": issued_at_ms,
        "expires_at_ms": expires_at_ms,
        "scope": scope_dict,
    }

    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return TOKEN_SIGNING_HEADER + canonical_json.encode("utf-8")


# ─── Errors ───────────────────────────────────────────────────────────

class AuthError(RuntimeError):
    """Raised when token issuance or verification fails."""


# ─── Issuance ─────────────────────────────────────────────────────────

def _default_expiry_for_mode(mode: IssuanceMode, now: datetime) -> datetime | None:
    """Apply default expiries when caller didn't specify."""
    if mode == IssuanceMode.LONG_LIVED:
        return None
    if mode == IssuanceMode.SESSION:
        return now + DEFAULT_SESSION_DURATION
    if mode == IssuanceMode.PER_TRADE:
        return now + DEFAULT_PER_TRADE_DURATION
    raise AuthError(f"Unknown issuance mode: {mode}")


def issue_token(
    *,
    scope: TokenScope,
    signer: TokenSigner,
    issuance_mode: IssuanceMode = IssuanceMode.SESSION,
    expires_at: datetime | None = None,
    tokens_dir: Path | None = None,
    save: bool = True,
    _now: datetime | None = None,  # injectable for tests
) -> AuthorizationToken:
    """Build, sign, and persist an authorization token.

    1. Compute issued_at and expires_at (mode-default if not provided)
    2. Build canonical token bytes
    3. Ask signer to sign via EIP-191 personal_sign
    4. Construct the AuthorizationToken with the signature
    5. Optionally persist to ~/.rift/tokens/{id}.json
    6. Return the immutable token

    Raises AuthError on signature failures.
    """
    now = _now or datetime.now(timezone.utc)
    if expires_at is None:
        expires_at = _default_expiry_for_mode(issuance_mode, now)

    # Pre-generate id so we can include it in the canonical bytes
    from uuid import uuid4
    token_id = uuid4()
    issued_at_ms = int(now.timestamp() * 1000)
    expires_at_ms = int(expires_at.timestamp() * 1000) if expires_at else None

    message_bytes = canonical_token_bytes(
        token_id=token_id,
        issuer=signer.address,
        issued_at_ms=issued_at_ms,
        expires_at_ms=expires_at_ms,
        scope=scope,
    )

    try:
        signature_hex = signer.sign_token_bytes(message_bytes)
    except Exception as e:
        raise AuthError(f"Token signing failed: {e}") from e

    token = AuthorizationToken(
        id=token_id,
        issued_at=now,
        expires_at=expires_at,
        issuer=signer.address,
        scope=scope,
        signature=signature_hex,
    )

    if save:
        save_token(token, tokens_dir=tokens_dir)

    return token


# ─── Verification ─────────────────────────────────────────────────────

def verify_token_signature(token: AuthorizationToken) -> bool:
    """Recover signer from token signature and check it matches `token.issuer`.

    Returns True iff the recovered address equals the stored issuer.
    Does NOT check expiry or revocation — caller composes this with
    `token.is_valid()` for the full "usable now" answer.
    """
    message_bytes = canonical_token_bytes(
        token_id=token.id,
        issuer=token.issuer,
        issued_at_ms=int(token.issued_at.timestamp() * 1000),
        expires_at_ms=(
            int(token.expires_at.timestamp() * 1000) if token.expires_at else None
        ),
        scope=token.scope,
    )
    encoded = encode_defunct(message_bytes)
    sig = token.signature
    if not sig.startswith("0x"):
        sig = "0x" + sig
    try:
        recovered = Account.recover_message(encoded, signature=sig)
    except Exception:
        return False
    return recovered.lower() == token.issuer.lower()


# ─── Persistence ──────────────────────────────────────────────────────

def _tokens_dir(override: Path | None = None) -> Path:
    d = override or DEFAULT_TOKENS_DIR
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def save_token(token: AuthorizationToken, tokens_dir: Path | None = None) -> Path:
    """Atomically write a token to disk. Returns the file path.

    File: {tokens_dir}/{id}.json (perms 0600).
    """
    d = _tokens_dir(tokens_dir)
    path = d / f"{token.id}.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(token.model_dump_json())
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def load_token(token_id: UUID, tokens_dir: Path | None = None) -> AuthorizationToken | None:
    """Look up a token by ID. Returns None if missing or corrupt.

    Used by execute.py to fetch the token an agent is presenting.
    """
    d = _tokens_dir(tokens_dir)
    path = d / f"{token_id}.json"
    if not path.exists():
        return None
    try:
        return AuthorizationToken.model_validate_json(path.read_text())
    except Exception:
        return None


def list_tokens(tokens_dir: Path | None = None) -> list[AuthorizationToken]:
    """List all tokens (newest first by file mtime). Skips unparseable files.

    Used by `rift auth list-tokens` and the audit.query MCP tool.
    """
    d = _tokens_dir(tokens_dir)
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[AuthorizationToken] = []
    for f in files:
        try:
            out.append(AuthorizationToken.model_validate_json(f.read_text()))
        except Exception:
            continue
    return out


def revoke_token(token_id: UUID, tokens_dir: Path | None = None) -> AuthorizationToken | None:
    """Flip the `revoked` flag on a stored token. Returns the updated token,
    or None if not found.

    Note: this is an OFFLINE revocation — the local token file gets marked
    revoked, but if an attacker has a copy of the signed token bytes from
    elsewhere they could still use them on a different machine. That's why
    the API wallet exists — even a stolen token can
    only authorize trades within its scope, signed by the local API wallet
    which has chain-enforced no-withdrawal. The main wallet stays safe.

    For true revocation, also rotate the API wallet itself
    (`rift auth rotate-agent`) — that invalidates ALL prior tokens.
    """
    existing = load_token(token_id, tokens_dir=tokens_dir)
    if existing is None:
        return None
    if existing.revoked:
        return existing
    revoked = existing.model_copy(update={"revoked": True})
    save_token(revoked, tokens_dir=tokens_dir)
    return revoked


def delete_token(token_id: UUID, tokens_dir: Path | None = None) -> bool:
    """Remove a token file completely. Returns True if removed, False if missing.

    Use sparingly — revoke_token() is usually safer because it preserves the
    audit trail showing the token was issued and revoked.
    """
    d = _tokens_dir(tokens_dir)
    path = d / f"{token_id}.json"
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
