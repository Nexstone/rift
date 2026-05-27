"""SealedBundle — manifest of everything that produced a result.

The bundle is a stable, hashable record of:
  - what code ran (git commit + dirty flag + substrate version)
  - what data went in (file hashes)
  - what config drove it (config hash)
  - what randomness was used (RNG seed)
  - what came out (result hash)
  - when it ran (timestamp)

`bundle_id` is the sha256 of the canonical JSON of all the above (except
bundle_id itself, which would be self-referential). Two bundles with the
same inputs always have the same bundle_id; mismatch proves something
changed.

Canonical JSON: sort_keys=True, separators=(',', ':'). No whitespace, no
floating-point fuzz. Same content → byte-identical → same hash.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# ─── Hashing helpers ───────────────────────────────────────────────────


def canonical_json(obj: dict[str, Any]) -> str:
    """Stable JSON: sorted keys, no whitespace. Same inputs → same bytes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def hash_text(text: str) -> str:
    """sha256 hex of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_canonical_json(obj: dict[str, Any]) -> str:
    """sha256 hex of the canonical JSON of obj."""
    return hash_text(canonical_json(obj))


def hash_data_files(paths: Iterable[Path | str], chunk_size: int = 1024 * 1024) -> str:
    """Stable hash over the contents of one or more files.

    Order-independent: paths are sorted by string representation before hashing.
    Each file's hash is concatenated and re-hashed.

    Missing files raise FileNotFoundError.
    """
    sorted_paths = sorted(Path(p) for p in paths)
    if not sorted_paths:
        return hashlib.sha256(b"empty").hexdigest()

    outer = hashlib.sha256()
    for p in sorted_paths:
        if not p.exists():
            raise FileNotFoundError(f"hash_data_files: missing file {p}")
        inner = hashlib.sha256()
        with open(p, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                inner.update(chunk)
        # Include the path's name in the outer hash so renames register
        outer.update(p.name.encode("utf-8"))
        outer.update(b":")
        outer.update(inner.hexdigest().encode("utf-8"))
        outer.update(b"\n")
    return outer.hexdigest()


# ─── Git state ─────────────────────────────────────────────────────────


def get_git_state(repo_dir: Path | str | None = None) -> tuple[str, bool]:
    """Return (commit_sha, dirty).

    `dirty` is True if the working tree has uncommitted changes OR if we
    couldn't determine the state (defensive — don't claim cleanliness we
    can't verify). When run outside a git repo, returns ("unknown", True).
    """
    cwd = str(repo_dir) if repo_dir else None
    try:
        sha_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd, capture_output=True, text=True, check=False, timeout=5,
        )
        if sha_proc.returncode != 0:
            return ("unknown", True)
        sha = sha_proc.stdout.strip()

        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, check=False, timeout=5,
        )
        if status_proc.returncode != 0:
            return (sha, True)
        dirty = bool(status_proc.stdout.strip())
        return (sha, dirty)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ("unknown", True)


# ─── SealedBundle ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class SealedBundle:
    """Reproducibility manifest. `bundle_id` is derived from all other fields.

    Construct via `SealedBundle.from_inputs(...)` — direct construction is
    allowed but the caller must supply a consistent bundle_id (or use the
    classmethod which computes it correctly).

    Attributes:
      bundle_id:         sha256 of canonical JSON of all other fields
      bundle_type:       "backtest" / "walkforward" / "monte_carlo" /
                         "sweep" / "live_propose" / "live_execute" / "custom"
      created_at_iso:    ISO 8601 UTC timestamp

      code_commit_sha:   git HEAD at run time
      code_dirty:        was the working tree dirty
      substrate_version: rift_substrate version at run time

      data_hash:         hash of input data files (use hash_data_files)
      config_hash:       hash of strategy/run config (use hash_canonical_json)
      code_hash:         hash of strategy source file (sha256 of text). May be ""
                         when no specific strategy file applies.
      rng_seed:          seed used for any bootstrap/MC/walkforward. None for
                         deterministic-only runs.

      result_hash:       hash of canonical result representation
      metadata:          free-form extras for the bundle (strategy name, pair,
                         interval, etc.). Hashed into bundle_id.
    """

    bundle_id: str
    bundle_type: str
    created_at_iso: str

    code_commit_sha: str
    code_dirty: bool
    substrate_version: str

    data_hash: str
    config_hash: str
    code_hash: str
    rng_seed: int | None

    result_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)

    # ─── Construction ─────────────────────────────────────────────

    @classmethod
    def from_inputs(
        cls,
        *,
        bundle_type: str,
        data_hash: str,
        config_hash: str,
        result_hash: str,
        code_hash: str = "",
        rng_seed: int | None = None,
        substrate_version: str = "",
        metadata: dict[str, Any] | None = None,
        git_repo_dir: Path | str | None = None,
        created_at_iso: str | None = None,
    ) -> "SealedBundle":
        """Build a bundle by computing its bundle_id from the supplied fields."""
        if substrate_version == "":
            substrate_version = _read_substrate_version()
        sha, dirty = get_git_state(git_repo_dir)
        created = created_at_iso or datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta = dict(metadata) if metadata else {}

        body = {
            "bundle_type": bundle_type,
            "created_at_iso": created,
            "code_commit_sha": sha,
            "code_dirty": dirty,
            "substrate_version": substrate_version,
            "data_hash": data_hash,
            "config_hash": config_hash,
            "code_hash": code_hash,
            "rng_seed": rng_seed,
            "result_hash": result_hash,
            "metadata": meta,
        }
        bundle_id = hash_canonical_json(body)

        return cls(
            bundle_id=bundle_id,
            bundle_type=bundle_type,
            created_at_iso=created,
            code_commit_sha=sha,
            code_dirty=dirty,
            substrate_version=substrate_version,
            data_hash=data_hash,
            config_hash=config_hash,
            code_hash=code_hash,
            rng_seed=rng_seed,
            result_hash=result_hash,
            metadata=meta,
        )

    # ─── Serialization ────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SealedBundle":
        return cls(
            bundle_id=d["bundle_id"],
            bundle_type=d["bundle_type"],
            created_at_iso=d["created_at_iso"],
            code_commit_sha=d["code_commit_sha"],
            code_dirty=bool(d["code_dirty"]),
            substrate_version=d["substrate_version"],
            data_hash=d["data_hash"],
            config_hash=d["config_hash"],
            code_hash=d.get("code_hash", ""),
            rng_seed=d.get("rng_seed"),
            result_hash=d["result_hash"],
            metadata=dict(d.get("metadata", {})),
        )

    def canonical_body(self) -> dict[str, Any]:
        """Body used to compute bundle_id (excludes bundle_id itself)."""
        b = self.to_dict()
        b.pop("bundle_id", None)
        return b

    def expected_bundle_id(self) -> str:
        """Recompute what bundle_id SHOULD be, given the current contents."""
        return hash_canonical_json(self.canonical_body())

    def is_self_consistent(self) -> bool:
        """True iff bundle_id equals the hash of its own canonical body."""
        return self.bundle_id == self.expected_bundle_id()

    def summary(self) -> str:
        dirty_marker = " (dirty)" if self.code_dirty else ""
        seed_str = f"seed={self.rng_seed}" if self.rng_seed is not None else "no-seed"
        meta_keys = ", ".join(sorted(self.metadata.keys())) if self.metadata else "—"
        return "\n".join([
            f"SealedBundle  {self.bundle_id[:16]}...  ({self.bundle_type})",
            "─" * 64,
            f"  Created:    {self.created_at_iso}",
            f"  Code:       {self.code_commit_sha[:12]}{dirty_marker}  substrate v{self.substrate_version}",
            f"  Data hash:  {self.data_hash[:32]}...",
            f"  Config:     {self.config_hash[:32]}...",
            f"  Code hash:  {self.code_hash[:32] if self.code_hash else '(none)'}",
            f"  Seed:       {seed_str}",
            f"  Result:     {self.result_hash[:32]}...",
            f"  Metadata:   {meta_keys}",
        ])


def _read_substrate_version() -> str:
    """Best-effort: read substrate version from package metadata."""
    try:
        from importlib.metadata import version
        return version("rift-substrate")
    except Exception:
        return "unknown"
