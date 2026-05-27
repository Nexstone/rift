"""Phase 0 key model — types for the three-layer wallet/auth scheme.

This module defines TYPES ONLY. No signing logic, no chain access, no
disk I/O. Other modules (rift_trade.api_wallet, rift_trade.auth) implement
the behavior; this file is the contract every other module agrees on.

Three layers (see docs/PHASE_0.md):

  MainWalletRef       — external wallet (Rabby/MetaMask/hardware) accessed
                         via WalletConnect. RIFT never sees the key.
                         Used for: API wallet registration, withdrawals,
                                   signing RIFT auth tokens at issuance.

  APIWalletKey        — locally-generated keypair. Cannot withdraw funds
                         (chain-enforced by Hyperliquid). Used for: signing
                         every order that hits the chain during trading.

  AuthorizationToken  — RIFT-internal policy envelope. Signed once by main
                         wallet via WC at issuance, then sits on disk and
                         gates which trades execute_proposal() will pass
                         through to the API wallet for signing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ─── Actors ───────────────────────────────────────────────────────────

class ActorKind(str, Enum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class Actor(BaseModel):
    """Who initiated a decision. Recorded in every audit entry."""
    model_config = ConfigDict(frozen=True)

    kind: ActorKind
    id: str = Field(
        ...,
        description="Wallet address (human), agent ID (e.g. 'claude-code-session-xyz'), or system component name",
        min_length=1,
        max_length=128,
    )
    session_id: str | None = Field(
        default=None,
        description="Links multiple decisions made in one agent loop (e.g. a single Claude conversation)",
    )


# ─── Network ──────────────────────────────────────────────────────────
#
# Mainnet-only. RIFT used to support testnet for development; that path
# was removed because (a) testnet liquidity is too thin for any useful
# signal validation, and (b) the codebase already has `rift backtest`,
# `rift simulate`, and `rift test-trade` which give better-than-testnet
# safety using real mainnet data without risking funds. Kept as a
# single-value enum to preserve the schema shape and let serializers
# round-trip cleanly.

class Network(str, Enum):
    MAINNET = "mainnet"


# ─── Wallet references ────────────────────────────────────────────────

class MainWalletRef(BaseModel):
    """A reference to the operator's main wallet. RIFT NEVER stores the key —
    we only know the address and which network it's paired against."""
    model_config = ConfigDict(frozen=True)

    address: str = Field(..., pattern=r"^0x[a-fA-F0-9]{40}$", description="EIP-55 address")
    network: Network

    @field_validator("address")
    @classmethod
    def lowercase_address(cls, v: str) -> str:
        # Store all addresses lowercase for consistent comparison. UIs can
        # render checksummed if they want.
        return v.lower()


class APIWalletKey(BaseModel):
    """A locally-generated trading agent key. The private key IS stored on
    disk at ~/.rift/credentials — this is safe because Hyperliquid's
    protocol enforces that API wallets can place trades but cannot
    withdraw funds."""
    model_config = ConfigDict(frozen=True)

    address: str = Field(..., pattern=r"^0x[a-fA-F0-9]{40}$", description="API wallet's public address")
    private_key: str = Field(
        ...,
        pattern=r"^(0x)?[a-fA-F0-9]{64}$",
        description="32-byte hex private key, with or without 0x prefix",
        repr=False,  # Never include in repr / logs
    )
    network: Network
    name: str = Field(
        default="RIFT",
        max_length=32,
        description="Human-readable name shown in main wallet during approval. Default 'RIFT' for brand recognition.",
    )
    registered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    registered_tx: str | None = Field(
        default=None,
        description="Hyperliquid L1 tx hash of the approveAgent action",
    )
    account_address: str | None = Field(
        default=None,
        pattern=r"^0x[a-fA-F0-9]{40}$",
        description=(
            "Main wallet address (the Hyperliquid account this agent trades for). "
            "Required for trade attribution + maxBuilderFee checks. Captured "
            "from the signer at registration; absent on legacy files."
        ),
    )
    builder_fee_approved: bool | None = Field(
        default=None,
        description=(
            "Whether the main wallet has approved RIFT's builder fee on-chain. "
            "Written after agent-pair confirms approval (or detects existing "
            "approval). The TS CLI gate (`hasFullSetup`) requires this to be "
            "True before allowing live trades."
        ),
    )

    @field_validator("address")
    @classmethod
    def lowercase_address(cls, v: str) -> str:
        return v.lower()

    @field_validator("private_key")
    @classmethod
    def normalize_private_key(cls, v: str) -> str:
        # Strip 0x prefix if present; store as 64 lowercase hex chars
        if v.startswith("0x"):
            v = v[2:]
        return v.lower()

    @field_validator("account_address")
    @classmethod
    def lowercase_account_address(cls, v: str | None) -> str | None:
        return v.lower() if v else v


