"""RIFT engine CLI — entry point called by the TypeScript CLI via child_process.spawn().

The actual command bodies live in rift.commands.{domain}; this file just:
  1. Sets file-permissions umask + loads ~/.rift/.env
  2. Soft-imports rift_strategies_sdk to auto-register SDK example strategies
  3. Imports each command module for its @app.command(...) side effects
  4. Re-exposes `app` for `[project.scripts] rift-engine = "rift.cli:app"`

Every command name (e.g. `rift sync`, `rift backtest`) remains flat at the
top level — the split is for readability, not for nesting commands.
"""

from __future__ import annotations

import os

# Security: restrict file permissions for all new files (0o600) and dirs (0o700)
os.umask(0o077)

# Load ~/.rift/.env on startup — all secrets and config in one place
from rift.config import load_env
load_env()

# Auto-register SDK-shipped example strategies (trend_follow, etc.).
# Soft dependency — if rift_strategies_sdk isn't installed, skip silently.
try:
    import rift_strategies_sdk  # noqa: F401 — side effect: registers example strategies
except ImportError:
    pass

# The shared Typer app is created in _shared; importing the command modules
# below registers all 91 commands onto it.
from rift.commands._shared import app  # noqa: F401 — re-exported as the entry point

# Import each domain module for the @app.command side effects.
# Order doesn't matter; collisions would surface as Typer errors at startup.
from rift.commands import (  # noqa: F401
    admin,
    cost,
    data,
    explore,
    phase0_auth,
    portfolio,
    research,
    research_tools,
    trade,
    workbench,
)


if __name__ == "__main__":
    app()
