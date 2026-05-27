"""Unit tests for rift_trade.auth — Phase 0 step 6.

Tests cover:
  - Issuance for all three modes (per-trade, session, long-lived)
  - Custom expiry override
  - Default expiry per mode (Phase 0 doc values)
  - Canonical message bytes (determinism + domain separation)
  - Sign + verify roundtrip with LocalTokenSigner
  - Signature recovery / address matching
  - Tampered token detection (modified scope, modified expires_at, etc.)
  - Persistence (atomic write, 0600 perms, roundtrip)
  - Lookup by ID, list, revoke, delete
  - Soft revocation preserves audit trail; delete removes file

No real WC. No real chain. Local key signing only for tests.
"""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from rift_core.keys import (
    TOKEN_SIGNING_HEADER,
    AuthorizationToken,
    TokenScope,
    TradeAction,
    TradeSide,
)
from rift_trade.auth import (
    DEFAULT_PER_TRADE_DURATION,
    DEFAULT_SESSION_DURATION,
    AuthError,
    IssuanceMode,
    LocalTokenSigner,
    canonical_token_bytes,
    delete_token,
    issue_token,
    list_tokens,
    load_token,
    revoke_token,
    save_token,
    verify_token_signature,
)


# ─── Shared fixtures ──────────────────────────────────────────────────

LOCAL_KEY = "0x" + "5" * 64  # Deterministic dev key


@pytest.fixture
def signer():
    return LocalTokenSigner(LOCAL_KEY)


@pytest.fixture
def scope():
    return TokenScope(
        coins=["ETH", "SUI"],
        sides=[TradeSide.BUY, TradeSide.SELL],
        actions=[TradeAction.OPEN, TradeAction.CLOSE],
        max_notional=Decimal("500"),
        max_daily=Decimal("2000"),
    )


@pytest.fixture
def tmp_tokens_dir(tmp_path):
    return tmp_path / "tokens"


# ─── LocalTokenSigner sanity ──────────────────────────────────────────

class TestLocalTokenSigner:
    def test_address_derived_from_key(self):
        from eth_account import Account
        signer = LocalTokenSigner(LOCAL_KEY)
        expected = Account.from_key(LOCAL_KEY).address.lower()
        assert signer.address == expected

    def test_handles_key_without_0x_prefix(self):
        from eth_account import Account
        signer = LocalTokenSigner("5" * 64)
        expected = Account.from_key("0x" + "5" * 64).address.lower()
        assert signer.address == expected

    def test_rejects_short_key(self):
        with pytest.raises(ValueError):
            LocalTokenSigner("0xabc")

    def test_sign_returns_130_hex_chars(self, signer):
        msg = b"test message"
        sig = signer.sign_token_bytes(msg)
        # signature.hex() doesn't include 0x prefix
        assert len(sig) == 130
        assert all(c in "0123456789abcdef" for c in sig)


# ─── canonical_token_bytes ────────────────────────────────────────────

