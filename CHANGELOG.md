# Changelog

All notable changes to RIFT are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.3] - 2026-05-29

### Fixed
- **`rift` CLI now works for end-user installs.** Previously the TS CLI walked the filesystem looking for `engine/pyproject.toml` + `engine/src/rift/cli.py` — a layout that only exists in a source clone. Users who installed via `pip install rift-engine-core` + `npm install -g @nexstone/rift-cli` (or `brew install Nexstone/tap/rift`) got a `rift` binary that threw "Cannot find RIFT engine directory" on every command. `packages/cli/src/lib/python-bridge.ts` now detects three modes in priority order: (1) `RIFT_ENGINE_BINARY` env var → use that binary, (2) source clone → use `python -m rift.cli` with PYTHONPATH set, (3) `rift-engine` on PATH → use that binary. The Homebrew formula now also sets `RIFT_ENGINE_BINARY` explicitly to pin the libexec venv's binary even if the user has another `rift-engine` installed elsewhere.
- `getStrategiesDir()` now returns `~/.rift/strategies` for installed users (created on demand) instead of a nonexistent path under a non-source layout.
- README "End-user install" now correctly says both `pip install rift-engine-core` AND `npm install -g @nexstone/rift-cli` are needed for the `rift` command — previously implied any one alone would work.

## [0.1.2] - 2026-05-29

### Fixed
- README "59 tools" MCP count corrected to 64 in three places — the count went from 59 to 64 in v0.1.1 with the addition of `spot_buy`, `spot_sell`, `perp_long`, `perp_short`, and `perp_close` namespaced aliases. The Changelog noted the additions but the README references were not updated.
- `.github/workflows/release.yml` `publish-npm` job: `npm publish dist-npm/*.tgz` was failing with `exit code 128` because npm 9+ interprets a path arg containing `/` as a `<github-org>/<repo>` package spec. Job now runs from `working-directory: dist-npm` with `npm publish *.tgz`, keeping the filename arg slash-free. This was cosmetic for v0.1.1 (the package was published manually from a developer machine) but blocks future CI-driven releases.
- `engine/src/rift/__init__.py` `__version__` was stuck at `0.1.0` despite the package shipping as v0.1.1. Now correctly tracks the release version.

## [Unreleased]

### Added
- Initial open-source release preparation
- `CODE_OF_CONDUCT.md`, `SECURITY.md`, `CONTRIBUTING.md`, `NOTICE`, `PRIVACY.md`, `KNOWN_ISSUES.md`
- GitHub issue + pull request templates
- MCP smoke test (`engine/tests/test_mcp_smoke.py`)
- `.python-version` pinning Python 3.13 for dev environments

### Changed
- `package.json` and `packages/cli/package.json` license corrected from `MIT` to `Apache-2.0` (the LICENSE file was always Apache-2.0; the package.json fields were incorrect)
- **Distribution consolidated.** RIFT now ships as a single `rift-engine-core` wheel that bundles all 10 internal namespace packages (`rift_core`, `rift_data`, `rift_engine`, `rift_trade`, `rift_portfolio`, `rift_research`, `rift_api`, `rift_strategies_sdk`, `rift_substrate`, `rift`). End-user install: `pip install rift-engine-core`. Previously each namespace was a separate PyPI distribution; the multi-package layout hit PyPI's new-project rate limit during the v0.1.0 release and is replaced by this single-wheel design. Internal Python imports and the dev workflow (`uv sync` from `engine/`) are unchanged.

### Fixed
- `get_api_key()` now reads from the canonical `~/.rift/credentials` file in addition to the legacy `~/.rift/hl_wallet` path, fixing a regression where users past the initial `rift auth setup` flow could not place trades
- `manual_trade` no longer blocks 2 minutes on a websocket soak it never needed
- `algo` TS wrapper now passes the strategy as `--strategy <name>` (was sending it as a positional arg, causing the Python daemon to reject input and exit immediately)
- `_build_state_dict` no longer raises `NameError` on `health_report` / `health_paused` — added as optional parameters with safe defaults
- `spawnDaemon` no longer injects an unsupported `--strategies-dir` flag that crashed the algo daemon at argparse
- `recon.run_recon()` no longer crashes at `Info(base_url, …)` — the duplicate `Info` constructor (the canonical `get_info_client()` is already called earlier) has been removed. This was a leftover from the `--sim` rip that broke every recon trade taken via a direct `run_recon()` caller.
- `recon` stop-loss placement no longer fails with `"Unknown format code 'f' for object of type 'str'"` — `triggerPx` is passed as a float (not `str(...)`-wrapped), matching what the Hyperliquid SDK expects.
- `test_trade` command had the same `triggerPx: str(...)` bug; fixed.
- `_check_orphaned_positions` no longer silently fails when a session-state file persists across daemon restarts with a null position. The condition now correctly flags any HL position not tracked by RIFT, regardless of whether the saved state file exists.
- The orphan-position warning text is now honest about the actual behavior: RIFT detects but does NOT manage the orphan; the warning directs the user to close the position manually before continuing.

