"""Provenance — sealed bundles for backtest and live audit trails.

A `SealedBundle` is a manifest that captures everything needed to identify
what produced a result: code commit, data hash, config hash, RNG seed,
substrate version, result hash. The bundle_id is a sha256 of the canonical
representation — same inputs → same bundle_id.

Use cases:

  Backtest:    every run produces a bundle. Re-running with the same code +
               data + config + seed produces the same bundle_id. Mismatch
               means something changed; the diff between bundles tells you what.

  Live trade:  every `DecisionRecord` (T2 propose, T3 execute) carries the
               bundle_id of the strategy version that produced the decision.
               Auditing a fill traces back through the bundle to the exact
               code commit and data window.

The manifest layer (this module) is sufficient for AUDITABILITY and
TRACEABILITY. Offline reproducibility (handing someone a tarball with
data + code so they can re-run from scratch) would be a separate artifact
layer — useful for sharing strategies but not required for the audit chain.

References:
  RIFT vision principle 11: "Full quant-shop reproducibility everywhere —
    seeded RNGs, data hashes, code commit pins, signed orders, full
    provenance chain on every backtest AND live run."
"""

from rift_substrate.provenance.bundle import (
    SealedBundle,
    canonical_json,
    get_git_state,
    hash_canonical_json,
    hash_data_files,
    hash_text,
)
from rift_substrate.provenance.store import (
    DEFAULT_BUNDLES_DIR,
    list_bundles,
    load_bundle,
    save_bundle,
    verify_bundle,
)

__all__ = [
    "DEFAULT_BUNDLES_DIR",
    "SealedBundle",
    "canonical_json",
    "get_git_state",
    "hash_canonical_json",
    "hash_data_files",
    "hash_text",
    "list_bundles",
    "load_bundle",
    "save_bundle",
    "verify_bundle",
]
