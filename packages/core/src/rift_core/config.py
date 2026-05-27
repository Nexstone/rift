"""RIFT configuration management."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".rift"
# Ensure config directory exists with restricted permissions
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
try:
    CONFIG_DIR.chmod(0o700)
except Exception:
    pass
CONFIG_PATH = CONFIG_DIR / "config.json"
ENV_PATH = CONFIG_DIR / ".env"

# Keys that should NEVER be loaded into os.environ at startup.
# Read just-in-time via get_env_var() to prevent strategy code from accessing them.
_SENSITIVE_KEYS = {"HYPERLIQUID_PRIVATE_KEY"}


def load_env() -> None:
    """Load ~/.rift/.env into os.environ. Called once at startup.

    Standard .env format: KEY=VALUE, one per line. Lines starting with # are comments.
    Does NOT override existing environment variables (explicit env takes precedence).
    """
    if not ENV_PATH.exists():
        return
    try:
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")  # strip quotes
            if key and key not in os.environ:  # don't override explicit env vars
                # Never load private keys into environ at startup — read just-in-time via get_env_var()
                if key in _SENSITIVE_KEYS:
                    continue
                os.environ[key] = value
    except Exception:
        pass


def set_env_var(key: str, value: str) -> None:
    """Set a key in ~/.rift/.env. Creates file if it doesn't exist.

    Updates existing key or appends new one. Also sets in current process env.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Read existing
    lines = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text().splitlines()

    # Update or append
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.partition("=")[0].strip()
            if k == key:
                lines[i] = f"{key}={value}"
                found = True
                break
    if not found:
        lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(lines) + "\n")
    ENV_PATH.chmod(0o600)  # restrict permissions — contains secrets
    os.environ[key] = value


def get_env_var(key: str, default: str = "") -> str:
    """Get a config value — checks os.environ first, then ~/.rift/.env file directly.

    Sensitive keys (private keys) are read from the file each time,
    never cached in os.environ, to prevent strategy code from accessing them.
    """
    # Always check explicit env var first (e.g., set by parent process)
    val = os.environ.get(key, "")
    if val:
        return val
    # Read directly from .env file (handles sensitive keys that aren't in os.environ)
    if ENV_PATH.exists():
        try:
            for line in ENV_PATH.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip("'\"")
                if k == key:
                    return v
        except Exception:
            pass
    return default


def load_config() -> dict[str, Any]:
    """Load config from ~/.rift/config.json."""
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text())


def save_config(config: dict[str, Any]) -> None:
    """Save config to ~/.rift/config.json with restricted permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    CONFIG_PATH.chmod(0o600)


def get_proxy() -> str | None:
    """Get configured proxy URL, or None."""
    config = load_config()
    return config.get("network", {}).get("proxy")


def set_proxy(proxy_url: str) -> None:
    """Save proxy URL to config."""
    config = load_config()
    if "network" not in config:
        config["network"] = {}
    config["network"]["proxy"] = proxy_url
    save_config(config)


def clear_proxy() -> None:
    """Remove proxy from config."""
    config = load_config()
    if "network" in config and "proxy" in config["network"]:
        del config["network"]["proxy"]
        if not config["network"]:
            del config["network"]
        save_config(config)


def _b3() -> str:
    return "944e297A765d7C"


def parse_duration(s: str) -> int:
    """Parse duration string to seconds.

    Examples: '4h' → 14400, '1d' → 86400, '30m' → 1800, '60s' → 60, '300' → 300
    """
    if not s:
        return 0
    s = s.strip().lower()
    if s.endswith("d"):
        return int(s[:-1]) * 86400
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("s"):
        return int(s[:-1])
    return int(s)
