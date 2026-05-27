# RIFT — Authorization & Execution

Reference for RIFT's trust architecture: how trades get authorized, signed, and executed on Hyperliquid.

---

## Architecture summary

### Capability tiers

Every action an AI agent or human operator can take falls into one of four tiers:

| Tier | Verb | Auth required | Examples |
|---|---|---|---|
| **T0 — Read** | observe | none | load candles, scan fills, query portfolio state |
| **T1 — Simulate** | analyze | none | backtest, walk-forward, Monte Carlo |
| **T2 — Propose** | recommend | none | construct structured trade proposals with rationale |
| **T3 — Execute** | act | **authorization token** | place / cancel / modify any on-chain trade |

T0–T2 are unconditional and high-bandwidth. T3 is the only tier with an authority check.

### Three-layer key model (Hyperliquid-native)

Hyperliquid's protocol natively supports an "API wallet" (also called "agent wallet"). RIFT uses it correctly:

| Layer | Storage | Purpose | Safety |
|---|---|---|---|
| **Main wallet** | external (Rabby / MetaMask / hardware via WC) — RIFT never sees the key | sign API wallet registration; sign withdrawals; sign auth tokens at issuance | only the operator's wallet app ever sees it |
| **API wallet** | local file at `~/.rift/credentials` (private key generated locally) | sign every order that hits HL during trading | **chain-enforced: cannot withdraw funds**. Compromise = bad trades only, never capital loss. Revocable by main wallet at any time. |
| **RIFT auth token** | local file at `~/.rift/tokens/{id}.json` | gate which trades `execute_proposal` will pass to the API wallet for signing | signed once by main wallet via WC at issuance; pure RIFT-internal policy, never sent to HL |

**Critical separation:** WalletConnect is needed only at issuance / withdrawal time, NOT at trade time. Autonomous trading is fully independent of WC session liveness — `rift algo` runs against the local API wallet key indefinitely.

### Token issuance modes

All three modes produce a `RIFT auth token` of identical shape, differing only in how/when they're signed:

1. **Per-trade approval (default for new installs).** No standing token exists. Each T3 action triggers a synchronous prompt; operator approves in WC-paired wallet, which signs a single-use token covering exactly that action.

2. **Session token (opt-in).** Operator issues a time-bounded token (e.g., "any buy/sell on ETH or SUI, up to $500 per action, $2000/day, expires in 4 hours"). **Default expiry: 4 hours.** Signed once via WC, then sits on disk.

3. **Long-lived strategy token (advanced opt-in).** Operator issues a token bound to a *validated* strategy. No time-based expiry, but enforces size caps + daily caps + global kill switch. Operator can revoke at any time.

### Safety primitives — always on

Independent of token scope:

1. **Global kill switch** — file flag at `~/.rift/KILL`, MCP-settable, CLI-settable. When active, no T3 actions proceed regardless of token. Existing positions untouched (panic-close is a separate explicit T3 action).
2. **Circuit breakers** — hard daily volume cap, drawdown trigger, max open position count. Operator-config only, not AI-modifiable.
3. **Pre-execution sanity gates** — margin check, slippage check vs current book, correlation limit check.
4. **Audit guarantee** — every T3 attempt (success or rejection) leaves a record. T3 actions that cannot write their audit record fail closed.

### Structured audit envelope

```python
class DecisionRecord:
    id: uuid
    timestamp: int                          # ms since epoch UTC
    actor: Actor                            # who: human, agent, or system
    kind: DecisionKind                      # OBSERVE | SIMULATE | AUTHOR | PROPOSE | AUTHORIZE | EXECUTE | GATE_REJECT | KILL_TOGGLE
    inputs: dict                            # everything actor saw (typed per kind)
    outputs: dict                           # what was decided/produced (typed per kind)
    rationale: str | None                   # required for T2/T3
    parent_id: uuid | None                  # links into a decision chain
    package: str                            # which rift package emitted this
    version: str                            # rift version at emit time
```

Storage: NDJSON to stdout + append-only Parquet at `~/.rift/audit/{YYYYMMDD}.parquet`. Both writes synchronous on T3; failure to write blocks the trade.

---

## UX decisions

