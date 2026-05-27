"""Unit tests for rift_core.keys — Phase 0 type contracts.

Tests verify construction, validation, normalization, json roundtrip.
No signing logic here — that lives in rift_trade.auth and is tested there.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from rift_core.keys import (
    ANY,
    APIWalletKey,
    Actor,
    ActorKind,
    AuthorizationToken,
    MainWalletRef,
    Network,
    TokenScope,
    TradeAction,
    TradeSide,
)


# ─── Actor ────────────────────────────────────────────────────────────

class TestActor:
    def test_construction_minimal(self):
        a = Actor(kind=ActorKind.HUMAN, id="0xabc")
        assert a.kind == ActorKind.HUMAN
        assert a.id == "0xabc"
        assert a.session_id is None

    def test_session_id_optional(self):
        a = Actor(kind=ActorKind.AGENT, id="claude-xyz", session_id="conv-123")
        assert a.session_id == "conv-123"

    def test_empty_id_rejected(self):
        with pytest.raises(ValidationError):
            Actor(kind=ActorKind.HUMAN, id="")

    def test_string_too_long_rejected(self):
        with pytest.raises(ValidationError):
            Actor(kind=ActorKind.HUMAN, id="x" * 200)

    def test_invalid_kind_rejected(self):
        with pytest.raises(ValidationError):
            Actor(kind="superuser", id="x")  # type: ignore

    def test_frozen(self):
        a = Actor(kind=ActorKind.SYSTEM, id="watchdog")
        with pytest.raises(ValidationError):
            a.id = "other"  # type: ignore  # frozen

    def test_json_roundtrip(self):
        a = Actor(kind=ActorKind.AGENT, id="bot", session_id="s1")
        roundtripped = Actor.model_validate_json(a.model_dump_json())
        assert roundtripped == a


# ─── MainWalletRef ────────────────────────────────────────────────────

class TestMainWalletRef:
    def test_valid_address(self):
        w = MainWalletRef(address="0xAbCdEf1234567890aBcDeF1234567890AbCdEf12", network=Network.MAINNET)
        # Stored lowercase
        assert w.address == "0xabcdef1234567890abcdef1234567890abcdef12"

    def test_invalid_address_format(self):
        with pytest.raises(ValidationError):
            MainWalletRef(address="not-an-address", network=Network.MAINNET)

    def test_short_address(self):
        with pytest.raises(ValidationError):
            MainWalletRef(address="0xabc", network=Network.MAINNET)

    def test_network_required(self):
        with pytest.raises(ValidationError):
            MainWalletRef(address="0xAbCdEf1234567890aBcDeF1234567890AbCdEf12")  # type: ignore

    def test_frozen(self):
        w = MainWalletRef(address="0x" + "a" * 40, network=Network.MAINNET)
        with pytest.raises(ValidationError):
            w.network = Network.MAINNET  # type: ignore


# ─── APIWalletKey ─────────────────────────────────────────────────────

class TestAPIWalletKey:
    VALID_KEY = "0x" + "b" * 64
    VALID_ADDR = "0x" + "c" * 40

    def test_construction(self):
        k = APIWalletKey(address=self.VALID_ADDR, private_key=self.VALID_KEY, network=Network.MAINNET)
        assert k.name == "RIFT"  # default
        assert k.registered_tx is None  # default
        assert isinstance(k.registered_at, datetime)

    def test_default_name_is_rift_uppercase(self):
        """Brand recognition: default agent name shown in main wallet's signing prompt."""
        k = APIWalletKey(address=self.VALID_ADDR, private_key=self.VALID_KEY, network=Network.MAINNET)
        assert k.name == "RIFT"

    def test_private_key_normalized_no_prefix(self):
        with_prefix = APIWalletKey(address=self.VALID_ADDR, private_key="0x" + "a" * 64, network=Network.MAINNET)
        without_prefix = APIWalletKey(address=self.VALID_ADDR, private_key="a" * 64, network=Network.MAINNET)
        assert with_prefix.private_key == without_prefix.private_key
        assert not with_prefix.private_key.startswith("0x")

    def test_private_key_must_be_64_hex(self):
        with pytest.raises(ValidationError):
            APIWalletKey(address=self.VALID_ADDR, private_key="too-short", network=Network.MAINNET)

    def test_repr_does_not_leak_private_key(self):
        k = APIWalletKey(address=self.VALID_ADDR, private_key=self.VALID_KEY, network=Network.MAINNET)
        assert self.VALID_KEY.lstrip("0x") not in repr(k)
        assert "b" * 64 not in repr(k)

    def test_name_length_limit(self):
        with pytest.raises(ValidationError):
            APIWalletKey(address=self.VALID_ADDR, private_key=self.VALID_KEY,
                         network=Network.MAINNET, name="x" * 100)


# ─── TokenScope ───────────────────────────────────────────────────────