### Changed (safety)
- **Recon now refuses to ride without an exchange-side stop.** If `exchange.order(...)` for the stop-loss trigger throws or returns a non-ok status after a limit fill, recon immediately closes the just-opened position via reduce-only IOC and exits. Previously the error was logged and the monitor loop continued without protection.
- **Recon monitor now has a local stop check as defense-in-depth.** Even when the exchange stop is in place, the monitor's tick loop exits with `exit_reason=stop_loss` if the mid crosses the stop price — catching the rare case where the exchange stop disappears or HL's stop reference differs from the public mid.
- **Algo daemon refuses to start when an orphan position is detected.** Previously it emitted a misleading "will manage it" warning and continued running with `position=None`, which would double-up or net-flat on the next entry signal. Now it emits a clear error pointing to `rift more close-position <coin>` and exits non-zero. Crash-recovery (saved state with an actual position record) is unchanged.
- **`rift recon --confirm <N>` warns when N < 65.** The tape-aggregation bucket flushes at the 60-second mark, so values below ~65 are guaranteed to abort with `tape_not_confirmed`. The flag's help text now documents this floor, and an explicit warning fires at command start when an out-of-range value is passed.

### Changed (hardening + UX — Phase 2)
- **README install steps** corrected: `cp .env.example ~/.rift/.env` now prefixes `mkdir -p ~/.rift &&` (fresh installs don't have the dir); explicit PATH/symlink instructions added for the `rift` bare command.
- **README quickstart** fixed: `rift research trend_follow BTC 4h` (which fails — positional args) replaced with `rift research trend_follow --pair BTC --tf 4h`.
- **Doctor "Builder fee not approved" hint** corrected from `run: rift research` (wrong) to `run: rift auth setup` (correct — that's the command that drives builder-fee approval).
- **Doctor expanded** with four new first-time-user checks: `~/.rift/` directory existence, `.env` file permissions (warns if not 0600), AWS credentials presence (info-level, since they're only needed for `rift sync`), and disk space (fails at <500MB, warns at <2GB).
- **Error messages on hot paths** rewritten to be actionable. Where the old message was bare `str(e)` or one-liners like `"Order failed"` / `"Builder configuration error"`, the message now names the failure mode AND a concrete next command. Touched sites: `manual_trade.py`, `recon.py`, `algo.py` (preflight, signal-detection loop, strategy-not-found), and the duplicate "Cannot query account" / "Insufficient balance" / "Cannot get price" emits across the trade entry points.
- **`portfolio.yaml` is now schema-validated.** Loading a malformed config raises a `ValueError` that names the specific bad field (e.g. `strategies.0.timeframe: Field required`) instead of failing mid-supervisor with a stack trace. Allocations are also checked to sum to ~1.0. The example `strategies/configs/portfolio_btc.yaml` is updated to reference the actually-shipping strategy (`trend_follow` instead of the long-removed `btc_funding_fade`).

### Added (docs)
- **`docs/BACKUP_AND_STATE.md`** — full inventory of `~/.rift/`, what's critical/high/medium/cache, three backup strategies (minimal / complete / archive), and a machine-to-machine migration walkthrough.

### Added (positioning + docs — Phase 3)
- **README top-level disclaimer.** First thing a visitor reads now: RIFT is software, not financial advice; past performance ≠ future returns; trading perps can lose all capital; the shipped strategy is demo-only.
- **`docs/QUICKSTART.md`** — 10-minute path from `git clone` to a sealed research bundle. No wallet, no AWS, no live trading required.
- **`docs/INSTALL.md`** — full install for macOS, Ubuntu/Debian Linux, WSL2 on Windows. Native Windows declared unsupported.
- **`docs/strategies/AUTHORING.md`** — how to write a strategy from scratch using the SDK. Walks through `trend_follow` as the reference and covers config dataclass, indicators, signal shapes, promotion gates, and the iteration workflow.
- **`docs/research/METHODOLOGY.md`** — what each of the 14 stages of `rift research` actually computes. Honest explanation of what walk-forward / Monte Carlo / purged CV / DSR / alpha decay / capacity / promotion gates buy you, and what they can't protect you from.
- **`docs/mcp/SETUP.md`** — wire RIFT into Claude Desktop / Claude Code / Cursor as an MCP server. Lists the 59 exposed tools by group and includes a security note about the local trust boundary.
- **`docs/CLI_REFERENCE.md`** — auto-generated per-command help text for all 40 top-level commands. Stays in sync via `bash scripts/gen_cli_reference.sh`.
- **`scripts/gen_cli_reference.sh`** — regenerator script (introspects `rift --help`, strips ANSI, emits markdown).
- **`strategies/configs/portfolio_btc.yaml`** updated to reference the actually-shipping `trend_follow` strategy instead of the long-removed `btc_funding_fade`.

### Changed (Phase 4 — dogfood pass)
- **`docs/QUICKSTART.md`** clarified that the `+25% / Sharpe 0.71` metrics quoted from the strategy docstring are only reproducible against the full 2-year archive (`rift sync` required). Without sync, `rift research` auto-fetches whatever recent window HL serves on demand, and the numbers will differ — by design, since backtest results are window-dependent.
- **`docs/RUNBOOK_ALGO_MONITORING.md`** added — daily/weekly checks for a long-running daemon, what's normal, what's not, when to intervene.
- **`docs/strategies/AUTHORING.md`** corrected to match the actual SDK: `Signal.long(size=...)` not `Signal.long(size_fraction=...)`, and `state.position` is a signed float (positive=long, negative=short, zero=flat) not a `Position` object.

### Fixed (Phase 4 — verified live via a one-off `phase4_verify` test strategy)
- **Algo daemon could not size a position when the strategy emitted a Signal with a stop loss.** The sizing line `sl_pct_trade = sig.stop_loss or sl_pct` treated `sig.stop_loss` (an absolute PRICE) as a percentage, making the denominator at line ~906 of `algo.py` ~75,000 instead of ~0.003. Result: every algo entry collapsed to a sub-$10 notional and fell silently below Hyperliquid's $10 minimum order value, so the daemon NEVER placed an entry. Trend_follow had never traded live in any of our verifications because of this. Fix converts `sig.stop_loss` to a percentage of `last_price` before sizing.
- **Algo daemon placed duplicate stop loss orders** when the entry under-filled by a small amount (e.g., HL rounded 0.00016 down to 0.00015). The partial-fill recovery branch fired at any underfill >1%, cancelled the original stop, and placed a new stop — but the cancel-ack often races the new placement, leaving both orders resting on HL. Both stops were `reduce_only=True` so the financial risk was nil (HL's reduce-only enforcement caps the close at actual position size), but it was an order-book leak. Fix: only re-place the stop on >50% underfills (genuine partial fills), and let `reduce_only` enforcement handle small rounding mismatches naturally with the originally-bundled stop.

### Added
- **End-to-end algo daemon trade execution is now verified on real mainnet.** Phase 4 used a temporary `phase4_verify` fast-signal strategy (since deleted) to observe: signal generation → portfolio gate check → position sizing → atomic entry+stop via `normalTpsl` → live monitoring → SIGTERM-triggered close → session log artifact. Round trip ~$0.02 in fees + slip. The bugs above were both caught by this exercise.
- Removed dead `--sim` flag and `recon_sim` mode entirely; only live execution remains in `recon`
- Removed dead `ab-test` / `ab-result` commands that depended on a non-existent `simulate` command
- Removed `sim_sessions` field from `state` command output and corresponding MCP tool surface
- `trend_follow` strategy now ships with a clear "demo / reference strategy" disclaimer in both the module and class docstrings, surfacing in `rift strategies list`

### Removed
- Stale `--sim` references throughout the engine + MCP tool surface
- `ab_test` and `ab_result` MCP tools (the underlying commands depended on the removed `simulate` command)
- Sim-mode branches from `history` command and its MCP tool

## How to read this file

- **Unreleased** = work that's landed on `main` but not yet tagged.
- Each released version gets its own section, dated.
- We follow Keep-a-Changelog categories: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`.
- Breaking changes are called out explicitly under a `Breaking` heading inside the relevant version.
