"""RIFT strategies SDK — scaffold + validator + shipped OSS reference.

Importing this package auto-registers the bundled example strategy via
its `@register(...)` decorator. After `import rift_strategies_sdk`,
the example name appears in `rift_engine.strategy.list_strategies()`.

Public surface:
  scaffold:  `rift new <name>` — generate a strategy skeleton
  validator: runs preflight checks on a strategy file
  examples/: one worked-example OSS strategy shipped with the engine

The example is `trend_follow` — a bidirectional EMA-crossover regime
strategy that passes RIFT's full promotion pipeline (5/5 gates) on BTC
4h with default parameters. It's coin-agnostic; run it on any coin with
`rift research trend_follow <COIN> 4h`. See the file's docstring for
the validated results and extension ideas for learners.
"""

# Auto-import example strategies so @register fires.
# noqa: F401 — this import has side effects (strategy registration).
from rift_strategies_sdk.examples import trend_follow  # noqa: F401

# Public validator surface — runs preflight checks on a strategy file.
from rift_strategies_sdk.validator import ValidationReport, validate_strategy

__all__ = ["ValidationReport", "validate_strategy"]