class TestTokenScope:
    def test_minimal_valid_scope(self):
        s = TokenScope(coins=["ETH"], max_notional=Decimal("500"), max_daily=Decimal("2000"))
        assert s.coins == ["ETH"]
        assert s.sides == "any"
        assert s.legs == 1

    def test_coins_normalized_uppercase_sorted(self):
        s = TokenScope(coins=["eth", "btc", "ETH"], max_notional=Decimal("100"), max_daily=Decimal("500"))
        assert s.coins == ["BTC", "ETH"]  # uppercased, deduped, sorted

    def test_coins_any_passes_through(self):
        s = TokenScope(coins="any", max_notional=Decimal("100"), max_daily=Decimal("500"))
        assert s.coins == "any"

    def test_max_notional_required(self):
        with pytest.raises(ValidationError):
            TokenScope(coins=["ETH"], max_daily=Decimal("500"))  # type: ignore

    def test_max_notional_must_be_positive(self):
        with pytest.raises(ValidationError):
            TokenScope(coins=["ETH"], max_notional=Decimal("0"), max_daily=Decimal("500"))

    def test_max_open_risk_optional(self):
        s = TokenScope(coins=["ETH"], max_notional=Decimal("100"), max_daily=Decimal("500"))
        assert s.max_open_risk is None

    def test_legs_range(self):
        with pytest.raises(ValidationError):
            TokenScope(coins=["ETH"], max_notional=Decimal("100"), max_daily=Decimal("500"), legs=0)
        with pytest.raises(ValidationError):
            TokenScope(coins=["ETH"], max_notional=Decimal("100"), max_daily=Decimal("500"), legs=11)

    def test_default_legs_is_one(self):
        s = TokenScope(coins=["ETH"], max_notional=Decimal("100"), max_daily=Decimal("500"))
        assert s.legs == 1

    def test_json_roundtrip(self):
        s = TokenScope(
            coins=["ETH", "SUI"],
            sides=[TradeSide.BUY],
            actions=[TradeAction.OPEN, TradeAction.CLOSE],
            strategies=["trend_follow"],
            max_notional=Decimal("500.5"),
            max_daily=Decimal("2000"),
            max_open_risk=Decimal("1000"),
            legs=3,
        )
        roundtripped = TokenScope.model_validate_json(s.model_dump_json())
        assert roundtripped == s


# ─── AuthorizationToken ───────────────────────────────────────────────

class TestAuthorizationToken:
    VALID_ISSUER = "0x" + "d" * 40
    VALID_SIG = "0x" + "e" * 130
    SCOPE = TokenScope(coins=["ETH"], max_notional=Decimal("500"), max_daily=Decimal("2000"))

    def test_construction_minimal(self):
        t = AuthorizationToken(issuer=self.VALID_ISSUER, scope=self.SCOPE, signature=self.VALID_SIG)
        assert isinstance(t.id, UUID)
        assert t.expires_at is None  # long-lived by default
        assert t.revoked is False

    def test_id_auto_generated_unique(self):
        t1 = AuthorizationToken(issuer=self.VALID_ISSUER, scope=self.SCOPE, signature=self.VALID_SIG)
        t2 = AuthorizationToken(issuer=self.VALID_ISSUER, scope=self.SCOPE, signature=self.VALID_SIG)
        assert t1.id != t2.id

    def test_signature_normalized(self):
        with_prefix = AuthorizationToken(
            issuer=self.VALID_ISSUER, scope=self.SCOPE, signature="0x" + "1" * 130)
        without_prefix = AuthorizationToken(
            issuer=self.VALID_ISSUER, scope=self.SCOPE, signature="1" * 130)
        assert with_prefix.signature == without_prefix.signature

    def test_signature_must_be_130_hex(self):
        with pytest.raises(ValidationError):
            AuthorizationToken(issuer=self.VALID_ISSUER, scope=self.SCOPE, signature="0xshort")

    def test_repr_does_not_leak_signature(self):
        t = AuthorizationToken(issuer=self.VALID_ISSUER, scope=self.SCOPE, signature=self.VALID_SIG)
        assert "e" * 130 not in repr(t)

    def test_is_expired_long_lived(self):
        t = AuthorizationToken(issuer=self.VALID_ISSUER, scope=self.SCOPE, signature=self.VALID_SIG)
        assert not t.is_expired()  # no expires_at → never expires

    def test_is_expired_past(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        t = AuthorizationToken(
            issuer=self.VALID_ISSUER, scope=self.SCOPE, signature=self.VALID_SIG, expires_at=past)
        assert t.is_expired()

    def test_is_expired_future(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        t = AuthorizationToken(
            issuer=self.VALID_ISSUER, scope=self.SCOPE, signature=self.VALID_SIG, expires_at=future)
        assert not t.is_expired()

    def test_is_valid_combines_revoked_and_expired(self):
        t = AuthorizationToken(issuer=self.VALID_ISSUER, scope=self.SCOPE, signature=self.VALID_SIG)
        assert t.is_valid()
        revoked = t.model_copy(update={"revoked": True})
        assert not revoked.is_valid()
        expired = t.model_copy(update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
        assert not expired.is_valid()

    def test_json_roundtrip_long_lived(self):
        t = AuthorizationToken(issuer=self.VALID_ISSUER, scope=self.SCOPE, signature=self.VALID_SIG)
        roundtripped = AuthorizationToken.model_validate_json(t.model_dump_json())
        assert roundtripped == t

    def test_json_roundtrip_session_token(self):
        t = AuthorizationToken(
            issuer=self.VALID_ISSUER,
            scope=self.SCOPE,
            signature=self.VALID_SIG,
            expires_at=datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
        )
        j = t.model_dump_json()
        roundtripped = AuthorizationToken.model_validate_json(j)
        assert roundtripped.expires_at == t.expires_at

    def test_issuer_lowercased(self):
        t = AuthorizationToken(
            issuer="0xABCDEF" + "1" * 34, scope=self.SCOPE, signature=self.VALID_SIG)
        assert t.issuer == "0xabcdef" + "1" * 34