# ─── Token scope envelope ─────────────────────────────────────────────

class TradeAction(str, Enum):
    OPEN = "open"
    CLOSE = "close"
    MODIFY = "modify"
    CANCEL = "cancel"


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


# Sentinel for "no restriction on this scope dimension"
class _Any(str, Enum):
    ANY = "any"


ANY = _Any.ANY  # Re-exported sentinel; use `scope.coins == ANY` to check


class TokenScope(BaseModel):
    """The envelope that bounds what trades a token authorizes.

    Each dimension can either be a list of allowed values or the special
    sentinel `ANY` (no restriction on that dimension). At least one numeric
    cap must be set — open-ended tokens are forbidden.
    """
    model_config = ConfigDict(frozen=True)

    coins: list[str] | Literal["any"] = Field(
        ...,
        description='Coin names allowed (e.g. ["ETH", "SUI"]) or "any". "any" requires explicit operator opt-in.',
    )
    sides: list[TradeSide] | Literal["any"] = Field(default="any")
    actions: list[TradeAction] | Literal["any"] = Field(default="any")
    strategies: list[str] | Literal["any"] = Field(
        default="any",
        description='Strategy names allowed (e.g. ["trend_follow", "my_strategy"]) or "any". Useful for binding a token to a single strategy.',
    )

    # Numeric caps — at least one must be finite
    max_notional: Decimal = Field(
        ...,
        gt=0,
        description="USD notional ceiling per single action. Required.",
    )
    max_daily: Decimal = Field(
        ...,
        gt=0,
        description="USD gross volume ceiling per UTC day under this token. Required.",
    )
    max_open_risk: Decimal | None = Field(
        default=None,
        gt=0,
        description="Max simultaneous open notional under this token. None = unlimited within other caps.",
    )

    legs: int = Field(
        default=1,
        ge=1,
        le=10,
        description="Max number of legs allowed in a single proposal under this token (multi-leg trades).",
    )

    @field_validator("coins")
    @classmethod
    def normalize_coins(cls, v):
        if v == "any":
            return "any"
        # Uppercase, dedupe, sort for deterministic comparison
        return sorted(set(c.upper() for c in v))


# ─── Authorization token ──────────────────────────────────────────────

class AuthorizationToken(BaseModel):
    """An operator-signed envelope that gates which trades execute_proposal()
    will pass through to the API wallet for signing.

    The signature is produced ONCE by the main wallet via WalletConnect at
    issuance time. After issuance the token sits on disk; subsequent T3
    actions just check the token's scope + signature, no WC contact needed."""
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = Field(
        default=None,
        description="UTC expiry. None = long-lived (only revocable, no auto-expiry).",
    )
    issuer: str = Field(
        ...,
        pattern=r"^0x[a-fA-F0-9]{40}$",
        description="Main wallet address that signed this token (the WC-paired wallet)",
    )

    scope: TokenScope

    signature: str = Field(
        ...,
        pattern=r"^(0x)?[a-fA-F0-9]{130}$",
        description="65-byte ECDSA signature from main wallet over canonical token bytes (hex, with or without 0x prefix)",
        repr=False,
    )
    revoked: bool = Field(default=False, description="Operator can flip this; check at use time")

    @field_validator("issuer")
    @classmethod
    def lowercase_issuer(cls, v: str) -> str:
        return v.lower()

    @field_validator("signature")
    @classmethod
    def normalize_signature(cls, v: str) -> str:
        if v.startswith("0x"):
            v = v[2:]
        return v.lower()

    def is_expired(self, now: datetime | None = None) -> bool:
        """True if this token has passed its expiry. Long-lived tokens never expire."""
        if self.expires_at is None:
            return False
        if now is None:
            now = datetime.now(timezone.utc)
        return now >= self.expires_at

    def is_valid(self, now: datetime | None = None) -> bool:
        """True if token is neither revoked nor expired."""
        return not self.revoked and not self.is_expired(now)


# Constants for canonical signing message (used by rift_trade.auth at issuance
# and rift_trade.execute at verify time). Kept here so both producers and
# verifiers agree on the format.
TOKEN_SIGNING_DOMAIN = "rift.auth.v1"
TOKEN_SIGNING_HEADER = b"RIFT-AUTH-TOKEN\x01"
