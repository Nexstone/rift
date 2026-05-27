#!/usr/bin/env python3
"""Seal the builder_fee.py integrity hash into _internal.py before release.

The integrity check in `packages/trade/src/rift_trade/builder_fee.py`
compares SHA256(builder_fee.py)[:16] against `_BUILDER_HASH` in
`packages/core/src/rift_core/_internal.py`. When the hash is empty, the
check is bypassed (dev mode). When non-empty, any tampering with
builder_fee.py — even a whitespace change — flips the hash and makes
`get_builder_info()` return an invalid fee `{"f": 9999}` that HL rejects.

Run this once per release that touches builder_fee.py:

    python scripts/seal_release.py            # writes the new hash
    python scripts/seal_release.py --check    # verifies current seal is current

The regression test `test_integrity_hash_matches_source` enforces the
invariant in CI once the seal is non-empty.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BUILDER_FILE = ROOT / "packages/trade/src/rift_trade/builder_fee.py"
INTERNAL_FILE = ROOT / "packages/core/src/rift_core/_internal.py"
HASH_PATTERN = re.compile(r'_BUILDER_HASH\s*=\s*"([^"]*)"')


def compute_hash() -> str:
    src = BUILDER_FILE.read_bytes()
    return hashlib.sha256(src).hexdigest()[:16]


def read_current_seal() -> str:
    text = INTERNAL_FILE.read_text()
    m = HASH_PATTERN.search(text)
    if not m:
        raise SystemExit(f"could not find _BUILDER_HASH assignment in {INTERNAL_FILE}")
    return m.group(1)


def write_seal(new_hash: str) -> None:
    text = INTERNAL_FILE.read_text()
    new_text, n = HASH_PATTERN.subn(f'_BUILDER_HASH = "{new_hash}"', text, count=1)
    if n != 1:
        raise SystemExit(f"failed to update _BUILDER_HASH in {INTERNAL_FILE}")
    INTERNAL_FILE.write_text(new_text)


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    expected = compute_hash()
    current = read_current_seal()

    if check_only:
        if not current:
            print("Seal is empty (dev mode). Run `scripts/seal_release.py` before release.")
            return 1
        if current != expected:
            print(f"Seal MISMATCH: stored={current} actual={expected}")
            print("Re-seal with: python scripts/seal_release.py")
            return 1
        print(f"Seal OK: {current}")
        return 0

    if current == expected:
        print(f"Seal already current: {current}")
        return 0

    write_seal(expected)
    print(f"Sealed: _BUILDER_HASH = \"{expected}\" (was: \"{current}\")")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
