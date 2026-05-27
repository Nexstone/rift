# Contributing to RIFT

Thanks for considering a contribution. RIFT is OSS quant infrastructure under Apache 2.0 — patches, bug reports, and strategy contributions are all welcome.

## Before you start

- Read [`README.md`](README.md) for the layer-by-layer architecture.
- Read [`docs/AUTH_AND_EXECUTION.md`](docs/AUTH_AND_EXECUTION.md) if you're touching anything trade-execution related.
- Read [`engine/SIGNALS.md`](engine/SIGNALS.md) if you're touching the signal factory.
- Search existing issues — your bug or feature may already be tracked.

## Development setup

```bash
# Clone
git clone <your-fork-url> rift
cd rift

# Python deps (uses uv to install Python 3.13 + all workspace packages editable)
cd engine && uv sync && cd ..

# Node deps (uses pnpm workspaces)
pnpm install

# Optional: set up AWS credentials for the data sync
cp .env.example ~/.rift/.env
# Edit ~/.rift/.env

# Verify
uv run --project engine pytest -m "not slow and not mainnet"
cd packages/cli && pnpm build && cd ../..
```

Required toolchain:

- **Python 3.13** (pinned via `.python-version`)
- **Node 20+** (pinned via `package.json` engines)
- **uv 0.4.x+** for Python package management
- **pnpm 9+** for Node package management

## Branch model

- `main` is the integration branch. CI must pass before merge.
- Feature work happens on `feature/short-description` branches.
- Bug fixes on `fix/short-description`.
- Open a PR against `main`. PRs require all CI jobs green before merge.

## What we look for in PRs

- **Tests** — new behavior is covered by tests. Existing behavior remains green.
- **No dead code** — if a feature is removed, every reference (code, docs, site copy, MCP descriptions) goes with it.
- **Honest framing** — don't claim something is validated, production-ready, or alpha-bearing unless the test data backs it up.
- **No silent network calls** — RIFT runs entirely under user control. Any new outbound destination needs explicit justification and a `PRIVACY.md` entry.
- **No telemetry, ever.** Crash reporters, install metrics, usage analytics — none of it. RIFT is local-first by design.
- **Commit messages** — imperative mood, concise subject line, body explains the *why*. Example: `Fix get_api_key fallback to legacy hl_wallet path`.

## Running the test suite

```bash
# Fast tests (excludes slow + mainnet)
uv run --project engine pytest -m "not slow and not mainnet"

# Slow tests (real data, longer runs)
uv run --project engine pytest -m "slow"

# Mainnet tests (touches real funds — opt in only)
uv run --project engine pytest -m "mainnet"

# TypeScript compile check
cd packages/cli && pnpm lint

# CLI smoke (every command parses + key reads run)
uv run --project engine pytest engine/tests/test_cli_smoke.py

# MCP smoke (requires TS build first)
cd packages/cli && pnpm build && cd ../..
uv run --project engine pytest engine/tests/test_mcp_smoke.py -m "slow"
```

## Strategy contributions

Reference: [`packages/strategies-sdk/src/rift_strategies_sdk/examples/trend_follow.py`](packages/strategies-sdk/src/rift_strategies_sdk/examples/trend_follow.py).

Before submitting a strategy:

1. It must pass the framework's promotion gates: `rift research <strategy_name> <pair> <tf>`.
2. The docstring must be honest about regime applicability and expected alpha decay.
3. If you make claims about historical performance, include the exact reproduction command in the docstring.
4. Strategies that fail walk-forward but cherry-pick a good in-sample window will not be merged.

## Updating dependencies

When you add, remove, or version-bump a Python or Node dependency:

- Re-run `uv sync` (Python) or `pnpm install` (Node) to update lockfiles.
- Commit the updated `uv.lock` / `pnpm-lock.yaml`.
- Update `NOTICE` if the dependency's license requires attribution.

## Licensing

By contributing, you agree that your contributions are licensed under Apache 2.0 (the project license). We do not require a separate CLA. Patent claims are covered by the Apache 2.0 patent grant.

If you're contributing on behalf of an employer, make sure your employer's IP policy allows it before submitting.

## What we will not accept

- Code that ships with telemetry, analytics, or crash reporting.
- Changes that weaken the authorization model (capability tiers, builder fee integrity, auth token gates) without an explicit security review.
- Strategies marketed as "winning" without walk-forward validation backing the claim.
- Cherry-picked backtest results presented as evidence of edge.
- Marketing copy added to source files. Source comments explain *why* the code does what it does. Marketing belongs in `README.md` or on the site.

## Questions

Open a discussion on GitHub Discussions, or email `nexstone@proton.me`.
