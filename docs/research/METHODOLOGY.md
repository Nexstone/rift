# Research methodology

What `rift research` actually does, and why each gate matters. This is the math underneath the verdict.

---

## The 14 stages of `rift research`

A single `rift research <strategy> --pair X --tf Y` run executes 14 sequential stages. Each one can fail the strategy independently. The combined verdict is `PASS` only if all required gates pass.

```
1.  Load data            → 6,655 BTC 4h candles + 20,528 funding rates
2.  Backtest             → +25.0% return, Sharpe 0.71, 33 trades
3.  Walk-forward         → 70% profitable windows, OOS Sharpe 0.10
4.  Monte Carlo (10k)    → 91.13% probability of profit
5.  Multi-pair           → tested on 3 other coins for robustness
6.  Feature importance   → which indicators drove the signal
7.  Volatility forecast  → forward ATR estimate
8.  Health check         → CUSUM + regime + decay → 84/100 grade A
9.  Purged CV (k-fold)   → 4/5 folds positive with embargo + purge
10. Alpha decay          → IC half-life of the signal
11. Capacity             → max trade size before impact eats edge
12. Cross-impact         → multi-asset basket execution (skipped on single-pair)
13. Promotion verdict    → 5 gates, all must pass
14. Sealed bundle        → cryptographic reproducibility manifest
```

Below: what each one is asking, and what a failing answer means.

---

## Backtest

**Question:** assuming everything I'm about to test is fair, what would this strategy have done historically?

Event-driven simulation on real Hyperliquid candle data with real funding rates baked into P&L. Trades fill at the candle's open (if the signal fires at close of bar N, the order lands at open of bar N+1) with optional slippage modeling.

**What's honest:** the trade count, the equity curve, the drawdown profile.
**What's still suspect:** everything past this stage exists *because* the backtest result is suspect on its own. A pretty backtest is the start of the conversation, not the end.

---

## Walk-forward analysis

**Question:** does the strategy still work on data it wasn't tuned on?

Splits the dataset into rolling train/test windows (default 6 months train, 3 months test). For each window:
1. Tune the strategy's parameters on the train segment
2. Apply those parameters to the test segment
3. Measure how the test segment performs

**What "degradation ratio" measures:** test-Sharpe / train-Sharpe. A ratio near 1.0 means the strategy generalizes. A ratio of 0.5 means half the in-sample edge is lookback bias.

**Default threshold:** 70% of windows must be profitable. Fewer than that and the strategy is overfitting to the training window structure.

`trend_follow` on BTC 4h: 70% windows profitable, degradation 0.35 (label: WEAK). Translation: it works *most* of the time but loses roughly two-thirds of its in-sample edge out-of-sample. Honest reading: marginal edge, will sometimes drawdown badly, do not size aggressively.

---

## Monte Carlo

**Question:** how much of this strategy's return is luck vs. edge?

Takes the actual trade-return sequence and bootstrap-resamples it 10,000 times with replacement. For each resampled path:
- Compounds returns to get a final equity
- Tracks the maximum drawdown
- Records whether the path ended profitable

Outputs:
- **`prob_profit`**: fraction of paths ending up. 91% means there's a 9% chance the realized run was the bad ninth and a 91% chance it'd repeat.
- **`prob_ruin`**: fraction of paths that drew down more than the configured ruin threshold (default 50%).
- **`p5 / p50 / p95`**: 5th, median, 95th percentile of final returns. The spread tells you path-dependence — narrow spread = robust, wide spread = lucky path.

**Caveat:** MC bootstrap preserves the *distribution* of trade returns but discards their *order*. A strategy that lost early and won late may show low MC variance but still ruin you if you start it during the loss phase. Walk-forward catches that; MC alone doesn't.

---

## Multi-pair

**Question:** is this an edge or a BTC quirk?

Applies the same strategy with the same config to N other coins (default 3: HYPE, ETH, ZEC). Reports per-pair Sharpe + return.

`trend_follow` on BTC works (Sharpe 0.71). On HYPE: 0.48. On ETH: 0.22. On ZEC: 1.12. Three of three pairs profitable means the edge isn't pair-specific. If only BTC worked, you'd have a one-asset strategy at best, an overfit at worst.

---

## Feature importance

**Question:** which signals actually drove the result?

Permutation importance: shuffle each indicator's values, re-run the backtest, measure how much performance degrades. Indicators whose shuffling kills performance were load-bearing. Indicators whose shuffling barely affects anything were noise.

Useful for: figuring out what your strategy actually pays attention to, vs. what you *think* it pays attention to. Often these differ.

---

## Volatility forecast

**Question:** what's the volatility regime ahead, and is the strategy suited to it?

Fits a GARCH(1,1) model to recent returns, projects forward 30 days. Used by the algo daemon to decide sizing (vol-targeted position sizes scale inversely to forecast vol).

Standalone in research, this stage is mostly informational. Where it matters: if forecast vol is expanding rapidly, mean-reversion strategies are entering a hostile regime, and trend strategies are entering a friendly one.

---

## Health check

**Question:** is this strategy *currently* working, or is it decaying?

Combines five sub-scores:
- **CUSUM:** detects shifts in the return distribution (regime breaks)
- **Regime:** consistency of the strategy across detected market regimes
- **Decay test:** are recent returns systematically worse than historical?
- **Alpha:** is the residual edge after subtracting market beta still positive?
- **Execution:** TCA — entry/exit slippage, fill quality

Outputs a 0–100 score and a letter grade. Above 70 = continue. Below = consider parameter refresh or retirement.

