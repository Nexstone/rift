# RIFT release procedure

Single source of truth for cutting a release.

## Pre-release: verification

Work top to bottom; do not skip.

### Code

- [ ] `git status` is clean on the commit you intend to release
- [ ] All `MEMORY.md` / planning docs reflect current reality
- [ ] No `TODO` / `FIXME` markers left in the new code
- [ ] `pytest -m "not slow and not mainnet"` — 0 failures (~1100+ tests)
- [ ] `pytest engine/tests/integration/test_phase0_pipeline.py` — 12/12 pass
- [ ] `python3 scripts/seal_release.py --check` — `Seal OK`
- [ ] `pytest -k test_integrity_hash_matches_source` — passes (not skipped)
- [ ] `tsc --noEmit` from `packages/cli/` — 0 errors
- [ ] `pnpm build` from `packages/cli/` — clean
- [ ] `uv build` (at repo root) — single consolidated `rift_engine-*.whl` + sdist produced
- [ ] `unzip -l dist/rift_engine-*.whl | grep '__init__.py'` lists all 10 namespaces:
      `rift`, `rift_api`, `rift_core`, `rift_data`, `rift_engine`,
      `rift_portfolio`, `rift_research`, `rift_strategies_sdk`, `rift_substrate`, `rift_trade`

### Mainnet sign-off

- [ ] Worked through every item in `engine/tests/integration/MAINNET_CHECKLIST.md`
- [ ] `RIFT_MAINNET_MAIN_KEY` + `RIFT_ACCEPT_MAINNET_RISK=1` set
- [ ] `pytest engine/tests/integration/test_phase0_mainnet.py -m mainnet` — passes
- [ ] Manual verification per the checklist:
  - HL UI shows position closed
  - HL UI shows test agent revoked
  - Builder fee address received its inflow within 10 min

### Repo cleanup

- [ ] No secrets in committed files (grep for `0x[0-9a-f]{64}`, `sk_`, `HYPERLIQUID_PRIVATE_KEY=` etc.)
- [ ] No internal references leaking ("Million Dollar Office", internal client names, etc.)
- [ ] `.gitignore` covers `.rift/`, `.env`, `*.tmp`
- [ ] Repo top-level: `LICENSE` exists, `README.md` present and accurate

### Version bumps

- [ ] `pyproject.toml` (root) → `version = "0.1.0"` — the single source of truth
- [ ] `packages/cli/package.json` → `"version": "0.1.0"`
- [ ] `engine/src/rift/__init__.py` → `__version__ = "0.1.0"`
- [ ] All version strings agree

Note: the per-package `packages/*/pyproject.toml` files exist for dev-only
editable installs and are not shipped. Their versions can drift without
affecting the release wheel.

## Release: cutting it

### Git

```bash
# Tag the release commit
git tag -a v$VERSION -m "v$VERSION"

# Push the tag (and the branch if not already pushed)
git push origin <branch> --tags
```

### PyPI (Python package)

One consolidated wheel — all 10 internal namespace packages
(`rift_core`, `rift_data`, `rift_engine`, `rift_trade`, `rift_portfolio`,
`rift_research`, `rift_api`, `rift_strategies_sdk`, `rift_substrate`, `rift`)
are bundled into a single `rift-engine` distribution. This avoids PyPI's
new-project rate limit that blocked the original multi-package design.

```bash
uv build                          # produces dist/rift_engine-$VERSION-*.whl + .tar.gz
UV_PUBLISH_TOKEN=<token> uv publish dist/*
```

Verify at https://pypi.org/project/rift-engine/$VERSION/

The release workflow does this automatically on tag push; manual steps
above are only for emergency hotfixes outside the workflow.

### npm (TypeScript CLI)

```bash
cd packages/cli
pnpm build
npm publish --access public  # if scoped @rift/cli
```

Verify at https://www.npmjs.com/package/@rift/cli

### GitHub release

```bash
gh release create v$VERSION \
  --title "v$VERSION" \
  --notes-file RELEASE_NOTES_v$VERSION.md
```

Attach the built artifacts if hosting wheels directly.

## Post-release: monitoring

For 48 hours after release:

- [ ] Monitor RIFT builder-fee address inflows on HL — should see real users' trades start to flow
- [ ] Check GitHub issues hourly the first day for breaking reports
- [ ] Watch npm/PyPI download counts as a basic adoption signal
- [ ] Be reachable on Discord/Twitter/whichever channels you publicize

## Rollback procedure

If a critical bug is discovered post-release:

1. Yank `rift-engine` $VERSION via the PyPI web UI:
   https://pypi.org/manage/project/rift-engine/release/$VERSION/ → "Options" → "Yank"
   (PyPI has no twine/uv yank command — must use the web UI)
2. Unpublish from npm (within 72h grace period): `npm unpublish @nexstone/rift-cli@$VERSION`
3. Cut a v0.1.1 hotfix with the fix
4. Tell users to upgrade prominently (README banner + GitHub release notes)
5. If the bug affected mainnet trades, investigate impact + reach out to affected users

