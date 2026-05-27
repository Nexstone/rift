# Phase 0 integration tests

Two layers of integration testing, with different prerequisites and
different signals.

## 1. Composition tests — `test_phase0_pipeline.py`

**Runs automatically** as part of the default test suite. No external
prerequisites. Uses a `MockHLExchange` instead of the real Hyperliquid
endpoint, but exercises **every Phase 0 module composed together**:

- `pair_wallet` → `issue_token` → `propose_trade` → `execute_proposal`
- Forged-token rejection, scope-violation rejection, kill-switch rejection,
  revoked-token rejection
- Audit-record emission and forensic-replay completeness
- Multi-leg execution (success + partial)
- Token + proposal disk round-trips preserve state
- Agent rotation flow

If a glue bug exists between modules — for example, `auth.canonical_token_bytes`
producing different bytes than `auth.verify_token_signature` reconstructs — these
tests catch it even though every unit test passes.

```bash
pytest engine/tests/integration/test_phase0_pipeline.py -v
```

12 tests, runs in <1 second.

## 2. Mainnet sign-off — `test_phase0_mainnet.py`

The Phase 0 release milestone: a real $11 trade on Hyperliquid mainnet with
full audit verification (position appears, builder fee paid, agent
revoked at end). Round-trip: market buy → close → revoke agent.

RIFT is **mainnet-only**. There used to be a separate testnet smoke test;
it was removed because (a) testnet liquidity made it a poor signal for
production behavior, and (b) we already have `rift backtest`, `rift simulate`,
and `rift test-trade` (minimum-size live mainnet trade) which cover the
"validate before risking real money" use cases with better fidelity.

**Doubly gated** — requires BOTH:

```bash
export RIFT_MAINNET_MAIN_KEY=0x<dedicated test wallet key>
export RIFT_ACCEPT_MAINNET_RISK=1
```

The risk-ack env var exists so this test can never run by accident — even
if someone leaves a mainnet key in their shell from another tool.

### Before you run this test

**Work through `MAINNET_CHECKLIST.md` in this directory.** It enumerates:

- Code state (clean git, all other tests green)
- Builder fee identity match (the address that receives revenue)
- Test wallet hygiene (dedicated wallet, ≤ $100 funded)
- Environment (env vars, file state)
- Pass criteria (what to verify in the HL UI after the test exits)

### Run

```bash
pytest engine/tests/integration/test_phase0_mainnet.py -m mainnet -v -s
```

The `-s` is important — the test prints a prominent banner explaining what's
about to happen, plus step-by-step progress. You want to see it.

### What it does (in order, against MAINNET)

1. Generate fresh API wallet locally
2. Sign `approveAgent` with funded main wallet → submit to HL mainnet
3. Issue session token with HARD scope: BTC only, $11/trade, $30/day
4. Fetch fresh BTC mid from mainnet
5. Build proposal sized to ≤ $11 notional (HL minimum order is $10)
6. Execute via `HyperliquidExchangeClient` → real chain order
7. Verify fill response includes tx hash
8. Fetch operator's holdings → verify BTC position appears
9. Close the position (market sell, reduce_only) → minimize residual exposure
10. Revoke the test API wallet → cleanup

Maximum financial exposure: ~$0.15-0.50 (slippage + builder fee on a $11 round-trip).
The test should complete in ~60-90 seconds.

### Expected output ends with

```
========================================================================
  PHASE 0 MAINNET SIGN-OFF: PASSED
========================================================================
  RIFT is verified to work end-to-end on mainnet.
  Phase 0 is ready to ship.
========================================================================
```

### After it passes

Proceed to the repo-root `RELEASE.md` for the actual release steps
(version bumps, git tag, PyPI + npm publish, GitHub release).
