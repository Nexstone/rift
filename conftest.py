"""Shared fixtures for the rift test suite.

Fixtures here are visible to ALL tests across all packages (pytest's
testpaths conftest collection). Keep them small, deterministic, and free
of network calls. Tests that need network or large cached data must use
the `@pytest.mark.slow` marker.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Iterator

import pytest


# ─── Paths ────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent
ENGINE_DIR = REPO_ROOT / "engine"
PACKAGES_DIR = REPO_ROOT / "packages"
USER_STRATEGIES_DIR = REPO_ROOT / "strategies"

# Real cached data — only used by tests marked @pytest.mark.slow
RIFT_HOME = Path.home() / ".rift"
RAW_CACHE_DIR = RIFT_HOME / "raw"
DATA_DIR = RIFT_HOME / "data"

# Synthetic fixtures bundled in the repo (committed, ~70KB).
# Installed into ~/.rift/data/ for CI runs where no real cache exists.
FIXTURE_DATA_DIR = REPO_ROOT / "tests" / "fixtures" / "data"
_FIXTURE_INSTALL_MARKER = DATA_DIR / "_installed_by_test_fixtures"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Isolated data dir for tests that write parquet/json files."""
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─── Synthetic fixtures (offline, fast) ───────────────────────────────

@pytest.fixture
def sample_fill_tuples() -> list[tuple]:
    """Small synthetic fill set covering both taker and maker, both sides,
    both open/close, across two 5-minute candle buckets.

    Both buckets are exact 5-min boundary aligned (divisible by 300_000 ms)
    so tests can assert on per-bucket aggregates without worrying about
    where the bucket boundaries fall.

    Schema matches rift_core.schema.FILL_SCHEMA:
      (timestamp, price, size, side, dir, is_open, is_long, crossed,
       closed_pnl, fee, start_position)
    """
    # Both timestamps are 5-min boundary aligned (divisible by 300_000)
    BUCKET0 = 1_700_000_100_000  # = 5666667 * 300_000
    BUCKET1 = 1_700_000_400_000  # = 5666668 * 300_000  (next 5-min bucket)
    return [
        # Bucket 0: open long taker + maker counterpart at +1s
        (BUCKET0 +   1_000, 50000.0, 1.0,  "B", "Open Long",   True,  True,  True,   0.0,  1.7,  0.0),
        (BUCKET0 +   1_000, 50000.0, 1.0,  "A", "Open Short",  True,  False, False,  0.0, -0.85, 1.0),
        # Bucket 0: close short taker (buying back) at +2 minutes, higher price
        (BUCKET0 + 120_000, 50100.0, 0.5,  "B", "Close Short", False, False, True, -50.0,  0.85, 1.0),
        (BUCKET0 + 120_000, 50100.0, 0.5,  "A", "Close Long",  False, True,  False, 50.0, -0.43, 0.5),
        # Bucket 1: open short taker, lower price (+5s into next bucket)
        (BUCKET1 +   5_000, 49900.0, 2.0,  "A", "Open Short",  True,  False, True,   0.0,  1.7,  0.5),
        (BUCKET1 +   5_000, 49900.0, 2.0,  "B", "Open Long",   True,  True,  False,  0.0, -0.85, -2.0),
    ]


@pytest.fixture
def sample_fills_df(sample_fill_tuples):
    """sample_fill_tuples as a polars DataFrame with the canonical schema."""
    from rift_data.s3.parse import _fills_list_to_df
    return _fills_list_to_df(sample_fill_tuples)


# ─── Test-fixture bundle install ──────────────────────────────────────
#
# The integration smoke test in packages/research/tests/test_advanced.py
# calls `load_candles_smart("BTC", "4h")` at module-collection time. On
# CI (or any clean checkout) there's no `~/.rift/data/` yet, so the test
# auto-skips with "data not available". To make CI actually exercise the
# full pipeline, we ship a tiny synthetic candle+funding bundle under
# tests/fixtures/data/ and copy it into ~/.rift/data/ at session start —
# but ONLY when the destination is empty, so we never clobber a real
# cache on a developer machine.

def _install_test_fixtures() -> list[Path]:
    """Copy fixture parquets into ~/.rift/data/ if no real cache exists.

    Returns a list of paths that were installed (so they can be cleaned
    up at session end). Returns [] if the user already has real data, no
    fixtures are bundled, or the env var RIFT_SKIP_FIXTURE_INSTALL is set.
    """
    if os.environ.get("RIFT_SKIP_FIXTURE_INSTALL"):
        return []
    if not FIXTURE_DATA_DIR.exists():
        return []

    installed: list[Path] = []
    for src in FIXTURE_DATA_DIR.rglob("*.parquet"):
        rel = src.relative_to(FIXTURE_DATA_DIR)
        dst = DATA_DIR / rel
        if dst.exists():
            # Real data present — don't touch.
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        installed.append(dst)
    return installed


def pytest_configure(config):
    """Install synthetic fixtures into ~/.rift/data/ before collection."""
    installed = _install_test_fixtures()
    if installed:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _FIXTURE_INSTALL_MARKER.write_text(
            "\n".join(str(p) for p in installed),
            encoding="utf-8",
        )


def pytest_unconfigure(config):
    """Remove any fixture files we installed (leave user's real data alone)."""
    if not _FIXTURE_INSTALL_MARKER.exists():
        return
    for line in _FIXTURE_INSTALL_MARKER.read_text(encoding="utf-8").splitlines():
        p = Path(line.strip())
        if p.exists() and p.is_file():
            p.unlink()
    _FIXTURE_INSTALL_MARKER.unlink()


# ─── Markers and skip helpers ─────────────────────────────────────────

def pytest_collection_modifyitems(config, items):
    """Auto-skip slow/regression tests if the required real data is missing."""
    cached_aug13 = RAW_CACHE_DIR / "20250813"
    has_full_cache = cached_aug13.exists() and any(
        (cached_aug13 / f"{h:02d}.jsonl").stat().st_size > 0
        for h in range(24)
        if (cached_aug13 / f"{h:02d}.jsonl").exists()
    )
    skip_no_cache = pytest.mark.skip(reason="needs ~/.rift/raw/20250813 cache (run `rift sync` first)")
    for item in items:
        if "slow" in item.keywords and not has_full_cache:
            item.add_marker(skip_no_cache)
