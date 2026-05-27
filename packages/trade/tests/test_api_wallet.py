"""Unit tests for rift_trade.api_wallet — Phase 0 step 4.

Tests cover:
  - Key generation (pure, deterministic with mocked secrets)
  - Action builders (verify HL-spec payload shape)
  - Registration flow with a mock signer + mocked HTTP POST
  - Disk persistence (atomic write + 0600 perms)
  - Revocation
  - Error handling (non-OK HL responses raise RegistrationError)

No real chain calls. No real WC. No real main wallet keys (except deterministic
test fixtures). Everything below runs offline.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rift_core.keys import APIWalletKey, Network
from rift_trade import api_wallet as aw
from rift_trade.api_wallet import (
    LocalKeySigner,
    MainWalletSigner,
    RegistrationError,
    build_approve_agent_action,
    build_revoke_agent_action,
    delete_api_wallet,
    generate_api_wallet,
    has_api_wallet,
    load_api_wallet,
    register_api_wallet,
    revoke_api_wallet,
    save_api_wallet,
)


# ─── Mock signer for testing ──────────────────────────────────────────

class MockSigner:
    """A MainWalletSigner that records calls without actually signing."""

    def __init__(self, address: str = "0x" + "a" * 40):
        self._addr = address.lower()
        self.approve_calls: list[tuple[dict, bool]] = []
        self.revoke_calls: list[tuple[dict, bool]] = []

    @property
    def address(self) -> str:
        return self._addr

    def sign_approve_agent(self, action: dict, is_mainnet: bool) -> dict:
        self.approve_calls.append((action, is_mainnet))
        return {"r": "0x" + "1" * 64, "s": "0x" + "2" * 64, "v": 27}

    def sign_revoke_agent(self, action: dict, is_mainnet: bool) -> dict:
        self.revoke_calls.append((action, is_mainnet))
        return {"r": "0x" + "3" * 64, "s": "0x" + "4" * 64, "v": 28}


# ─── Key generation ───────────────────────────────────────────────────

class TestGenerateApiWallet:
    def test_generates_valid_pydantic_model(self):
        w = generate_api_wallet(Network.MAINNET)
        assert isinstance(w, APIWalletKey)
        assert w.network == Network.MAINNET
        assert w.name == "RIFT"  # default branding
        assert w.registered_tx is None  # not yet registered

    def test_default_name_is_rift_uppercase(self):
        w = generate_api_wallet(Network.MAINNET)
        assert w.name == "RIFT"

    def test_custom_name_propagates(self):
        w = generate_api_wallet(Network.MAINNET, name="my-bot")
        assert w.name == "my-bot"

    def test_two_generations_produce_different_keys(self):
        """Cryptographic randomness — keys must collide with probability ~zero."""
        w1 = generate_api_wallet(Network.MAINNET)
        w2 = generate_api_wallet(Network.MAINNET)
        assert w1.private_key != w2.private_key
        assert w1.address != w2.address

    def test_private_key_is_64_hex_chars(self):
        w = generate_api_wallet(Network.MAINNET)
        # APIWalletKey strips 0x prefix internally
        assert len(w.private_key) == 64
        assert all(c in "0123456789abcdef" for c in w.private_key)

    def test_address_matches_derived_from_private_key(self):
        """The pubkey address should be deterministically derived from the private key."""
        from eth_account import Account
        w = generate_api_wallet(Network.MAINNET)
        derived = Account.from_key("0x" + w.private_key).address.lower()
        assert w.address == derived

    def test_deterministic_with_seeded_secrets(self):
        """Using a known seed → known key. Lets us pin behavior in tests."""
        with patch("rift_trade.api_wallet.secrets.token_hex",
                   return_value="0" * 63 + "1"):  # 1 in 2^256
            w = generate_api_wallet(Network.MAINNET)
        # The address derived from key "0x0...01" is well-known
        assert w.private_key == "0" * 63 + "1"


# ─── Action builders ──────────────────────────────────────────────────

class TestBuildApproveAgentAction:
    def test_mainnet_chain_name(self):
        w = generate_api_wallet(Network.MAINNET)
        action = build_approve_agent_action(w, nonce_ms=1000)
        assert action["hyperliquidChain"] == "Mainnet"
        # signatureChainId is the wallet's signing chain, not the HL env.
        # We use 0x66eee unconditionally to match the official SDK
        # (sign_user_signed_action hardcodes the same value).
        assert action["signatureChainId"] == "0x66eee"

    def test_mainnet_chain_name(self):
        w = generate_api_wallet(Network.MAINNET)
        action = build_approve_agent_action(w, nonce_ms=1000)
        assert action["hyperliquidChain"] == "Mainnet"
        assert action["signatureChainId"] == "0x66eee"

    def test_action_shape_matches_hl_spec(self):
        w = generate_api_wallet(Network.MAINNET, name="MyBot")
        action = build_approve_agent_action(w, nonce_ms=1715890234567)
        assert action["type"] == "approveAgent"
        assert action["agentAddress"] == w.address
        assert action["agentName"] == "MyBot"
        assert action["nonce"] == 1715890234567

    def test_nonce_defaults_to_current_ms(self):
        import time
        w = generate_api_wallet(Network.MAINNET)
        before = int(time.time() * 1000)
        action = build_approve_agent_action(w)
        after = int(time.time() * 1000)
        assert before - 1000 <= action["nonce"] <= after + 1000


class TestBuildRevokeAgentAction:
    def test_uses_zero_address_to_revoke(self):
        w = generate_api_wallet(Network.MAINNET)
        action = build_revoke_agent_action(w, nonce_ms=1000)
        assert action["type"] == "approveAgent"
        # Zero address is the revoke convention
        assert action["agentAddress"] == "0x" + "0" * 40

    def test_preserves_chain_metadata(self):
        w = generate_api_wallet(Network.MAINNET)
        action = build_revoke_agent_action(w, nonce_ms=1000)
        assert action["hyperliquidChain"] == "Mainnet"
        assert action["signatureChainId"] == "0x66eee"


# ─── Registration flow with mocked HTTP ───────────────────────────────

class TestRegisterApiWallet:
    def test_happy_path_returns_wallet_with_tx_hash(self):
        wallet = generate_api_wallet(Network.MAINNET)
        signer = MockSigner()
        mock_response = {"status": "ok", "response": {"type": "default",
                                                       "data": {"hash": "0x" + "f" * 64}}}
        with patch("rift_trade.api_wallet._post_l1_action", return_value=mock_response):
            updated = register_api_wallet(wallet, signer)

        # Wallet returned with registered_tx populated
        assert updated.registered_tx == "0x" + "f" * 64
        # Original wallet unchanged (Pydantic frozen)
        assert wallet.registered_tx is None
        # Signer was called once
        assert len(signer.approve_calls) == 1

    def test_signer_called_with_correct_action(self):
        wallet = generate_api_wallet(Network.MAINNET, name="my-bot")
        signer = MockSigner()
        mock_response = {"status": "ok", "response": {"data": {"hash": "0xabc"}}}
        with patch("rift_trade.api_wallet._post_l1_action", return_value=mock_response):
            register_api_wallet(wallet, signer, nonce_ms=12345)

        action, is_mainnet = signer.approve_calls[0]
        assert is_mainnet is True
        assert action["agentName"] == "my-bot"
        assert action["agentAddress"] == wallet.address
        assert action["nonce"] == 12345

    def test_hl_rejection_raises_registration_error(self):
        wallet = generate_api_wallet(Network.MAINNET)
        signer = MockSigner()
        rejection = {"status": "err", "response": "Invalid signature"}
        with patch("rift_trade.api_wallet._post_l1_action", return_value=rejection):
            with pytest.raises(RegistrationError) as exc:
                register_api_wallet(wallet, signer)
        assert exc.value.response == rejection

    def test_base_url_override_respected(self):
        wallet = generate_api_wallet(Network.MAINNET)
        signer = MockSigner()
        with patch("rift_trade.api_wallet._post_l1_action",
                   return_value={"status": "ok", "response": {"data": {"hash": "0xa"}}}) as mock_post:
            register_api_wallet(wallet, signer, base_url="https://custom.example/")
        # First positional arg to _post_l1_action is base_url
        assert mock_post.call_args.args[0] == "https://custom.example/"

    def test_missing_tx_hash_in_response_is_ok(self):
        """HL doesn't always return a hash; not having one isn't an error."""
        wallet = generate_api_wallet(Network.MAINNET)
        signer = MockSigner()
        with patch("rift_trade.api_wallet._post_l1_action",
                   return_value={"status": "ok"}):  # no response.data.hash
            updated = register_api_wallet(wallet, signer)
        assert updated.registered_tx is None


# ─── Revocation ───────────────────────────────────────────────────────

class TestRevokeApiWallet:
    def test_happy_path(self):
        wallet = generate_api_wallet(Network.MAINNET)
        signer = MockSigner()
        with patch("rift_trade.api_wallet._post_l1_action",
                   return_value={"status": "ok"}):
            response = revoke_api_wallet(wallet, signer)
        assert response == {"status": "ok"}
        assert len(signer.revoke_calls) == 1

    def test_rejection_raises_registration_error(self):
        wallet = generate_api_wallet(Network.MAINNET)
        signer = MockSigner()
        with patch("rift_trade.api_wallet._post_l1_action",
                   return_value={"status": "err"}):
            with pytest.raises(RegistrationError):
                revoke_api_wallet(wallet, signer)


# ─── Disk persistence ─────────────────────────────────────────────────

class TestSaveLoadApiWallet:
    def test_save_and_load_roundtrip(self, tmp_path):
        wallet = generate_api_wallet(Network.MAINNET, name="my-bot")
        path = tmp_path / "credentials"
        save_api_wallet(wallet, path)
        loaded = load_api_wallet(path)
        assert loaded == wallet

    def test_file_permissions_600(self, tmp_path):
        wallet = generate_api_wallet(Network.MAINNET)
        path = tmp_path / "credentials"
        save_api_wallet(wallet, path)
        mode = stat.S_IMODE(path.stat().st_mode)
        # Should be 0600 — owner read+write, no group/world
        assert mode == 0o600, f"Expected 0600, got {oct(mode)}"

    def test_parent_dir_created_with_0700(self, tmp_path):
        wallet = generate_api_wallet(Network.MAINNET)
        path = tmp_path / "subdir" / "credentials"
        save_api_wallet(wallet, path)
        assert path.parent.exists()
        # 0700 (with possible variation due to umask; just verify owner has rwx)
        mode = stat.S_IMODE(path.parent.stat().st_mode)
        assert mode & 0o700 == 0o700

    def test_atomic_write_no_tmp_left_behind(self, tmp_path):
        wallet = generate_api_wallet(Network.MAINNET)
        path = tmp_path / "credentials"
        save_api_wallet(wallet, path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        assert not tmp.exists()

    def test_load_returns_none_when_missing(self, tmp_path):
        assert load_api_wallet(tmp_path / "nonexistent") is None

    def test_load_returns_none_when_corrupt(self, tmp_path):
        path = tmp_path / "credentials"
        path.write_text("not valid json")
        assert load_api_wallet(path) is None

    def test_has_api_wallet_true_when_present(self, tmp_path):
        wallet = generate_api_wallet(Network.MAINNET)
        path = tmp_path / "credentials"
        save_api_wallet(wallet, path)
        assert has_api_wallet(path) is True

    def test_has_api_wallet_false_when_missing(self, tmp_path):
        assert has_api_wallet(tmp_path / "nonexistent") is False

    def test_has_api_wallet_false_when_corrupt(self, tmp_path):
        path = tmp_path / "credentials"
        path.write_text("garbage")
        assert has_api_wallet(path) is False

    def test_overwrite_existing_file(self, tmp_path):
        path = tmp_path / "credentials"
        w1 = generate_api_wallet(Network.MAINNET)
        save_api_wallet(w1, path)
        w2 = generate_api_wallet(Network.MAINNET, name="other")
        save_api_wallet(w2, path)
        loaded = load_api_wallet(path)
        assert loaded == w2


class TestDeleteApiWallet:
    def test_delete_returns_true_when_existed(self, tmp_path):
        wallet = generate_api_wallet(Network.MAINNET)
        path = tmp_path / "credentials"
        save_api_wallet(wallet, path)
        assert delete_api_wallet(path) is True
        assert not path.exists()

    def test_delete_returns_false_when_missing(self, tmp_path):
        assert delete_api_wallet(tmp_path / "nonexistent") is False


# ─── LocalKeySigner ───────────────────────────────────────────────────

class TestLocalKeySigner:
    """LocalKeySigner is for dev/test only, but let's verify it works."""

    SAMPLE_KEY = "0x" + "5" * 64  # Deterministic test key

    def test_construction(self):
        signer = LocalKeySigner(self.SAMPLE_KEY)
        assert signer.address.startswith("0x")
        assert len(signer.address) == 42

    def test_address_consistent_with_eth_account(self):
        from eth_account import Account
        signer = LocalKeySigner(self.SAMPLE_KEY)
        expected = Account.from_key(self.SAMPLE_KEY).address.lower()
        assert signer.address == expected

    def test_rejects_short_key(self):
        with pytest.raises(ValueError, match="32 bytes"):
            LocalKeySigner("0xabc")

    def test_handles_key_without_0x_prefix(self):
        signer = LocalKeySigner("5" * 64)  # no 0x
        # Should still derive same address
        from eth_account import Account
        expected = Account.from_key("0x" + "5" * 64).address.lower()
        assert signer.address == expected

    def test_sign_approve_agent_returns_signature_dict(self):
        signer = LocalKeySigner(self.SAMPLE_KEY)
        wallet = generate_api_wallet(Network.MAINNET)
        action = build_approve_agent_action(wallet, nonce_ms=1000)
        sig = signer.sign_approve_agent(action, is_mainnet=False)
        # HL signature shape: {"r": "0x...", "s": "0x...", "v": int}
        assert isinstance(sig, dict)
        assert "r" in sig and "s" in sig and "v" in sig