## Post-release cleanup (one-time, v0.1.x only)

The original multi-package release attempt published 4 standalone packages
to PyPI before hitting the new-project rate limit:
`rift-api`, `rift-core`, `rift-data`, `rift-engine-core` — all at v0.1.0.
The consolidated v0.1.0 wheel does NOT depend on them; they are orphaned cruft.

Yank them via the PyPI web UI (login required, no CLI equivalent):
- https://pypi.org/manage/project/rift-api/release/0.1.0/
- https://pypi.org/manage/project/rift-core/release/0.1.0/
- https://pypi.org/manage/project/rift-data/release/0.1.0/
- https://pypi.org/manage/project/rift-engine-core/release/0.1.0/

For each: "Options" → "Yank" → reason "Superseded by consolidated rift-engine package".

## What ships in RIFT

For release-notes drafting:

- 1 Python distribution (`rift-engine`) bundling 10 internal namespace packages:
  `rift_core`, `rift_data`, `rift_engine`, `rift_trade`, `rift_portfolio`,
  `rift_research`, `rift_api`, `rift_strategies_sdk`, `rift_substrate`, `rift` (CLI)
- 1 TypeScript CLI package (`@nexstone/rift-cli`)
- Trust architecture:
  - Three-layer key model (main wallet via WC + locally-stored API wallet + auth tokens)
  - 4 capability tiers (T0/T1/T2/T3) with T3 gated by signed authorization tokens
  - 6-gate safety pipeline (kill switch, scope, daily cap, circuit breakers, margin, slippage)
  - Structured audit substrate (DecisionRecord + 8 typed schemas)
  - Fail-closed audit-write invariant on T3
- Hyperliquid-native API wallet integration via `Exchange.approve_agent`
- Account-mode aware throughout: HL's four modes (Standard, Unified Account, Portfolio
  Margin, DEX Abstraction) all read correctly by the same `read_collateral()` helper.
  Detected at `agent-pair` time and surfaced per-mode in `balance`, `auth setup`, and
  gate sizing. Includes new commands:
    - `rift account-mode-status <addr>`  show mode + collateral breakdown
    - `rift account-mode-set <mode> --local-main-key <key>`  switch modes with
      consolidation warnings and fail-loud on HL errors
  Mode-aware `rift trade transfer` skips cleanly under Unified/PM instead of forwarding
  HL's cryptic "Action disabled" error.
- One shipped OSS reference strategy (`trend_follow`) registered via SDK — a bidirectional
  EMA-crossover trend follower. Passes RIFT's full promotion pipeline (5/5 gates) on BTC 4h
  with default config: +25% over 2 years, Sharpe 0.71, max DD -6.88%, 91.6% Monte Carlo
  probability of profit. Coin-agnostic — works on any synced market.
- 1100+ unit + integration tests, all green
- Composition smoke tests for the trust-critical path (agent pair, token issue,
  T3 propose + execute, builder fee collection, mode switching). Mainnet-only by
  design — RIFT has no testnet code path; see `engine/tests/integration/test_phase0_mainnet.py`
- Builder fee at 0.03% perps / 1% spot accruing to `0x0916EAb573817F02b96665386c944e297A765d7C`
- Integrity-sealed: `_BUILDER_HASH` in `rift_core/_internal.py` pins the on-disk
  SHA256 of `builder_fee.py` — any tampering trips the regression test

## Known limitations

For honest expectations management:

- **WalletConnect bridge.** Current main-wallet signing uses `LocalKeySigner`
  (`--local-main-key` flag). Full WC bridge support is a planned addition.
- **MCP server.** Shipped via the CLI `rift serve` command (60+ tools wrapping the
  Python engine via subprocess; see `packages/cli/src/commands/serve.ts`). Tools cover
  research (backtest / walk-forward / Monte Carlo / sweep), workbench, live algo,
  portfolio supervision, TCA, audit, and spot/perps trading.
- **Only one OSS strategy.** By design, `trend_follow` is the only OSS reference.
  RIFT's value is the SDK + validation tooling for building your OWN edge, not a menu
  of pre-baked strategies. `rift new <name>` scaffolds a starter template.
- **Persistent status footer.** Currently only on `home`; full coverage planned.
- **Full Portfolio Margin collateral math.** RIFT counts USDC only for PM users.
  HL docs state that under both unified-account and portfolio-margin, the perp dex
  user state is "not meaningful" — real trading collateral is in spot, same as unified.
  PM specifically also allows borrowing against non-USDC collateral (HYPE/BTC/USDH at
  oracle × LTV), but HL doesn't expose the LTV ratios via any documented info endpoint.
  Published LTVs are pre-alpha and changeable. A PM user with significant non-USDC
  collateral will see less available margin in RIFT than HL actually grants. Planned
  fix: either a hardcoded LTV table (sync'd to HL's values) or read LTVs from an
  undocumented endpoint once one becomes available.
