# Pre-mainnet checklist

Work through every item before running `test_phase0_mainnet.py`. Skipping
items will eventually cost you money.

## Code state

- [ ] On the commit you intend to release (no uncommitted local changes — `git status` clean)
- [ ] Full test suite passes — `pytest -m "not slow and not mainnet"` shows 0 failures
- [ ] Composition integration tests pass — `pytest engine/tests/integration/test_phase0_pipeline.py`
- [ ] Integrity hash sealed — `python3 scripts/seal_release.py --check` reports `Seal OK`
- [ ] `rift-engine version` reports the version you expect to ship
- [ ] `rift-engine doctor` shows all checks green (the `Hyperliquid API` check should succeed)

## Builder fee identity

The builder fee math is RIFT's revenue model. Double-check it.

- [ ] `BUILDER_ADDRESS` in `rift_trade/builder_fee.py` matches the address you control on Hyperliquid:
  ```
  0x0916EAb573817F02b96665386c944e297A765d7C
  ```
- [ ] You can sign with that address (or your team can) — verify access
- [ ] You've already approved the builder fee on Hyperliquid via `rift approve-builder-fee` from
      the operator's main wallet — HL requires per-account approval before builder fees start flowing
- [ ] Builder fee percentage in `BUILDER_FEE_DISPLAY` matches Hyperliquid's record for the address:
  ```
  0.03% perps / 1% spot
  ```

If any of the above is off, the test will pass but the fee won't flow correctly.

## Test wallet

The wallet you'll set as `RIFT_MAINNET_MAIN_KEY`:

- [ ] Is a **dedicated test wallet** — NOT the wallet holding meaningful funds
- [ ] Has been funded with $15-100 USDC on Hyperliquid mainnet ($15 minimum: HL's $10 order floor + slippage; more than $100 is wasted)
- [ ] Has no existing positions or orders (clean state for the smoke test)
- [ ] You have the seed phrase saved somewhere safe in case you need to recover later

**Never use your real trading wallet for this test.** The test wallet should
have just enough funds to cover the test trade + a couple dollars buffer.

## Environment

- [ ] You're on a stable internet connection (HL has ~50ms ping from US/EU)
- [ ] No mid-air token from a prior test on disk — clean `~/.rift/tokens/` if cluttered
- [ ] No mid-air API wallet from a prior test — `~/.rift/credentials` either reflects
      your real trading agent OR is absent. (The test uses tmp dirs, so this won't
      collide, but check anyway.)
- [ ] Mac/Linux: `umask 077` if you'll be inspecting credential files

## Both env vars set

- [ ] `RIFT_MAINNET_MAIN_KEY=0x<your funded test wallet key>`
- [ ] `RIFT_ACCEPT_MAINNET_RISK=1`

The test refuses to run unless BOTH are set. The second one is a "yes I really
mean it" gate to prevent accidental mainnet trades.

## Acceptance criteria for "passed"

After the test exits with `PASSED`:

- [ ] Test output shows `✔ Buy status: filled`
- [ ] Test output shows `✔ Close status: filled` (residual exposure cleaned up)
- [ ] Test output shows `✔ Agent revoked` (test wallet cleaned up)
- [ ] Test output shows `✔ Fee paid: $0.0XXX` (builder fee actually flowed)
- [ ] Within 5 minutes, check Hyperliquid UI manually:
  - Position should be flat (closed)
  - No leftover orders
  - The approved agent named "RIFT-MAINNET-SIGNOFF" should be revoked
- [ ] Within 10 minutes, check the builder fee address received its slice:
  ```
  Go to: https://app.hyperliquid.xyz/explorer/address/0x0916EAb573817F02b96665386c944e297A765d7C
  Look for the most recent inflow matching the test timestamp
  ```

If any of those are off, **do not ship**. Investigate.

## If the test fails

Common failures and what they mean:

| Failure | Likely cause | What to do |
|---|---|---|
| `approveAgent submission failed: Invalid signature` | EIP-712 schema drift in HL | Compare current HL docs to `rift_trade.api_wallet.build_approve_agent_action` |
| `Insufficient margin` | Test wallet underfunded | Fund test wallet with $15+ USDC (HL $10 min order + buffer) |
| `Trade rejected: ...` | Gate logic too strict or buggy | Re-run composition tests, check `rift_trade.gates` |
| `Close status: rejected` | Position open but not closeable | Close manually via HL UI immediately; investigate later |
| Test passes but no builder fee | Builder fee not yet approved on mainnet for your address | Run `rift approve-builder-fee` first |

## After a successful run

You've completed Phase 0 sign-off. The system is verified end-to-end against
mainnet. Proceed to the release steps in `/RELEASE.md` at the repo root.
