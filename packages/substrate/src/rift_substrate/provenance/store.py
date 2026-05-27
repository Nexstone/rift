"""Disk store for SealedBundles — save / load / list / verify.

Bundles are written as JSON files under `~/.rift/bundles/<bundle_id>.json`.
The file name IS the bundle_id, so listing and lookup are trivial. Bundles
are immutable — overwriting an existing file with the same id is a no-op
(content is identical by definition); a write with the same id but different
content is a bug and raises.
"""

from __future__ import annotations

import json
from pathlib import Path

from rift_substrate.provenance.bundle import SealedBundle


DEFAULT_BUNDLES_DIR = Path.home() / ".rift" / "bundles"


def save_bundle(
    bundle: SealedBundle,
    bundles_dir: Path | str | None = None,
) -> Path:
    """Save the bundle to `<bundles_dir>/<bundle_id>.json`.

    Returns the path written. If the file already exists with identical
    content (same bundle_id, same body), the write is skipped (idempotent).
    If the file exists with different content under the same id, raises
    `ValueError` (shouldn't happen — bundle_id is content-addressed).
    """
    d = Path(bundles_dir) if bundles_dir else DEFAULT_BUNDLES_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{bundle.bundle_id}.json"

    payload = json.dumps(bundle.to_dict(), sort_keys=True, indent=2, default=str)
    if path.exists():
        existing = path.read_text()
        if existing == payload:
            return path  # idempotent
        raise ValueError(
            f"bundle id collision at {path}: same id, different content "
            f"(this should be impossible if hashing is correct)"
        )

    path.write_text(payload)
    return path


def load_bundle(
    bundle_id: str,
    bundles_dir: Path | str | None = None,
) -> SealedBundle:
    """Load a bundle by id."""
    d = Path(bundles_dir) if bundles_dir else DEFAULT_BUNDLES_DIR
    path = d / f"{bundle_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no bundle named {bundle_id} in {d}")
    return SealedBundle.from_dict(json.loads(path.read_text()))


def list_bundles(bundles_dir: Path | str | None = None) -> list[str]:
    """List bundle ids in the store (sorted)."""
    d = Path(bundles_dir) if bundles_dir else DEFAULT_BUNDLES_DIR
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def verify_bundle(bundle: SealedBundle) -> bool:
    """Re-hash the bundle's contents and verify bundle_id matches.

    If verification fails, the bundle was tampered with (someone edited
    the JSON without updating the id). Use this when loading bundles from
    untrusted sources.
    """
    return bundle.is_self_consistent()
