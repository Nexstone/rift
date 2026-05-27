"""Tests for substrate.provenance — SealedBundle determinism + audit chain.

Pins the invariants that make sealed bundles useful:
  1. Same inputs → same bundle_id (deterministic)
  2. Any input change → different bundle_id
  3. Hash is over canonical JSON (order-independent)
  4. File hashing is order-independent + content-sensitive
  5. Save/load round-trip preserves identity
  6. Tampered bundles fail verification
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rift_substrate.provenance import (
    SealedBundle,
    canonical_json,
    get_git_state,
    hash_canonical_json,
    hash_data_files,
    hash_text,
    list_bundles,
    load_bundle,
    save_bundle,
    verify_bundle,
)


# ─── Hashing primitives ───────────────────────────────────────────────


class TestHashing:
    def test_canonical_json_sorts_keys(self):
        a = canonical_json({"b": 1, "a": 2})
        b = canonical_json({"a": 2, "b": 1})
        assert a == b == '{"a":2,"b":1}'

    def test_hash_canonical_json_stable(self):
        h1 = hash_canonical_json({"a": 1, "b": 2})
        h2 = hash_canonical_json({"b": 2, "a": 1})
        assert h1 == h2

    def test_hash_canonical_json_changes_with_content(self):
        h1 = hash_canonical_json({"a": 1})
        h2 = hash_canonical_json({"a": 2})
        assert h1 != h2

    def test_hash_text_deterministic(self):
        assert hash_text("hello") == hash_text("hello")
        assert hash_text("hello") != hash_text("world")

    def test_hash_data_files_order_independent(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("alpha")
        f2.write_text("beta")
        h1 = hash_data_files([f1, f2])
        h2 = hash_data_files([f2, f1])
        assert h1 == h2

    def test_hash_data_files_content_sensitive(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("foo")
        h1 = hash_data_files([f])
        f.write_text("bar")
        h2 = hash_data_files([f])
        assert h1 != h2

    def test_hash_data_files_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="missing"):
            hash_data_files([tmp_path / "nonexistent.txt"])

    def test_hash_data_files_handles_empty(self):
        # No files → stable fallback hash, doesn't crash
        h = hash_data_files([])
        assert isinstance(h, str)
        assert len(h) == 64


# ─── Git state ────────────────────────────────────────────────────────


class TestGitState:
    def test_returns_tuple_of_sha_and_dirty(self):
        sha, dirty = get_git_state()
        assert isinstance(sha, str)
        assert isinstance(dirty, bool)

    def test_outside_git_repo_returns_unknown_dirty(self, tmp_path):
        sha, dirty = get_git_state(tmp_path)
        # tmp_path isn't a git repo → unknown + dirty (safe defaults)
        assert sha == "unknown" or len(sha) == 40  # either fallback or actual SHA
        assert dirty is True  # safe default when we can't verify


# ─── SealedBundle construction ────────────────────────────────────────


class TestSealedBundleConstruction:
    def test_from_inputs_produces_self_consistent_bundle(self):
        b = SealedBundle.from_inputs(
            bundle_type="backtest",
            data_hash="d" * 64,
            config_hash="c" * 64,
            result_hash="r" * 64,
            rng_seed=42,
        )
        assert b.is_self_consistent()

    def test_same_inputs_same_bundle_id(self):
        ts = "2026-01-01T00:00:00+00:00"
        kwargs = dict(
            bundle_type="backtest",
            data_hash="d" * 64,
            config_hash="c" * 64,
            result_hash="r" * 64,
            rng_seed=42,
            created_at_iso=ts,
        )
        b1 = SealedBundle.from_inputs(**kwargs)
        b2 = SealedBundle.from_inputs(**kwargs)
        assert b1.bundle_id == b2.bundle_id

    def test_different_seed_different_bundle_id(self):
        ts = "2026-01-01T00:00:00+00:00"
        b1 = SealedBundle.from_inputs(
            bundle_type="backtest", data_hash="d" * 64,
            config_hash="c" * 64, result_hash="r" * 64,
            rng_seed=42, created_at_iso=ts,
        )
        b2 = SealedBundle.from_inputs(
            bundle_type="backtest", data_hash="d" * 64,
            config_hash="c" * 64, result_hash="r" * 64,
            rng_seed=43, created_at_iso=ts,  # different seed
        )
        assert b1.bundle_id != b2.bundle_id

    def test_different_data_hash_different_bundle_id(self):
        ts = "2026-01-01T00:00:00+00:00"
        b1 = SealedBundle.from_inputs(
            bundle_type="backtest", data_hash="d1" * 32,
            config_hash="c" * 64, result_hash="r" * 64,
            rng_seed=42, created_at_iso=ts,
        )
        b2 = SealedBundle.from_inputs(
            bundle_type="backtest", data_hash="d2" * 32,
            config_hash="c" * 64, result_hash="r" * 64,
            rng_seed=42, created_at_iso=ts,
        )
        assert b1.bundle_id != b2.bundle_id

    def test_metadata_affects_bundle_id(self):
        ts = "2026-01-01T00:00:00+00:00"
        b1 = SealedBundle.from_inputs(
            bundle_type="backtest", data_hash="d" * 64,
            config_hash="c" * 64, result_hash="r" * 64,
            rng_seed=42, created_at_iso=ts,
            metadata={"strategy": "alpha"},
        )
        b2 = SealedBundle.from_inputs(
            bundle_type="backtest", data_hash="d" * 64,
            config_hash="c" * 64, result_hash="r" * 64,
            rng_seed=42, created_at_iso=ts,
            metadata={"strategy": "beta"},
        )
        assert b1.bundle_id != b2.bundle_id

    def test_summary_renders(self):
        b = SealedBundle.from_inputs(
            bundle_type="backtest", data_hash="d" * 64,
            config_hash="c" * 64, result_hash="r" * 64, rng_seed=42,
        )
        text = b.summary()
        assert "SealedBundle" in text
        assert "backtest" in text


# ─── Serialization ────────────────────────────────────────────────────


class TestSerialization:
    def test_to_dict_from_dict_round_trip(self):
        b = SealedBundle.from_inputs(
            bundle_type="backtest", data_hash="d" * 64,
            config_hash="c" * 64, result_hash="r" * 64, rng_seed=42,
            metadata={"foo": "bar"},
        )
        d = b.to_dict()
        b2 = SealedBundle.from_dict(d)
        assert b2.bundle_id == b.bundle_id
        assert b2.is_self_consistent()

    def test_canonical_body_excludes_bundle_id(self):
        b = SealedBundle.from_inputs(
            bundle_type="backtest", data_hash="d" * 64,
            config_hash="c" * 64, result_hash="r" * 64, rng_seed=42,
        )
        body = b.canonical_body()
        assert "bundle_id" not in body


# ─── Store ────────────────────────────────────────────────────────────


class TestStore:
    def test_save_load_round_trip(self, tmp_path):
        b = SealedBundle.from_inputs(
            bundle_type="backtest", data_hash="d" * 64,
            config_hash="c" * 64, result_hash="r" * 64, rng_seed=42,
        )
        path = save_bundle(b, bundles_dir=tmp_path)
        assert path.exists()
        loaded = load_bundle(b.bundle_id, bundles_dir=tmp_path)
        assert loaded.bundle_id == b.bundle_id
        assert loaded.is_self_consistent()

    def test_save_is_idempotent(self, tmp_path):
        b = SealedBundle.from_inputs(
            bundle_type="backtest", data_hash="d" * 64,
            config_hash="c" * 64, result_hash="r" * 64, rng_seed=42,
        )
        p1 = save_bundle(b, bundles_dir=tmp_path)
        p2 = save_bundle(b, bundles_dir=tmp_path)
        assert p1 == p2

    def test_save_collision_with_different_content_raises(self, tmp_path):
        b = SealedBundle.from_inputs(
            bundle_type="backtest", data_hash="d" * 64,
            config_hash="c" * 64, result_hash="r" * 64, rng_seed=42,
        )
        save_bundle(b, bundles_dir=tmp_path)
        # Manually corrupt the file
        path = tmp_path / f"{b.bundle_id}.json"
        path.write_text('{"corrupted": true}')
        with pytest.raises(ValueError, match="collision"):
            save_bundle(b, bundles_dir=tmp_path)

    def test_load_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="no bundle"):
            load_bundle("nonexistent_id", bundles_dir=tmp_path)

    def test_list_bundles_sorted(self, tmp_path):
        ids = []
        for seed in [42, 7, 99]:
            b = SealedBundle.from_inputs(
                bundle_type="backtest", data_hash="d" * 64,
                config_hash="c" * 64, result_hash="r" * 64,
                rng_seed=seed,
                created_at_iso=f"2026-01-{seed:02d}T00:00:00+00:00",
            )
            save_bundle(b, bundles_dir=tmp_path)
            ids.append(b.bundle_id)
        result = list_bundles(tmp_path)
        assert sorted(result) == result  # is sorted
        assert set(result) == set(ids)

    def test_list_bundles_empty_dir(self, tmp_path):
        # Empty dir returns []
        assert list_bundles(tmp_path) == []


# ─── Tamper detection ────────────────────────────────────────────────


class TestVerifyBundle:
    def test_clean_bundle_verifies(self):
        b = SealedBundle.from_inputs(
            bundle_type="backtest", data_hash="d" * 64,
            config_hash="c" * 64, result_hash="r" * 64, rng_seed=42,
        )
        assert verify_bundle(b) is True

    def test_tampered_bundle_fails_verification(self, tmp_path):
        """Modifying any field without updating bundle_id breaks verification."""
        b = SealedBundle.from_inputs(
            bundle_type="backtest", data_hash="d" * 64,
            config_hash="c" * 64, result_hash="r" * 64, rng_seed=42,
        )
        # Save it
        save_bundle(b, bundles_dir=tmp_path)
        # Manually edit the JSON on disk (changing data_hash, leaving bundle_id alone)
        path = tmp_path / f"{b.bundle_id}.json"
        d = json.loads(path.read_text())
        d["data_hash"] = "tampered" * 8
        path.write_text(json.dumps(d, indent=2))
        # Load and verify
        loaded = load_bundle(b.bundle_id, bundles_dir=tmp_path)
        assert verify_bundle(loaded) is False