| # | Decision | Behavior |
|---|---|---|
| 1 | Network | **mainnet-only**. Testnet support was removed — `rift backtest`, `rift simulate`, and `rift test-trade` (minimum-size live trade) cover the "validate before risking real money" use cases. |
| 2a | First-run onboarding | Bare `rift` triggers wizard if `~/.rift/` not initialized, else shows home menu |
| 2b | Default wizard scope | **Full setup (wallet + everything)**. `--research-only` flag for explicit opt-out |
| 2c | Resume behavior | **Idempotent** — re-running picks up where previous run left off |
| 2d | Reset path | `rift init --reset` deletes credentials/env, walks setup from scratch |
| 3 | Signature display | **Raw signed payload shown** in TUI for transparency |
| 4a | Agent address visibility | Shown in `rift doctor`, `rift auth status`, and live-trading command headers |
| 4b | Persistent status footer | One-line footer on every interactive (TTY) CLI command. Suppressed when stdout is piped. |
| 4c | Footer indicators | **● green** = live ready · **○ gray** = research-only · **⚠ yellow** = incomplete setup · **✗ red** = broken (kill switch / agent revoked) |
| 5 | Builder fee copy | (verbatim, do not modify) *"RIFT is free and open-source. To support ongoing development, a 0.03% builder fee is applied to perps trades (1% on spot) executed through RIFT on Hyperliquid. Backtesting, simulation, and analysis are always free. Example: On a $10,000 perps trade, the fee is $3."* |
| 6a | Agent name (HL-displayed) | **Hardcoded `"RIFT"`** (all caps for brand recognition in users' approved-agents lists) |
| 6b | Agent name override | `--agent-name <custom>` flag for the 5% who want non-default |
| 7 | Recovery / rotation | `rift auth rotate-agent` — does it right (revoke old, generate new, register new, all via WC) |

---

## HL Account Modes

Hyperliquid supports four account abstraction modes. RIFT detects the user's mode at
`agent-pair` time and reads collateral correctly for each via the single
`rift_data.account_mode.read_collateral()` helper (TS mirror in
`packages/cli/src/lib/account-mode.ts`).

| Mode (HL native) | RIFT name | What it means | How RIFT reads collateral |
|---|---|---|---|
| `disabled` | `standard` | Separate spot and perp balances. Manual `usd_class_transfer` to move between. | Perp `accountValue - margin_used`. Spot USDC ignored for gates. |
| `unifiedAccount` | `unified` | **HL UI default.** Spot USDC IS perp collateral, used automatically. | Perp + spot USDC summed. |
| `portfolioMargin` | `portfolio_margin` | Pooled collateral across USDC + LTV-weighted HYPE/BTC/USDH. Requires $10k account value or $5M lifetime volume. | Same as unified per HL docs (perp state is "not meaningful"; real collateral in spot). Counts USDC only; non-USDC PM collateral under-counted (LTV table not in HL info endpoints). |
| `dexAbstraction` | n/a | Being discontinued by HL. RIFT does not target this mode. | Reported as `unknown` mode; treated as unified (sum) for safety. |

### Commands

- **`rift account-mode-status <addr>`** — Print mode + full collateral
  breakdown (perp value, margin used, perp available, spot USDC, total tradeable, is
  perp-only). No write operations.
- **`rift account-mode-set <mode> --local-main-key <key>`** — Switch the
  wallet's mode. Modes: `standard | unified | portfolio_margin`. Fail-loud on HL errors
  (e.g. PM $10k minimum). Re-queries to confirm. Warns about asymmetric consolidation
  behavior (Standard → Unified auto-moves USDC perp → spot; Unified → Standard leaves
  USDC in spot — user must manually transfer to refund perp).

### What RIFT does NOT do

- **Switch a user's mode without their consent.** `agent-pair` only *detects* the mode
  and emits a hint. Users must explicitly run `account-mode-set` to change it.
- **Value non-USDC collateral under PM.** Counts USDC only for PM users. HL
  doesn't expose LTV ratios via any documented info endpoint; the published values
  (e.g. `HYPE = 0.5`) are pre-alpha and changeable. PM users with HYPE/BTC/USDH
  spot positions will see less available margin in RIFT than HL actually grants.

---

## Footer rendering specification

### When to render

- **Render:** every CLI command whose stdout is a TTY (interactive terminal)
- **Suppress:** when stdout is piped (e.g., `rift list-data | jq`), redirected to file, or run under CI

Detection: `process.stdout.isTTY` in TS, `sys.stdout.isatty()` in Python.

### Format

```
──────────────────────────────────────────────────────────────────────
<indicator> <state-label>  ·  <details>
```

### State matrix

| State | Indicator | Label | Details |
|---|---|---|---|
| Fully set up | `●` (green) | `live ready` | `agent 0x9b14…3a7c · mainnet · rift v$VERSION` |
| Research-only | `○` (gray) | `research-only` | `run \`rift init\` to enable trading` |
| Setup incomplete | `✗` (red) | `setup incomplete` | `run \`rift\` to continue` |
| Kill switch active | `✗` (red) | `KILL SWITCH ACTIVE` | `run \`rift kill --off\` to resume` |
| Agent revoked on-chain | `✗` (red) | `agent revoked` | `run \`rift auth rotate-agent\`` |

State is computed once per CLI invocation (cheap — just file existence checks).

---

## What we are explicitly NOT doing

- ❌ **AI-issued authorization tokens.** Agents cannot grant themselves authority. Period.
- ❌ **AI-modifiable circuit breakers.** Operator config only.
- ❌ **Bypass paths for "trusted" agents.** Every T3 action goes through `execute_proposal`. No back doors.
- ❌ **Aggregating multi-user accounts.** RIFT operates one wallet at a time.
- ❌ **Cloud-hosted variant.** Local-first. API wallet key + tokens live on operator's machine. Main wallet keys live in operator's wallet app.
- ❌ **Approval-by-default for any T3 action.** Per-trade approval is the floor; opt-ins go up from there, never down.
- ❌ **Main wallet keys on RIFT's disk.** Only the API wallet's key is stored locally. Main wallet signs exclusively via WalletConnect.
