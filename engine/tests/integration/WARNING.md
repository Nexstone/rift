# ⚠️ WARNING — `test_phase0_mainnet.py` moves real money

## Read before running

This test executes a **real perpetual futures trade on Hyperliquid mainnet**.
It is intentionally hard to run so it cannot fire by accident.

### Cost per run

- **$10–12 USDC** of trade execution (HL minimum order is $10; test caps at $11)
- **$0.01–0.50 in fees + slippage** per round-trip (buy → close)
- Plus the one-time on-chain `approveAgent` call (free on HL — no L1 gas)

### Purpose

This is the **release sign-off gate** for RIFT. It validates end-to-end:

1. API wallet pairing on real mainnet (`approveAgent` → HL accepts)
2. Session token issuance with tight scope
3. Builder fee approval check (must pass before HL accepts the order)
4. Real order placement → fill returned with price
5. Position visible in HL holdings
6. Close (market sell, `reduce_only`) succeeds
7. Agent revoked → clean state

Composition tests (`test_phase0_pipeline.py`) use a mock exchange — they
catch glue bugs between modules but cannot catch HL API drift, encoding
issues, or builder-fee approval edge cases. **This file is the only
test that exercises the real chain.**

## How to run it

Both env vars must be set:

```bash
export RIFT_MAINNET_MAIN_KEY=0x<your funded mainnet wallet private key>
export RIFT_ACCEPT_MAINNET_RISK=1
```

The risk-ack env var exists as a deliberate friction layer. Set it ONLY
when you understand you're about to spend money. It does nothing on its
own — it just unblocks this test.

Then:

```bash
uv run --project engine pytest engine/tests/integration/test_phase0_mainnet.py \
    -m mainnet -v -s
```

The `-s` flag is important — it surfaces the step-by-step banner output.

## DO NOT run it if:

- You haven't worked through `MAINNET_CHECKLIST.md` in this directory
- Your test wallet is also your real trading wallet
- You don't have $15+ USDC on HL mainnet (need $11 for trade + buffer for slippage)
- The integrity hash (`_BUILDER_HASH`) hasn't been sealed for this release
- You don't have ~60-90 seconds to babysit it

## When to run it

Every Phase 0 release. Once per release, no more. CI never.

## What it does NOT validate

- The TypeScript CLI surface — that requires manual walkthrough
- Live algo / portfolio supervision — separate tests required
- Multi-asset strategies — only exercises BTC
- Stop-loss / take-profit branch logic — only does naked buy + close