class TestCanonicalTokenBytes:
    def _kwargs(self, scope):
        return dict(
            token_id=uuid4(),
            issuer="0x" + "a" * 40,
            issued_at_ms=1715890234567,
            expires_at_ms=1715900000000,
            scope=scope,
        )

    def test_starts_with_signing_header(self, scope):
        b = canonical_token_bytes(**self._kwargs(scope))
        assert b.startswith(TOKEN_SIGNING_HEADER)

    def test_deterministic(self, scope):
        """Same inputs → same bytes. Critical for verify to recover the right signer."""
        kw = self._kwargs(scope)
        b1 = canonical_token_bytes(**kw)
        b2 = canonical_token_bytes(**kw)
        assert b1 == b2

    def test_different_scope_different_bytes(self):
        s1 = TokenScope(coins=["ETH"], max_notional=Decimal("100"), max_daily=Decimal("500"))
        s2 = TokenScope(coins=["BTC"], max_notional=Decimal("100"), max_daily=Decimal("500"))
        kw1 = self._kwargs(s1)
        kw2 = dict(kw1, scope=s2)
        assert canonical_token_bytes(**kw1) != canonical_token_bytes(**kw2)

    def test_long_lived_serializes_none_expiry(self, scope):
        kw = self._kwargs(scope)
        kw["expires_at_ms"] = None
        b = canonical_token_bytes(**kw)
        # Strip header, parse JSON
        body = b[len(TOKEN_SIGNING_HEADER):].decode("utf-8")
        payload = json.loads(body)
        assert payload["expires_at_ms"] is None

    def test_issuer_lowercased(self, scope):
        kw = self._kwargs(scope)
        kw["issuer"] = "0xABCDEF" + "1" * 34
        b = canonical_token_bytes(**kw)
        body = b[len(TOKEN_SIGNING_HEADER):].decode("utf-8")
        payload = json.loads(body)
        assert payload["issuer"] == "0xabcdef" + "1" * 34

    def test_coins_sorted_for_determinism(self, ):
        """Token scope normalizes coins on construction; canonical_token_bytes
        must produce identical output regardless of original input order."""
        s1 = TokenScope(coins=["SUI", "ETH"], max_notional=Decimal("100"), max_daily=Decimal("500"))
        s2 = TokenScope(coins=["ETH", "SUI"], max_notional=Decimal("100"), max_daily=Decimal("500"))
        kw1 = dict(token_id=uuid4(), issuer="0x" + "a" * 40,
                    issued_at_ms=1, expires_at_ms=2, scope=s1)
        kw2 = dict(kw1, scope=s2)
        assert canonical_token_bytes(**kw1) == canonical_token_bytes(**kw2)


# ─── Issuance ─────────────────────────────────────────────────────────

class TestIssueToken:
    def test_session_default_expiry(self, signer, scope):
        before = datetime.now(timezone.utc)
        token = issue_token(scope=scope, signer=signer,
                            issuance_mode=IssuanceMode.SESSION, save=False)
        # ~4 hours from now
        expected = before + DEFAULT_SESSION_DURATION
        assert abs((token.expires_at - expected).total_seconds()) < 5

    def test_per_trade_default_expiry(self, signer, scope):
        before = datetime.now(timezone.utc)
        token = issue_token(scope=scope, signer=signer,
                            issuance_mode=IssuanceMode.PER_TRADE, save=False)
        expected = before + DEFAULT_PER_TRADE_DURATION
        assert abs((token.expires_at - expected).total_seconds()) < 5

    def test_long_lived_no_expiry(self, signer, scope):
        token = issue_token(scope=scope, signer=signer,
                            issuance_mode=IssuanceMode.LONG_LIVED, save=False)
        assert token.expires_at is None

    def test_custom_expiry_override(self, signer, scope):
        custom = datetime.now(timezone.utc) + timedelta(hours=12)
        token = issue_token(scope=scope, signer=signer,
                            issuance_mode=IssuanceMode.SESSION,
                            expires_at=custom, save=False)
        assert token.expires_at == custom

    def test_issuer_matches_signer_address(self, signer, scope):
        token = issue_token(scope=scope, signer=signer, save=False)
        assert token.issuer == signer.address

    def test_signature_is_130_hex(self, signer, scope):
        token = issue_token(scope=scope, signer=signer, save=False)
        assert len(token.signature) == 130

    def test_unique_ids(self, signer, scope):
        t1 = issue_token(scope=scope, signer=signer, save=False)
        t2 = issue_token(scope=scope, signer=signer, save=False)
        assert t1.id != t2.id

    def test_issued_at_uses_injected_now(self, signer, scope):
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
        token = issue_token(scope=scope, signer=signer,
                            _now=now, save=False)
        assert token.issued_at == now

    def test_signer_failure_wrapped_as_auth_error(self, scope):
        class BrokenSigner:
            address = "0x" + "a" * 40
            def sign_token_bytes(self, msg):
                raise RuntimeError("HSM unplugged")
        with pytest.raises(AuthError, match="HSM unplugged"):
            issue_token(scope=scope, signer=BrokenSigner(), save=False)

    def test_save_writes_to_disk(self, signer, scope, tmp_tokens_dir):
        token = issue_token(scope=scope, signer=signer,
                            tokens_dir=tmp_tokens_dir, save=True)
        path = tmp_tokens_dir / f"{token.id}.json"
        assert path.exists()

    def test_save_false_does_not_write(self, signer, scope, tmp_tokens_dir):
        token = issue_token(scope=scope, signer=signer,
                            tokens_dir=tmp_tokens_dir, save=False)
        path = tmp_tokens_dir / f"{token.id}.json"
        assert not path.exists()


