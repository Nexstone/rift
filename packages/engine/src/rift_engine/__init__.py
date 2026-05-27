"""RIFT engine — backtesting + research engine.

Depends on rift_core (schema, config) and rift_data (data loading).
Does NOT depend on rift_trade (live execution) — engine is read-only against
the chain. T3 actions live in rift_trade.

Public surface:
  strategy: Strategy base class, Signal, Side, Indicator, register
  backtest: run_backtest, Trade, BacktestResult
  walkforward: run_walkforward
  montecarlo: run_montecarlo
  sweep: run_sweep
  smart_optimize: smart_optimize
  signals: signal provider subpackage (momentum, volatility, microstructure, funding, ...)
"""
