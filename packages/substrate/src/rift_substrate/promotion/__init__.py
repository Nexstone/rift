"""Strategy promotion gates — yes/no for "should this strategy go live?"

A backtest that looks good is not enough. Quant shops gate strategies behind
a battery of statistical and operational checks before live capital risks.
This module packages the standard institutional checks as composable gates:

  - **Deflated Sharpe Ratio** — Bailey & López de Prado (2014). Probability
    the observed Sharpe reflects a real edge, deflated for selection bias.
  - **Out-of-sample CV pass rate** — fraction of purged-CV folds where the
    OOS Sharpe clears a threshold. Catches strategies that fit the train
    period but break in test.
  - **Capacity** — analyze_capacity must allow at least a minimum trade
    size. Strategies whose impact eats the alpha at any practical size
    fail this gate even if their backtest Sharpe is great.
  - **Track record** — minimum number of observations + trades. A 3-trade
    backtest can have a Sharpe of 5 by luck.
  - **Max drawdown** — peak-to-trough loss within bounds.

Each gate is its own function; `evaluate_promotion()` runs the configured
set and returns a `PromotionVerdict` with PASS/FAIL plus a per-gate
breakdown for transparency.

This module deliberately ships SENSIBLE DEFAULTS but never opinions about
which gates to apply or what thresholds to use. The defaults are
industry-standard starting points (DSR > 0.95, CV pass rate > 70%,
max DD < 20%) — callers tune for their style.

Reference:
  Bailey, D. H. & López de Prado, M. (2014). "The Deflated Sharpe Ratio:
    Correcting for Selection Bias..." J. Portfolio Management.
  López de Prado, M. (2018). "Advances in Financial Machine Learning."
    Wiley. Ch. 11 on backtest overfitting.
"""

from rift_substrate.promotion.core import (
    GateResult,
    PromotionVerdict,
    evaluate_promotion,
    gate_capacity,
    gate_cv_pass_rate,
    gate_deflated_sharpe,
    gate_max_drawdown,
    gate_track_record,
)

__all__ = [
    "GateResult",
    "PromotionVerdict",
    "evaluate_promotion",
    "gate_capacity",
    "gate_cv_pass_rate",
    "gate_deflated_sharpe",
    "gate_max_drawdown",
    "gate_track_record",
]