# ─── Verification ─────────────────────────────────────────────────────

class TestVerifyTokenSignature:
    def test_freshly_signed_token_verifies(self, signer, scope):
        token = issue_token(scope=scope, signer=signer, save=False)
        assert verify_token_signature(token) is True

    def test_tampered_scope_fails(self, signer, scope):
        token = issue_token(scope=scope, signer=signer, save=False)
        # Mutate the scope (Pydantic frozen, so model_copy)
        bad_scope = TokenScope(
            coins=["BTC"],  # attacker changes allowed coin
            max_notional=scope.max_notional,
            max_daily=scope.max_daily,
        )
        tampered = token.model_copy(update={"scope": bad_scope})
        assert verify_token_signature(tampered) is False

    def test_tampered_expires_at_fails(self, signer, scope):
        token = issue_token(scope=scope, signer=signer, save=False)
        tampered = token.model_copy(update={"expires_at":
            datetime.now(timezone.utc) + timedelta(days=365)})
        assert verify_token_signature(tampered) is False

    def test_tampered_issuer_fails(self, signer, scope):
        token = issue_token(scope=scope, signer=signer, save=False)
        tampered = token.model_copy(update={"issuer": "0x" + "f" * 40})
        assert verify_token_signature(tampered) is False

    def test_completely_bogus_signature_fails(self, signer, scope):
        token = issue_token(scope=scope, signer=signer, save=False)
        bad = token.model_copy(update={"signature": "0" * 130})
        # Either fails to recover, or recovers wrong address
        assert verify_token_signature(bad) is False

    def test_different_signers_dont_cross_verify(self, scope):
        signer_a = LocalTokenSigner("a" * 64)
        signer_b = LocalTokenSigner("b" * 64)
        token_a = issue_token(scope=scope, signer=signer_a, save=False)
        # Forge a token claiming signer_b is issuer but using signer_a's signature
        forged = token_a.model_copy(update={"issuer": signer_b.address})
        assert verify_token_signature(forged) is False


# ─── Persistence ──────────────────────────────────────────────────────

class TestPersistence:
    def test_save_and_load_roundtrip(self, signer, scope, tmp_tokens_dir):
        token = issue_token(scope=scope, signer=signer,
                            tokens_dir=tmp_tokens_dir, save=True)
        loaded = load_token(token.id, tokens_dir=tmp_tokens_dir)
        assert loaded == token

    def test_loaded_token_still_verifies(self, signer, scope, tmp_tokens_dir):
        """Roundtrip through disk must preserve signature validity."""
        token = issue_token(scope=scope, signer=signer,
                            tokens_dir=tmp_tokens_dir, save=True)
        loaded = load_token(token.id, tokens_dir=tmp_tokens_dir)
        assert verify_token_signature(loaded) is True

    def test_file_permissions_600(self, signer, scope, tmp_tokens_dir):
        token = issue_token(scope=scope, signer=signer,
                            tokens_dir=tmp_tokens_dir, save=True)
        path = tmp_tokens_dir / f"{token.id}.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_atomic_write_no_tmp_left(self, signer, scope, tmp_tokens_dir):
        token = issue_token(scope=scope, signer=signer,
                            tokens_dir=tmp_tokens_dir, save=True)
        path = tmp_tokens_dir / f"{token.id}.json"
        tmp = path.with_suffix(path.suffix + ".tmp")
        assert not tmp.exists()

    def test_load_nonexistent_returns_none(self, tmp_tokens_dir):
        assert load_token(uuid4(), tokens_dir=tmp_tokens_dir) is None

    def test_load_corrupt_returns_none(self, tmp_tokens_dir):
        tmp_tokens_dir.mkdir(parents=True, exist_ok=True)
        fake_id = uuid4()
        (tmp_tokens_dir / f"{fake_id}.json").write_text("not json")
        assert load_token(fake_id, tokens_dir=tmp_tokens_dir) is None


