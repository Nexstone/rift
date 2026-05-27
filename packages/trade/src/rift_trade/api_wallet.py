"""Phase 0 step 4 — API wallet generation, registration, persistence, revocation.

The API wallet is Hyperliquid's native delegated-trading concept: a separate
keypair that the main wallet authorizes to trade on its behalf. Chain-level
constraint: API wallets can place trades but CANNOT withdraw funds. Compromise
of the local key file = bad trades only, never capital loss.

Architecture: this module is signer-agnostic. It exposes a `MainWalletSigner`
Protocol that defines the two operations needed (sign_approve_agent and
sign_revoke_agent). Concrete signers live elsewhere:

  rift_trade.signers.LocalKeySigner    — dev/test only: signs with a local key
  rift_trade.signers.WCSigner          — Phase 0 production: bridges to WalletConnect
                                          via the TS CLI; signs without ever
                                          exposing the main wallet key

This split lets us unit-test the api_wallet flow against a MockSigner without
needing WC running or a real main wallet.

All chain-touching functions accept a `base_url` override so tests can point
at a mock HTTP server. By default they auto-pick MAINNET/TESTNET from the
APIWalletKey's network field.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from eth_account import Account
from hyperliquid.api import API
from hyperliquid.utils import constants

from rift_core.keys import APIWalletKey, Network


# ─── Constants ────────────────────────────────────────────────────────

DEFAULT_CREDENTIALS_PATH = Path.home() / ".rift" / "credentials"

# HL-side "chain name" strings used in the action payload's hyperliquidChain field.
_CHAIN_NAMES = {
    Network.MAINNET: "Mainnet",
}

# HL convention (mirrors hyperliquid-python-sdk's sign_user_signed_action):
# signatureChainId is the chain ID the wallet used to sign and CAN BE ANY CHAIN —
# HL accepts whatever the wallet domain says. `hyperliquidChain` is the field
# that selects mainnet vs testnet. We use 0x66eee (HL's native L1 chain id)
# unconditionally so EIP-712 signatures we build match SDK-built ones and any
# wallet (WC included) can produce them regardless of which chain it's on.
_SIGNATURE_CHAIN_ID = "0x66eee"


# ─── Main wallet signer Protocol ──────────────────────────────────────

class MainWalletSigner(Protocol):
    """Signs HL L1 actions on behalf of the operator's main wallet.

    All implementations must expose `.address` (the main wallet's address)
    and the two signing methods used during API wallet lifecycle.

    The signature dict returned must match what HL's exchange endpoint
    expects: `{"r": "0x...", "s": "0x...", "v": <int>}`.
    """

    @property
    def address(self) -> str: ...

    def sign_approve_agent(self, action: dict, is_mainnet: bool) -> dict: ...

    def sign_revoke_agent(self, action: dict, is_mainnet: bool) -> dict: ...


# ─── Pure local-key signer (dev/testing only) ─────────────────────────

class LocalKeySigner:
    """Signs L1 actions with a local main wallet private key.

    NOT recommended for production operators — Phase 0's whole point is to
    avoid storing the main wallet key on disk. Use WCSigner for real
    operation; this class exists for development, integration testing,
    and the (rare) operator who explicitly opts in via `rift init --local-main-key`.
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

    def sign_approve_agent(self, action: dict, is_mainnet: bool) -> dict:
        from hyperliquid.utils.signing import sign_agent
        return sign_agent(self._account, action, is_mainnet)

    def sign_revoke_agent(self, action: dict, is_mainnet: bool) -> dict:
        # HL revokes by signing an approveAgent with agentAddress=0x0...0
        # (zero address). Same signing function works for both.
        from hyperliquid.utils.signing import sign_agent
        return sign_agent(self._account, action, is_mainnet)


# ─── Key generation (no chain access) ─────────────────────────────────

def generate_api_wallet(network: Network, name: str = "RIFT") -> APIWalletKey:
    """Generate a brand new API wallet keypair locally.

    Pure local operation — no network, no chain, no signing. The returned
    APIWalletKey has `registered_tx=None` until register_api_wallet is called.

    Args:
      network: which Hyperliquid network this wallet will be authorized for
      name:    human-readable name shown in main wallet's signing prompt.
               Defaults to "RIFT" for brand recognition; operators can pass
               their own via `rift init --agent-name <custom>`.

    Returns:
      An unregistered APIWalletKey with a fresh random keypair.
    """
    # 32 random bytes → 64-char hex private key. Mirrors what HL SDK's
    # Exchange.approve_agent does internally.
    private_key = "0x" + secrets.token_hex(32)
    account = Account.from_key(private_key)
    return APIWalletKey(
        address=account.address,
        private_key=private_key,
        network=network,
        name=name,
    )


# ─── Action builders ──────────────────────────────────────────────────

def build_approve_agent_action(api_wallet: APIWalletKey, nonce_ms: int | None = None) -> dict:
    """Build the HL L1 action payload that authorizes an API wallet.

    The returned dict is what gets EIP-712 signed by the main wallet via
    `signer.sign_approve_agent(...)`. Action format per HL docs:

      {
        "type": "approveAgent",
        "hyperliquidChain": "Mainnet" | "Testnet",
        "signatureChainId": "0xa4b1" (mainnet) | "0x66eee" (testnet),
        "agentAddress": <api wallet 0x...>,
        "agentName": <string, may be empty>,
        "nonce": <ms timestamp>,
      }

    Caller is responsible for ensuring the nonce is monotonically increasing
    on a given account. Default = current UTC ms.
    """
    if nonce_ms is None:
        nonce_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return {
        "type": "approveAgent",
        "hyperliquidChain": _CHAIN_NAMES[api_wallet.network],
        "signatureChainId": _SIGNATURE_CHAIN_ID,
        "agentAddress": api_wallet.address,
        "agentName": api_wallet.name,
        "nonce": nonce_ms,
    }


def build_revoke_agent_action(api_wallet: APIWalletKey, nonce_ms: int | None = None) -> dict:
    """Build the HL L1 action that revokes an API wallet.

    Revocation = re-running approveAgent with the same agentAddress but
    immediately invalidating it (HL convention). In practice we just submit
    a new approveAgent for the zero address to clear the agent slot, OR
    issue a wallet-level revoke. We use the latter for explicit intent.

    NOTE: The exact HL revocation API surface evolves. This function
    produces the action shape we currently target; if HL adds a dedicated
    revokeAgent action type later, update here in one place.
    """
    if nonce_ms is None:
        nonce_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return {
        "type": "approveAgent",
        "hyperliquidChain": _CHAIN_NAMES[api_wallet.network],
        "signatureChainId": _SIGNATURE_CHAIN_ID,
        "agentAddress": "0x" + "0" * 40,   # Zero address = revoke
        "agentName": api_wallet.name,
        "nonce": nonce_ms,
    }


# ─── Chain submission ─────────────────────────────────────────────────

def _base_url_for(network: Network, override: str | None = None) -> str:
    if override is not None:
        return override
    # Mainnet-only post-testnet rip. `network` kept in signature for callers
    # but only Network.MAINNET is a valid value.
    return constants.MAINNET_API_URL


def _post_l1_action(
    base_url: str,
    action: dict,
    signature: dict,
    nonce: int,
    timeout: float = 30.0,
) -> dict:
    """POST an L1 action to HL's exchange endpoint. Returns the raw JSON
    response. Caller is responsible for inspecting `response["status"]`."""
    api = API(base_url=base_url, timeout=timeout)
    payload = {
        "action": action,
        "nonce": nonce,
        "signature": signature,
        "vaultAddress": None,
        "expiresAfter": None,
    }
    return api.post("/exchange", payload)


def _extract_tx_hash(response: dict) -> str | None:
    """Pull the L1 tx hash out of an HL response. HL's response shape varies
    by action type; we try a couple of common locations."""
    # Common shape: {"status": "ok", "response": {"type": "default"}}
    # Or: {"status": "ok", "response": {"data": {"hash": "0x..."}}}
    # Or: just status without a hash for some L1 actions.
    if not isinstance(response, dict):
        return None
    resp = response.get("response", {})
    if isinstance(resp, dict):
        data = resp.get("data", {})
        if isinstance(data, dict):
            for key in ("hash", "txHash", "tx_hash"):
                if key in data:
                    return data[key]
    return None


# ─── Registration ─────────────────────────────────────────────────────

class RegistrationError(RuntimeError):
    """Raised when API wallet registration fails on-chain."""

    def __init__(self, message: str, response: dict | None = None):
        super().__init__(message)
        self.response = response


def register_api_wallet(
    api_wallet: APIWalletKey,
    signer: MainWalletSigner,
    *,
    base_url: str | None = None,
    nonce_ms: int | None = None,
) -> APIWalletKey:
    """Register an API wallet on Hyperliquid via the main wallet's signer.

    1. Build the approveAgent action
    2. Delegate signing to the signer (could be local key or WC bridge)
    3. POST to HL's exchange endpoint
    4. Verify success
    5. Return a new APIWalletKey with `registered_tx` populated

    Raises RegistrationError if HL returns a non-OK status.
    """
    is_mainnet = api_wallet.network == Network.MAINNET
    action = build_approve_agent_action(api_wallet, nonce_ms=nonce_ms)

    signature = signer.sign_approve_agent(action, is_mainnet)

    response = _post_l1_action(
        _base_url_for(api_wallet.network, base_url),
        action,
        signature,
        action["nonce"],
    )

    if not isinstance(response, dict) or response.get("status") != "ok":
        raise RegistrationError(
            f"Hyperliquid rejected approveAgent: {response}",
            response=response,
        )

    tx_hash = _extract_tx_hash(response)
    # Capture the main wallet's address from the signer so downstream TS
    # commands (trade / algo / portfolio-start) can read it from the file
    # without needing the main wallet's private key again.
    main_address = getattr(signer, "address", None)
    return api_wallet.model_copy(update={
        "registered_tx": tx_hash,
        "account_address": main_address,
    })


def revoke_api_wallet(
    api_wallet: APIWalletKey,
    signer: MainWalletSigner,
    *,
    base_url: str | None = None,
    nonce_ms: int | None = None,
) -> dict:
    """Revoke an API wallet's authorization on Hyperliquid.

    Used by `rift auth rotate-agent` and `rift auth revoke-agent`. After
    successful revocation the local key file becomes useless (HL will
    reject any orders signed by it).
    """
    is_mainnet = api_wallet.network == Network.MAINNET
    action = build_revoke_agent_action(api_wallet, nonce_ms=nonce_ms)
    signature = signer.sign_revoke_agent(action, is_mainnet)
    response = _post_l1_action(
        _base_url_for(api_wallet.network, base_url),
        action,
        signature,
        action["nonce"],
    )
    if not isinstance(response, dict) or response.get("status") != "ok":
        raise RegistrationError(
            f"Hyperliquid rejected agent revocation: {response}",
            response=response,
        )
    return response


# ─── Disk I/O ─────────────────────────────────────────────────────────

def _serialize_wallet(wallet: APIWalletKey) -> str:
    """Serialize an APIWalletKey to JSON. Pydantic's model_dump_json handles
    the datetime + UUID + Network fields correctly."""
    return wallet.model_dump_json()


def save_api_wallet(wallet: APIWalletKey, path: Path | None = None) -> Path:
    """Persist an API wallet to disk with restricted permissions (0600).

    The directory is created with 0700 perms if needed. Atomic write via
    tmp + rename so a crash mid-write cannot leave a corrupt credentials file.

    Returns the path written.
    """
    if path is None:
        path = DEFAULT_CREDENTIALS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_serialize_wallet(wallet))
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


def load_api_wallet(path: Path | None = None) -> APIWalletKey | None:
    """Read an API wallet from disk. Returns None if the file doesn't exist
    or fails to parse. Caller can distinguish via has_api_wallet() if needed."""
    if path is None:
        path = DEFAULT_CREDENTIALS_PATH
    if not path.exists():
        return None
    try:
        return APIWalletKey.model_validate_json(path.read_text())
    except Exception:
        return None


def has_api_wallet(path: Path | None = None) -> bool:
    """True iff a parseable API wallet file exists at `path`."""
    return load_api_wallet(path) is not None


def delete_api_wallet(path: Path | None = None) -> bool:
    """Delete the API wallet credentials file. Returns True if a file was
    removed, False if it didn't exist. Used by `rift init --reset`."""
    if path is None:
        path = DEFAULT_CREDENTIALS_PATH
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