This stage isn't a release gate — it's diagnostic. A research run gets an "A" if the strategy is healthy today, regardless of whether it passes promotion. A passing strategy that's already decaying is a strategy you don't want to deploy.

---

## Purged k-fold cross-validation

**Question:** if I split the data into 5 chunks at random, does the strategy work on 4 of them?

Random splits + embargo (buffer between train/test to prevent leakage) + purging (remove overlapping samples). Outputs Sharpe per fold and a pass rate.

**Why it matters:** walk-forward is sequential (recent train → next-period test). Purged CV is non-sequential — it can train on 2024-Q1 + 2025-Q1 and test on 2024-Q3. Catches regime-dependent strategies that walk-forward might miss.

**Default threshold:** 70% of folds must be positive (Sharpe ≥ 0).

---

## Alpha decay

**Question:** how long is the signal good for?

Computes Information Coefficient (IC = correlation between signal and forward returns) at multiple horizons (1h, 2h, 5h, 10h, 20h, 50h). Fits an exponential decay curve. Reports half-life (τ) — the time it takes for IC to drop to half its initial value.

**Interpretation:** if your strategy holds for 8 hours but the signal's half-life is 2 hours, you're holding past the edge. If half-life is 50+ hours and you hold for 4, you're missing most of the move.

`trend_follow` half-life is not measurable in the bundled run because IC stays near zero across horizons — the signal is so noisy that decay is dominated by random walk. Honest reading: trend_follow doesn't have a clean alpha-decay signature; it works through path-dependence (capturing long moves) rather than per-bar predictability.

---

## Capacity

**Question:** how much money can I run through this strategy before market impact eats the edge?

Models three constraints:
- **Impact:** estimated price impact as a function of trade size, calibrated against L2 book depth
- **ADV:** average daily volume — you can't trade more than a fraction of it
- **Edge erosion:** at what size does slippage eat the entire backtest alpha?

Outputs `max_trade_size_usd` (the binding constraint, usually ADV for liquid coins, impact for illiquid ones) and `half_alpha_size_usd` (the size at which slippage eats half your edge).

For `trend_follow` on BTC: $403M ADV-bound, $434M impact-bound. Translation: you can run nine figures through this strategy on BTC alone. The framework will not let you scale aggressively on shitcoins without telling you the math says don't.

---

## Cross-impact

**Question:** if I trade multiple correlated coins, do my trades step on each other?

Estimates the correlation matrix of returns and the cross-impact of multi-asset baskets. Skipped on single-pair runs.

Important for: portfolios. Useless for: single-strategy single-coin deployment.

---

## Promotion verdict — the 5 gates

This is the bouncer. A strategy passes only if all five gates pass.

| Gate | Default threshold | Why |
|---|---|---|
| **Deflated Sharpe Ratio (DSR)** | ≥ 0.85 | Adjusts observed Sharpe for the number of trials you ran (parameter sweeps, multiple strategies). A high in-sample Sharpe means little if you tried 50 configs to get it. DSR penalizes that explicitly. |
| **CV pass rate** | ≥ 70% | At least 70% of purged k-fold folds must be positive. Catches strategies that look great in aggregate but fail on most slices of history. |
| **Capacity** | ≥ $10K | The strategy must be able to deploy at least $10K without slippage destroying the edge. Sub-$10K capacity means academic interest, not deployable alpha. |
| **Track record** | ≥ 1000 observations and ≥ 25 trades | Tiny sample sizes produce noise that masquerades as edge. The minimum keeps honest. |
| **Max drawdown** | ≤ 25% | Above 25% drawdown is institutional-investor disqualifying. A strategy whose backtest drew down 40% will draw down 40%+ live; you will turn it off before it recovers. |

A strategy can override the defaults for legitimate reasons. A 4h trend-follower that fires 30 trades over 2 years can declare `min_trades=25`. A market-neutral strategy with high turnover can declare `max_dd_pct=0.10` (tighter, not looser).

What gets rejected as dishonest: lowering `min_trades` to 5 because your strategy only got lucky once.

---

## Sealed reproducibility bundle

After all stages pass, the framework writes a content-addressed JSON manifest to `~/.rift/bundles/<hash>.json` containing:

- The strategy source code (full text, not a reference)
- The strategy config used
- The exact dataset hash (so you know which candles were used)
- Every metric from every stage above
- The framework version
- A SHA256 hash of all the above

The hash is the filename. Two runs that produce the same hash are bit-identical results. Run the same strategy on the same data with the same framework version — you'll get the same bundle every time. That's reproducibility, not "trust me, the screenshot looks good."

You can ship a bundle hash to anyone running RIFT, and they can verify it locally:

```bash
rift verify <bundle-hash>
```

---

## What the framework explicitly is *not*

- **Not a guarantee.** Passing 5/5 gates means the strategy was statistically defensible on historical data. Markets drift. The framework can't see the future.
- **Not a substitute for capacity discipline.** A strategy that passes at $10K capacity will fail at $10M if you ignore the warning. The framework tells you the size; respecting it is on you.
- **Not protection from bad assumptions.** If your strategy assumes funding is always negative on shitcoins (often true, sometimes spectacularly false), the framework will tell you the historical hit rate. It won't tell you when the assumption breaks.
- **Not insurance against your own behavior.** No backtest survives if you turn the strategy off after the first drawdown and back on after the recovery. Discipline is yours.

The framework's job is to make sure you're not lying to yourself about the past. Whether the future cooperates is between you and the market.