# ─── list_tokens ──────────────────────────────────────────────────────

class TestListTokens:
    def test_lists_all_saved(self, signer, scope, tmp_tokens_dir):
        ids = set()
        for _ in range(3):
            t = issue_token(scope=scope, signer=signer,
                            tokens_dir=tmp_tokens_dir, save=True)
            ids.add(t.id)
        listed = list_tokens(tokens_dir=tmp_tokens_dir)
        assert {t.id for t in listed} == ids

    def test_newest_first(self, signer, scope, tmp_tokens_dir):
        first = issue_token(scope=scope, signer=signer,
                            tokens_dir=tmp_tokens_dir, save=True)
        # Ensure mtime difference is detectable
        import time; time.sleep(0.01)
        second = issue_token(scope=scope, signer=signer,
                              tokens_dir=tmp_tokens_dir, save=True)
        listed = list_tokens(tokens_dir=tmp_tokens_dir)
        assert listed[0].id == second.id

    def test_skips_corrupt_files(self, signer, scope, tmp_tokens_dir):
        good = issue_token(scope=scope, signer=signer,
                            tokens_dir=tmp_tokens_dir, save=True)
        (tmp_tokens_dir / "garbage.json").write_text("xxx")
        listed = list_tokens(tokens_dir=tmp_tokens_dir)
        # Good token still appears; garbage silently skipped
        assert any(t.id == good.id for t in listed)


# ─── Revocation ───────────────────────────────────────────────────────

class TestRevokeToken:
    def test_marks_token_revoked(self, signer, scope, tmp_tokens_dir):
        token = issue_token(scope=scope, signer=signer,
                            tokens_dir=tmp_tokens_dir, save=True)
        assert token.revoked is False
        result = revoke_token(token.id, tokens_dir=tmp_tokens_dir)
        assert result.revoked is True

    def test_revoked_token_persisted(self, signer, scope, tmp_tokens_dir):
        token = issue_token(scope=scope, signer=signer,
                            tokens_dir=tmp_tokens_dir, save=True)
        revoke_token(token.id, tokens_dir=tmp_tokens_dir)
        reloaded = load_token(token.id, tokens_dir=tmp_tokens_dir)
        assert reloaded.revoked is True

    def test_revoke_missing_returns_none(self, tmp_tokens_dir):
        assert revoke_token(uuid4(), tokens_dir=tmp_tokens_dir) is None

    def test_revoke_already_revoked_idempotent(self, signer, scope, tmp_tokens_dir):
        token = issue_token(scope=scope, signer=signer,
                            tokens_dir=tmp_tokens_dir, save=True)
        first = revoke_token(token.id, tokens_dir=tmp_tokens_dir)
        second = revoke_token(token.id, tokens_dir=tmp_tokens_dir)
        assert first.revoked is True
        assert second.revoked is True

    def test_revoked_token_fails_is_valid(self, signer, scope, tmp_tokens_dir):
        token = issue_token(scope=scope, signer=signer,
                            tokens_dir=tmp_tokens_dir, save=True)
        assert token.is_valid() is True
        revoked = revoke_token(token.id, tokens_dir=tmp_tokens_dir)
        assert revoked.is_valid() is False


# ─── delete_token ─────────────────────────────────────────────────────

class TestDeleteToken:
    def test_delete_returns_true_when_existed(self, signer, scope, tmp_tokens_dir):
        token = issue_token(scope=scope, signer=signer,
                            tokens_dir=tmp_tokens_dir, save=True)
        assert delete_token(token.id, tokens_dir=tmp_tokens_dir) is True
        assert load_token(token.id, tokens_dir=tmp_tokens_dir) is None

    def test_delete_returns_false_when_missing(self, tmp_tokens_dir):
        assert delete_token(uuid4(), tokens_dir=tmp_tokens_dir) is False
