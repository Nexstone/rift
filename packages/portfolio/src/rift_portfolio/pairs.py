"""BTC/ETH basis spread (pairs trading) backtester.

Trades the ratio between two assets. When the ratio deviates from
its rolling mean by more than N standard deviations, enter a
spread trade (long the undervalued, short the overvalued).
Exit when the ratio reverts toward the mean.

This is delta-neutral — net market exposure is near zero.
Profit comes from the relationship, not the direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import polars as pl


@dataclass
class SpreadTrade:
    """A completed spread trade."""
    entry_time: int
    exit_time: int
    direction: str  # "long_ratio" (long A / short B) or "short_ratio" (short A / long B)
    entry_ratio: float
    exit_ratio: float
    entry_zscore: float
    exit_zscore: float
    size_a: float  # position size in asset A
    size_b: float  # position size in asset B
    pnl: float
    pnl_pct: float


@dataclass
class PairsResult:
    """Results from a pairs trading backtest."""
    asset_a: str
    asset_b: str
    interval: str
    initial_equity: float
    final_equity: float
    total_return_pct: float
    num_trades: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    profit_factor: float
    total_funding: float
    avg_hold_candles: float
    equity_curve: list[float] = field(default_factory=list)
    trades: list[SpreadTrade] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "asset_a": self.asset_a,
            "asset_b": self.asset_b,
            "interval": self.interval,
            "initial_equity": self.initial_equity,
            "final_equity": round(self.final_equity, 2),
            "total_return_pct": round(self.total_return_pct, 2),
            "num_trades": self.num_trades,
            "win_rate": round(self.win_rate, 2),
            "avg_win_pct": round(self.avg_win_pct, 2),
            "avg_loss_pct": round(self.avg_loss_pct, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "profit_factor": round(self.profit_factor, 2),
            "total_funding": round(self.total_funding, 2),
            "avg_hold_candles": round(self.avg_hold_candles, 1),
        }

    def to_export_dict(self) -> dict:
        d = self.to_dict()
        d["trades"] = [
            {
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "direction": t.direction,
                "entry_ratio": round(t.entry_ratio, 4),
                "exit_ratio": round(t.exit_ratio, 4),
                "entry_zscore": round(t.entry_zscore, 4),
                "exit_zscore": round(t.exit_zscore, 4),
                "pnl": round(t.pnl, 2),
                "pnl_pct": round(t.pnl_pct, 2),
            }
            for t in self.trades
        ]
        d["equity_curve"] = [round(e, 2) for e in self.equity_curve]
        return d


def run_pairs_backtest(
    df_a: pl.DataFrame,
    df_b: pl.DataFrame,
    asset_a: str = "BTC",
    asset_b: str = "ETH",
    interval: str = "1h",
    initial_equity: float = 10000.0,
    lookback: int = 168,        # 7 days rolling window for z-score
    entry_zscore: float = 2.0,  # enter when |z| > this
    exit_zscore: float = 0.5,   # exit when |z| < this
    stop_zscore: float = 4.0,   # stop loss at extreme z-score
    max_hold_candles: int = 72,  # max 3 days
    fee_rate: float = 0.00035,  # per side per asset
    slippage_pct: float = 0.0005,
    funding_a: pl.DataFrame | None = None,
    funding_b: pl.DataFrame | None = None,
    on_progress: callable = None,
) -> PairsResult:
    """Run a pairs trading backtest on two assets.

    Trades the ratio A/B. When the z-score of the ratio exceeds the
    entry threshold, enters a spread trade. Exits when z-score reverts.

    - Z > entry_zscore: ratio is high → short A, long B (expect ratio to fall)
    - Z < -entry_zscore: ratio is low → long A, short B (expect ratio to rise)
    """
    # Merge on timestamp
    merged = df_a.join(df_b, on="timestamp", suffix="_b")
    if len(merged) < lookback + 10:
        raise ValueError(f"Not enough overlapping data. Got {len(merged)} candles, need at least {lookback + 10}.")

    timestamps = merged["timestamp"].to_numpy()
    closes_a = merged["close"].to_numpy().astype(float)
    closes_b = merged["close_b"].to_numpy().astype(float)
    n = len(closes_a)

    # Build funding rate maps
    funding_map_a: dict[int, float] = {}
    funding_map_b: dict[int, float] = {}
    if funding_a is not None and len(funding_a) > 0:
        for ts, rate in zip(funding_a["timestamp"].to_numpy(), funding_a["funding_rate"].to_numpy()):
            funding_map_a[int(ts) // 3600000 * 3600000] = float(rate)
    if funding_b is not None and len(funding_b) > 0:
        for ts, rate in zip(funding_b["timestamp"].to_numpy(), funding_b["funding_rate"].to_numpy()):
            funding_map_b[int(ts) // 3600000 * 3600000] = float(rate)

    # Compute ratio and rolling z-score
    ratio = closes_a / closes_b
    zscore = np.full(n, 0.0)

    for i in range(lookback, n):
        window = ratio[i - lookback:i]
        mean = np.mean(window)
        std = np.std(window)
        if std > 0:
            zscore[i] = (ratio[i] - mean) / std

    # Simulate
    equity = initial_equity
    position_active = False
    direction = ""  # "short_ratio" or "long_ratio"
    size_a = 0.0
    size_b = 0.0
    entry_price_a = 0.0
    entry_price_b = 0.0
    entry_ratio_val = 0.0
    entry_zscore_val = 0.0
    entry_time = 0
    hold_count = 0
    total_funding = 0.0
    last_funding_hour = 0

    equity_curve = [equity]
    trades: list[SpreadTrade] = []

    for i in range(lookback, n):
        price_a = closes_a[i]
        price_b = closes_b[i]
        z = zscore[i]
        ts = int(timestamps[i])

        if on_progress and i % max(1, n // 20) == 0:
            pct = int((i - lookback) / (n - lookback) * 100)
            on_progress(pct, f"Candle {i}/{n}, equity: ${equity:.2f}")

        # Apply funding to open positions
        if position_active:
            current_hour = ts // 3600000 * 3600000
            if current_hour > last_funding_hour:
                # Funding on asset A position
                rate_a = funding_map_a.get(current_hour, 0.0)
                if size_a != 0 and rate_a != 0:
                    pos_val_a = abs(size_a) * price_a
                    if size_a > 0:  # long A
                        funding_a_pay = -pos_val_a * rate_a
                    else:  # short A
                        funding_a_pay = pos_val_a * rate_a
                    equity += funding_a_pay
                    total_funding += funding_a_pay

                # Funding on asset B position
                rate_b = funding_map_b.get(current_hour, 0.0)
                if size_b != 0 and rate_b != 0:
                    pos_val_b = abs(size_b) * price_b
                    if size_b > 0:  # long B
                        funding_b_pay = -pos_val_b * rate_b
                    else:  # short B
                        funding_b_pay = pos_val_b * rate_b
                    equity += funding_b_pay
                    total_funding += funding_b_pay

                last_funding_hour = current_hour

            hold_count += 1

        # Exit conditions
        if position_active:
            should_exit = False
            exit_reason = ""

            # Z-score reverted
            if direction == "short_ratio" and z < exit_zscore:
                should_exit = True
                exit_reason = "revert"
            elif direction == "long_ratio" and z > -exit_zscore:
                should_exit = True
                exit_reason = "revert"

            # Stop loss — z-score went further against us
            if direction == "short_ratio" and z > stop_zscore:
                should_exit = True
                exit_reason = "stop"
            elif direction == "long_ratio" and z < -stop_zscore:
                should_exit = True
                exit_reason = "stop"

            # Max hold time
            if hold_count >= max_hold_candles:
                should_exit = True
                exit_reason = "timeout"

            if should_exit:
                # Close both legs
                # P&L on asset A
                if size_a > 0:  # was long A
                    exit_a = price_a * (1 - slippage_pct)  # sell at worse price
                    pnl_a = size_a * (exit_a - entry_price_a) - abs(size_a * exit_a * fee_rate)
                else:  # was short A
                    exit_a = price_a * (1 + slippage_pct)  # buy back at worse price
                    pnl_a = abs(size_a) * (entry_price_a - exit_a) - abs(size_a * exit_a * fee_rate)

                # P&L on asset B
                if size_b > 0:  # was long B
                    exit_b = price_b * (1 - slippage_pct)
                    pnl_b = size_b * (exit_b - entry_price_b) - abs(size_b * exit_b * fee_rate)
                else:  # was short B
                    exit_b = price_b * (1 + slippage_pct)
                    pnl_b = abs(size_b) * (entry_price_b - exit_b) - abs(size_b * exit_b * fee_rate)

                total_pnl = pnl_a + pnl_b
                pnl_pct = (total_pnl / equity) * 100 if equity > 0 else 0
                equity += total_pnl

                trades.append(SpreadTrade(
                    entry_time=entry_time,
                    exit_time=ts,
                    direction=direction,
                    entry_ratio=entry_ratio_val,
                    exit_ratio=ratio[i],
                    entry_zscore=entry_zscore_val,
                    exit_zscore=z,
                    size_a=size_a,
                    size_b=size_b,
                    pnl=total_pnl,
                    pnl_pct=pnl_pct,
                ))

                position_active = False
                size_a = 0.0
                size_b = 0.0
                hold_count = 0

        # Entry conditions
        if not position_active and abs(z) > entry_zscore:
            # Position sizing: risk 2% of equity, split equally between legs
            risk_amount = equity * 0.02
            half_capital = equity * 0.5  # half on each leg

            if z > entry_zscore:
                # Ratio is high → short A, long B (expect ratio to fall)
                direction = "short_ratio"
                entry_price_a = price_a * (1 - slippage_pct)  # short A: selling, worse = lower price
                entry_price_b = price_b * (1 + slippage_pct)  # long B: buying, worse = higher price
                size_a = -(half_capital / entry_price_a)  # short
                size_b = half_capital / entry_price_b      # long

            else:
                # Ratio is low → long A, short B (expect ratio to rise)
                direction = "long_ratio"
                entry_price_a = price_a * (1 + slippage_pct)  # long A: buying, worse = higher price
                entry_price_b = price_b * (1 - slippage_pct)  # short B: selling, worse = lower price
                size_a = half_capital / entry_price_a      # long
                size_b = -(half_capital / entry_price_b)   # short

            # Entry fees
            fee_a = abs(size_a) * entry_price_a * fee_rate
            fee_b = abs(size_b) * entry_price_b * fee_rate
            equity -= (fee_a + fee_b)

            entry_ratio_val = ratio[i]
            entry_zscore_val = z
            entry_time = ts
            position_active = True
            hold_count = 0

        equity_curve.append(equity)

    # Close any remaining position at last price
    if position_active:
        price_a = closes_a[-1]
        price_b = closes_b[-1]

        if size_a > 0:
            pnl_a = size_a * (price_a - entry_price_a) - abs(size_a * price_a * fee_rate)
        else:
            pnl_a = abs(size_a) * (entry_price_a - price_a) - abs(size_a * price_a * fee_rate)

        if size_b > 0:
            pnl_b = size_b * (price_b - entry_price_b) - abs(size_b * price_b * fee_rate)
        else:
            pnl_b = abs(size_b) * (entry_price_b - price_b) - abs(size_b * price_b * fee_rate)

        total_pnl = pnl_a + pnl_b
        pnl_pct = (total_pnl / equity) * 100 if equity > 0 else 0
        equity += total_pnl

        trades.append(SpreadTrade(
            entry_time=entry_time, exit_time=int(timestamps[-1]),
            direction=direction, entry_ratio=entry_ratio_val, exit_ratio=ratio[-1],
            entry_zscore=entry_zscore_val, exit_zscore=zscore[-1],
            size_a=size_a, size_b=size_b, pnl=total_pnl, pnl_pct=pnl_pct,
        ))

    # Compute metrics
    eq_arr = np.array(equity_curve)
    returns = np.diff(eq_arr) / eq_arr[:-1]
    returns = returns[~np.isnan(returns)]

    winning = [t for t in trades if t.pnl > 0]
    losing = [t for t in trades if t.pnl <= 0]

    win_rate = len(winning) / len(trades) * 100 if trades else 0
    avg_win = float(np.mean([t.pnl_pct for t in winning])) if winning else 0
    avg_loss = float(np.mean([t.pnl_pct for t in losing])) if losing else 0

    peak = np.maximum.accumulate(eq_arr)
    drawdown = (eq_arr - peak) / peak
    max_dd = float(np.min(drawdown)) * 100 if len(drawdown) > 0 else 0

    sharpe = 0.0
    if len(returns) > 1 and np.std(returns) > 0:
        from rift_engine.backtest import _periods_per_year
        periods = _periods_per_year(interval)
        sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(periods))

    gross_profit = sum(t.pnl for t in winning) if winning else 0
    gross_loss = abs(sum(t.pnl for t in losing)) if losing else 1
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

    total_return = (equity - initial_equity) / initial_equity * 100

    avg_hold = float(np.mean([
        (t.exit_time - t.entry_time) / (3600 * 1000)
        for t in trades
    ])) if trades else 0

    if on_progress:
        on_progress(100, "Pairs backtest complete")

    return PairsResult(
        asset_a=asset_a,
        asset_b=asset_b,
        interval=interval,
        initial_equity=initial_equity,
        final_equity=equity,
        total_return_pct=total_return,
        num_trades=len(trades),
        win_rate=win_rate,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        max_drawdown_pct=max_dd,
        sharpe_ratio=sharpe,
        profit_factor=profit_factor,
        total_funding=total_funding,
        avg_hold_candles=avg_hold,
        equity_curve=equity_curve,
        trades=trades,
    )
