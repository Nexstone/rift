"""Performance reports and volatility forecasting.

Tearsheets are built on rift_substrate.stats — bootstrap CIs on every metric,
PSR and (optional) DSR for statistical significance, frequency-agnostic
annualization via periods_per_year_for_interval(). No retail tearsheet
library; everything is auditable substrate math underneath.

GARCH forecasting via `arch` for forward-looking vol prediction.

Includes live session and portfolio report generators.

Usage:
    from rift_research.reports import generate_tearsheet, forecast_volatility
    from rift_research.reports import generate_live_report, generate_portfolio_report

    # Generate Markdown tearsheet with bootstrap CIs + PSR/DSR
    generate_tearsheet(equity_curve, "trend_follow", interval="4h")

    # Forecast next-period volatility
    vol = forecast_volatility(close_prices)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np

from rift_substrate import periods_per_year_for_interval
from rift_substrate.stats import (
    Stats,
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
)


def _drawdown_analysis(equity: np.ndarray) -> dict:
    """Compute drawdown metrics from an equity curve.

    Returns:
      max_dd:               worst peak-to-trough drawdown (negative fraction)
      max_dd_start_idx:     index where the peak was set
      max_dd_trough_idx:    index of the deepest trough
      max_dd_duration:      length of the deepest drawdown (peak to trough)
      time_in_drawdown:     fraction of observations spent in any drawdown
      n_dd_over_5pct:       count of distinct drawdowns deeper than 5%
    """
    eq = np.asarray(equity, dtype=np.float64)
    if eq.size < 2:
        return {"max_dd": 0.0, "max_dd_start_idx": 0, "max_dd_trough_idx": 0,
                "max_dd_duration": 0, "time_in_drawdown": 0.0, "n_dd_over_5pct": 0}

    running_max = np.maximum.accumulate(eq)
    dd_series = (eq - running_max) / running_max
    max_dd = float(dd_series.min())
    trough_idx = int(dd_series.argmin())
    # Peak that preceded the trough
    peak_idx = int(running_max[:trough_idx + 1].argmax()) if trough_idx > 0 else 0
    dd_duration = trough_idx - peak_idx

    time_in_dd = float((dd_series < 0).sum() / len(dd_series))

    # Count distinct drawdowns >5%
    in_dd = False
    deepest_in_run = 0.0
    n_deep = 0
    for d in dd_series:
        if d < 0:
            if not in_dd:
                in_dd = True
                deepest_in_run = d
            else:
                deepest_in_run = min(deepest_in_run, d)
        else:
            if in_dd and deepest_in_run < -0.05:
                n_deep += 1
            in_dd = False
            deepest_in_run = 0.0
    if in_dd and deepest_in_run < -0.05:
        n_deep += 1

    return {
        "max_dd": max_dd,
        "max_dd_start_idx": peak_idx,
        "max_dd_trough_idx": trough_idx,
        "max_dd_duration": dd_duration,
        "time_in_drawdown": time_in_dd,
        "n_dd_over_5pct": n_deep,
    }


def _trade_stats(trade_returns: list[float]) -> dict:
    """Win rate, profit factor, avg win/loss from per-trade returns."""
    r = np.asarray(trade_returns, dtype=np.float64)
    r = r[np.isfinite(r)]
    if r.size == 0:
        return {"n": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0, "profit_factor": 0.0,
                "best": 0.0, "worst": 0.0}
    wins = r[r > 0]
    losses = r[r < 0]
    avg_win = float(wins.mean()) if wins.size else 0.0
    avg_loss = float(losses.mean()) if losses.size else 0.0
    profit_factor = float(wins.sum() / -losses.sum()) if losses.size and losses.sum() < 0 else float("inf") if wins.size else 0.0
    return {
        "n": int(r.size),
        "wins": int(wins.size),
        "losses": int(losses.size),
        "win_rate": float(wins.size / r.size),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "best": float(r.max()),
        "worst": float(r.min()),
    }


def _render_tearsheet_markdown(
    strategy_name: str,
    bundle,
    psr_at_zero: float,
    dsr: float | None,
    n_trials: int,
    dd_info: dict,
    trade_info: dict | None,
    interval: str,
) -> str:
    """Render a tearsheet in Markdown."""
    lines: list[str] = []
    lines.append(f"# RIFT Tearsheet — {strategy_name}")
    lines.append("")
    lines.append(f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}_  ")
    lines.append(f"_Interval: `{interval}` · Periods/year: {bundle.periods_per_year:g} · Observations: {bundle.n_observations}_")
    lines.append("")

    lines.append("## Performance Metrics")
    lines.append("")
    lines.append("| Metric          | Estimate    | 95% CI                         |")
    lines.append("|-----------------|-------------|--------------------------------|")
    lines.append(f"| Annual return   | {bundle.annual_return:+.2%}     | [{bundle.annual_return_ci_95[0]:+.2%}, {bundle.annual_return_ci_95[1]:+.2%}] |")
    lines.append(f"| Annual vol      | {bundle.annual_vol:.2%}      | [{bundle.annual_vol_ci_95[0]:.2%}, {bundle.annual_vol_ci_95[1]:.2%}] |")
    lines.append(f"| Sharpe ratio    | {bundle.sharpe:+.3f}     | [{bundle.sharpe_ci_95[0]:+.3f}, {bundle.sharpe_ci_95[1]:+.3f}]   |")
    lines.append(f"| Sortino ratio   | {bundle.sortino:+.3f}     | [{bundle.sortino_ci_95[0]:+.3f}, {bundle.sortino_ci_95[1]:+.3f}]   |")
    lines.append(f"| Calmar ratio    | {bundle.calmar:+.3f}     | _(no CI)_                      |")
    lines.append(f"| Max drawdown    | {bundle.max_drawdown:.2%}    | [{bundle.max_drawdown_ci_95[0]:.2%}, {bundle.max_drawdown_ci_95[1]:.2%}] |")
    lines.append("")
    lines.append("CIs computed via stationary block bootstrap (Politis & Romano 1994).")
    lines.append("")

    lines.append("## Statistical Significance")
    lines.append("")
    lines.append("| Test                                             | Value                       |")
    lines.append("|--------------------------------------------------|-----------------------------|")
    lines.append(f"| PSR — P(true Sharpe > 0 \\| observed)             | {psr_at_zero:.4f} ({psr_at_zero:.1%}) |")
    if dsr is not None:
        lines.append(f"| DSR — deflated for {n_trials} trial candidates           | {dsr:.4f} ({dsr:.1%})  |")
    else:
        lines.append(f"| DSR — deflated Sharpe                            | _not applicable (n_trials=1)_ |")
    lines.append(f"| Skewness                                         | {bundle.skew:+.3f}                  |")
    lines.append(f"| Kurtosis (Pearson; normal = 3)                   | {bundle.kurtosis:+.3f}                  |")
    lines.append(f"| Excess kurtosis                                  | {bundle.kurtosis - 3:+.3f}                  |")
    lines.append(f"| Autocorrelation lag-1                            | {bundle.autocorr_lag1:+.3f}                  |")
    lines.append("")
    lines.append("PSR via Bailey & López de Prado (2012). DSR via Bailey & López de Prado (2014).")
    lines.append("")

    lines.append("## Drawdown Analysis")
    lines.append("")
    lines.append("| Metric                       | Value         |")
    lines.append("|------------------------------|---------------|")
    lines.append(f"| Worst drawdown               | {dd_info['max_dd']:.2%}        |")
    lines.append(f"| Drawdown duration (periods)  | {dd_info['max_dd_duration']}             |")
    lines.append(f"| Time spent in any drawdown   | {dd_info['time_in_drawdown']:.1%}         |")
    lines.append(f"| Distinct drawdowns > 5%      | {dd_info['n_dd_over_5pct']}             |")
    lines.append("")

    if trade_info and trade_info["n"] > 0:
        lines.append("## Trade-Level Stats")
        lines.append("")
        lines.append("| Metric             | Value           |")
        lines.append("|--------------------|-----------------|")
        lines.append(f"| Total trades       | {trade_info['n']}              |")
        lines.append(f"| Winners / losers   | {trade_info['wins']} / {trade_info['losses']}            |")
        lines.append(f"| Win rate           | {trade_info['win_rate']:.1%}            |")
        lines.append(f"| Avg win            | {trade_info['avg_win']:+.4f}        |")
        lines.append(f"| Avg loss           | {trade_info['avg_loss']:+.4f}        |")
        pf = trade_info["profit_factor"]
        pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
        lines.append(f"| Profit factor      | {pf_s}            |")
        lines.append(f"| Best trade         | {trade_info['best']:+.4f}        |")
        lines.append(f"| Worst trade        | {trade_info['worst']:+.4f}        |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("_RIFT — Quant Trading Infrastructure for Humans and AI. Published by Nexstone._")
    return "\n".join(lines) + "\n"


def generate_tearsheet(
    equity_curve: list[float],
    strategy_name: str = "Strategy",
    output_dir: str | Path = "",
    interval: str = "1h",
    n_trials: int = 1,
    variance_of_trial_sharpes: float = 0.0,
    trade_returns: list[float] | None = None,
    seed: int = 42,
) -> str:
    """Generate a tearsheet using rift_substrate.stats — bootstrap CIs, PSR, DSR.

    Output is Markdown — human-readable in any editor, easy to grep/diff,
    convertible to HTML by standard tools. No retail tearsheet library;
    every metric is bootstrap-confidence-bounded substrate math.

    Args:
      equity_curve:               list of equity values over time
      strategy_name:              label for the report
      output_dir:                 where to save (default ~/.rift/reports)
      interval:                   candle interval, used for annualization (e.g., "1h", "5m", "1d")
      n_trials:                   if > 1, DSR corrects for selection bias from this many sweep candidates
      variance_of_trial_sharpes:  sample variance of Sharpes across the n_trials (only used if n_trials > 1)
      trade_returns:              optional per-trade returns for trade-level stats
      seed:                       RNG seed for bootstrap reproducibility

    Returns:
      Absolute path to the saved `.md` file. Empty string if input too short.
    """
    if len(equity_curve) < 10:
        return ""

    eq = np.array(equity_curve, dtype=np.float64)
    returns = np.diff(eq) / eq[:-1]
    returns = returns[np.isfinite(returns)]

    if len(returns) < 2:
        return ""

    periods_per_year = periods_per_year_for_interval(interval)

    # Bootstrap CIs via substrate
    bundle = Stats.from_returns(returns, periods_per_year=periods_per_year, seed=seed)

    # PSR — PSR takes PER-PERIOD Sharpe, MetricBundle.sharpe is annualized
    per_period_sharpe = bundle.sharpe / np.sqrt(periods_per_year) if periods_per_year > 0 else 0.0
    psr_at_zero = probabilistic_sharpe_ratio(
        observed_sharpe=per_period_sharpe,
        n_observations=bundle.n_observations,
        threshold=0.0,
        skew=bundle.skew,
        kurtosis=bundle.kurtosis,
    )

    # DSR — only meaningful when correcting for selection bias across multiple trials
    dsr = None
    if n_trials > 1:
        dsr = deflated_sharpe_ratio(
            observed_sharpe=per_period_sharpe,
            n_observations=bundle.n_observations,
            n_trials=n_trials,
            variance_of_trial_sharpes=variance_of_trial_sharpes,
            skew=bundle.skew,
            kurtosis=bundle.kurtosis,
        )

    # Drawdown analysis
    dd_info = _drawdown_analysis(eq)

    # Trade-level stats (optional)
    trade_info = _trade_stats(trade_returns) if trade_returns else None

    # Render
    md = _render_tearsheet_markdown(
        strategy_name=strategy_name,
        bundle=bundle,
        psr_at_zero=psr_at_zero,
        dsr=dsr,
        n_trials=n_trials,
        dd_info=dd_info,
        trade_info=trade_info,
        interval=interval,
    )

    # Save
    if not output_dir:
        output_dir = Path.home() / ".rift" / "reports"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{strategy_name.replace(' ', '_')}_tearsheet.md"
    output_path = output_dir / filename
    output_path.write_text(md)
    return str(output_path)


def forecast_volatility(
    close_prices: list[float] | np.ndarray,
    horizon: int = 1,
    model_type: str = "GARCH",
    periods_per_year: float = 8760.0,
) -> dict:
    """Forecast future volatility using GARCH model.

    GARCH (Generalized Autoregressive Conditional Heteroskedasticity)
    predicts FUTURE volatility from historical patterns. Unlike ATR which
    just measures past vol, GARCH models volatility clustering — high vol
    tends to follow high vol, low vol follows low vol.

    Args:
        close_prices: Array of closing prices
        horizon: Forecast horizon (number of periods ahead)
        model_type: "GARCH" (standard) or "EGARCH" (exponential, captures leverage effect)
        periods_per_year: Annualization factor for the input series. Use
            rift_substrate.periods_per_year_for_interval(interval) to derive
            from a TF string (e.g., "1h" → 8760). Defaults to 8760 (hourly).

    Returns:
        {
            "current_vol": float,      # current annualized volatility
            "forecast_vol": float,     # predicted next-period volatility
            "vol_expanding": bool,     # True if forecast > current
            "vol_ratio": float,        # forecast / current (>1 = expanding)
            "confidence": float,       # model fit quality (0-1)
        }
    """
    try:
        from arch import arch_model
    except ImportError:
        return {
            "current_vol": 0.0, "forecast_vol": 0.0,
            "vol_expanding": False, "vol_ratio": 1.0, "confidence": 0.0,
        }

    prices = np.array(close_prices)
    if len(prices) < 100:
        return {
            "current_vol": 0.0, "forecast_vol": 0.0,
            "vol_expanding": False, "vol_ratio": 1.0, "confidence": 0.0,
        }

    # Compute log returns (percentage * 100 for numerical stability)
    returns = np.diff(np.log(prices)) * 100
    returns = returns[np.isfinite(returns)]

    if len(returns) < 50:
        return {
            "current_vol": 0.0, "forecast_vol": 0.0,
            "vol_expanding": False, "vol_ratio": 1.0, "confidence": 0.0,
        }

    try:
        # Fit GARCH(1,1) model
        if model_type == "EGARCH":
            model = arch_model(returns, vol="EGARCH", p=1, q=1, mean="Zero", rescale=False)
        else:
            model = arch_model(returns, vol="Garch", p=1, q=1, mean="Zero", rescale=False)

        result = model.fit(disp="off", show_warning=False)

        # Current conditional volatility
        cond_vol = result.conditional_volatility
        current_vol = float(cond_vol[-1]) if len(cond_vol) > 0 else 0.0

        # Forecast
        forecast = result.forecast(horizon=horizon)
        forecast_var = forecast.variance.values[-1, 0]
        forecast_vol = float(np.sqrt(forecast_var))

        # Annualize using caller-supplied periods_per_year (default 8760 = hourly)
        annualize_factor = np.sqrt(periods_per_year)
        current_annual = current_vol * annualize_factor / 100
        forecast_annual = forecast_vol * annualize_factor / 100

        vol_ratio = forecast_vol / current_vol if current_vol > 0 else 1.0

        # Model fit quality (pseudo R-squared from log-likelihood)
        confidence = min(1.0, max(0.0, 1 - abs(result.aic) / (abs(result.aic) + 1000)))

        return {
            "current_vol": round(current_annual, 4),
            "forecast_vol": round(forecast_annual, 4),
            "vol_expanding": vol_ratio > 1.05,
            "vol_ratio": round(vol_ratio, 4),
            "confidence": round(confidence, 2),
        }

    except Exception:
        # GARCH failed to converge — fall back to simple vol
        current_vol = float(np.std(returns[-20:])) * np.sqrt(periods_per_year) / 100
        return {
            "current_vol": round(current_vol, 4),
            "forecast_vol": round(current_vol, 4),
            "vol_expanding": False,
            "vol_ratio": 1.0,
            "confidence": 0.0,
        }


# ─── LIVE SESSION REPORTS ─────────────────────────────────────

def generate_live_report(
    session_log_path: str = "",
    output_dir: str | Path = "",
) -> str:
    """Generate an HTML report from a completed live trading session.

    Includes: TCA summary, PnL attribution, trade log, and a substrate.stats
    tearsheet link with bootstrap CIs and PSR. Returns path to the HTML file.
    """
    from rift_engine.tca import analyze_trades
    from rift_engine.attribution import attribute_pnl

    # Find session log
    if session_log_path:
        log_path = Path(session_log_path)
    else:
        # Use most recent session log
        sessions_dir = Path.home() / ".rift" / "algo_sessions"
        if not sessions_dir.exists():
            return ""
        logs = sorted(sessions_dir.glob("ALGO_*.json"))
        if not logs:
            return ""
        log_path = logs[-1]

    if not log_path.exists():
        return ""

    data = json.loads(log_path.read_text())
    trades = data.get("trades", [])
    strategy = data.get("strategy", "unknown")
    pair = data.get("pair", "unknown")
    initial_equity = data.get("initial_equity", 10000)
    final_equity = data.get("final_equity", initial_equity)

    if not trades:
        return ""

    # Build equity curve from trades
    equity_curve = [initial_equity]
    eq = initial_equity
    for t in trades:
        eq += t.get("pnl", 0)
        equity_curve.append(eq)

    # Output directory
    if not output_dir:
        output_dir = Path.home() / ".rift" / "reports"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"ALGO_{strategy}_{pair}_{timestamp}.html"
    output_path = output_dir / filename

    # Run TCA
    tca = analyze_trades(trades)

    # Run attribution
    attr = attribute_pnl(trades, initial_equity=initial_equity)

    # Generate HTML
    html_parts: list[str] = []
    html_parts.append(_html_header(f"RIFT Live Report — {strategy} {pair}"))

    # Executive summary
    total_pnl = final_equity - initial_equity
    total_pnl_pct = (total_pnl / initial_equity * 100) if initial_equity > 0 else 0
    num_trades = len(trades)
    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    win_rate = (wins / num_trades * 100) if num_trades > 0 else 0

    html_parts.append(f"""
    <div class="section">
        <h2>Executive Summary</h2>
        <div class="metrics-grid">
            <div class="metric"><span class="label">Strategy</span><span class="value">{strategy}</span></div>
            <div class="metric"><span class="label">Pair</span><span class="value">{pair}</span></div>
            <div class="metric"><span class="label">Initial Equity</span><span class="value">${initial_equity:,.2f}</span></div>
            <div class="metric"><span class="label">Final Equity</span><span class="value">${final_equity:,.2f}</span></div>
            <div class="metric"><span class="label">Total P&L</span><span class="value {'positive' if total_pnl >= 0 else 'negative'}">${total_pnl:+,.2f} ({total_pnl_pct:+.2f}%)</span></div>
            <div class="metric"><span class="label">Trades</span><span class="value">{num_trades}</span></div>
            <div class="metric"><span class="label">Win Rate</span><span class="value">{win_rate:.1f}%</span></div>
            <div class="metric"><span class="label">Period</span><span class="value">{data.get('started_at', '')} — {data.get('ended_at', '')}</span></div>
        </div>
    </div>
    """)

    # PnL Attribution waterfall
    html_parts.append(f"""
    <div class="section">
        <h2>P&L Attribution</h2>
        <table class="data-table">
            <tr><th>Component</th><th>Amount</th><th>% of Total</th></tr>
            <tr><td>Alpha (strategy edge)</td><td class="{'positive' if attr.alpha_pnl >= 0 else 'negative'}">${attr.alpha_pnl:+,.2f}</td><td>{attr.alpha_pct:+.1f}%</td></tr>
            <tr><td>Beta (market exposure)</td><td class="{'positive' if attr.beta_pnl >= 0 else 'negative'}">${attr.beta_pnl:+,.2f}</td><td>{attr.beta_pct:+.1f}%</td></tr>
            <tr><td>Funding income</td><td class="{'positive' if attr.funding_pnl >= 0 else 'negative'}">${attr.funding_pnl:+,.2f}</td><td>{attr.funding_pct:+.1f}%</td></tr>
            <tr><td>Execution costs</td><td class="negative">${attr.execution_cost:,.2f}</td><td>{attr.execution_pct:.1f}%</td></tr>
            <tr class="total"><td><strong>Total P&L</strong></td><td><strong>${attr.total_pnl:+,.2f}</strong></td><td></td></tr>
        </table>
        <p class="note">Beta coefficient: {attr.beta_coefficient:.4f} | R²: {attr.r_squared:.4f} | Market return: {attr.market_return_pct:+.2f}%</p>
    </div>
    """)

    # TCA Summary
    html_parts.append(f"""
    <div class="section">
        <h2>Execution Quality (TCA)</h2>
        <div class="metrics-grid">
            <div class="metric"><span class="label">Grade</span><span class="value grade-{tca.grade.lower()}">{tca.grade}</span></div>
            <div class="metric"><span class="label">Score</span><span class="value">{tca.score}/100</span></div>
            <div class="metric"><span class="label">Avg Entry Slippage</span><span class="value">{tca.avg_entry_slippage_bps:.1f} bps</span></div>
            <div class="metric"><span class="label">Avg Exit Slippage</span><span class="value">{tca.avg_exit_slippage_bps:.1f} bps</span></div>
            <div class="metric"><span class="label">Total Slippage Cost</span><span class="value negative">${tca.total_slippage_cost:,.2f}</span></div>
            <div class="metric"><span class="label">Total Fee Cost</span><span class="value negative">${tca.total_fee_cost:,.2f}</span></div>
            <div class="metric"><span class="label">Total Execution Cost</span><span class="value negative">${tca.total_execution_cost:,.2f}</span></div>
        </div>
    """)
    if tca.twap_count > 0:
        html_parts.append(f"""
        <p class="note">IOC trades: {tca.ioc_count} (avg {tca.ioc_avg_slippage_bps:.1f} bps) | TWAP trades: {tca.twap_count} (avg {tca.twap_avg_slippage_bps:.1f} bps)</p>
        """)
    html_parts.append("</div>")

    # Trade log
    html_parts.append("""
    <div class="section">
        <h2>Trade Log</h2>
        <table class="data-table">
            <tr><th>Side</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Funding</th><th>Duration</th><th>Slippage</th><th>Method</th></tr>
    """)
    for t in trades:
        side = t.get("side", "").upper()
        pnl = t.get("pnl", 0)
        pnl_class = "positive" if pnl >= 0 else "negative"
        funding = t.get("funding", t.get("funding_collected", 0))
        slip = t.get("entry_slippage_bps", 0)
        html_parts.append(f"""
            <tr>
                <td>{side}</td>
                <td>${t.get('entry_price', 0):,.2f}</td>
                <td>${t.get('exit_price', 0):,.2f}</td>
                <td class="{pnl_class}">${pnl:+,.2f}</td>
                <td>${funding:+,.2f}</td>
                <td>{t.get('candles_held', 0)}c</td>
                <td>{slip:.1f} bps</td>
                <td>{t.get('execution_method', 'ioc')}</td>
            </tr>
        """)
    html_parts.append("</table></div>")

    # Substrate-based tearsheet (link from this report)
    try:
        ts_path = generate_tearsheet(
            equity_curve=equity_curve,
            strategy_name=f"{strategy}_{pair}",
            output_dir=output_dir,
            interval=data.get("interval", "1h"),
        )
        if ts_path:
            ts_filename = Path(ts_path).name
            html_parts.append(f"""
        <div class="section">
            <h2>Performance Tearsheet</h2>
            <p>Full bootstrap-confidence metrics (PSR, Sharpe CI, drawdown analysis): <a href="{ts_filename}">{ts_filename}</a></p>
        </div>
        """)
    except Exception:
        pass

    html_parts.append(_html_footer())
    output_path.write_text("\n".join(html_parts))

    return str(output_path)


def generate_portfolio_report(
    output_dir: str | Path = "",
    period: str = "all",
) -> str:
    """Generate a portfolio-level report combining all live sessions.

    Args:
        output_dir: Where to save the report
        period: "daily", "weekly", or "all"

    Returns path to the generated HTML file.
    """
    from rift_engine.tca import analyze_all_sessions
    from rift_engine.attribution import attribute_all_sessions
    from rift_trade.supervisor import get_supervisor_status

    if not output_dir:
        output_dir = Path.home() / ".rift" / "reports"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d")
    filename = f"portfolio_{period}_{timestamp}.html"
    output_path = output_dir / filename

    # Get supervisor state if running
    sup_state = get_supervisor_status()

    # Run aggregated TCA and attribution
    tca = analyze_all_sessions()
    attr = attribute_all_sessions()

    # Load all session logs
    sessions_dir = Path.home() / ".rift" / "live_sessions"
    sessions: list[dict] = []
    if sessions_dir.exists():
        for f in sorted(sessions_dir.glob("ALGO_*.json")):
            try:
                sessions.append(json.loads(f.read_text()))
            except Exception:
                pass

    # Load alerts
    alerts: list[dict] = []
    alerts_file = Path.home() / ".rift" / "algo" / "alerts.log"
    if alerts_file.exists():
        for line in alerts_file.read_text().strip().split("\n"):
            if line.strip():
                try:
                    alerts.append(json.loads(line))
                except Exception:
                    pass

    html_parts: list[str] = []
    html_parts.append(_html_header(f"RIFT Portfolio Report — {period.title()} — {timestamp}"))

    # Portfolio summary
    portfolio = sup_state.get("portfolio", {}) if sup_state else {}
    strategies = sup_state.get("strategies", []) if sup_state else []

    html_parts.append(f"""
    <div class="section">
        <h2>Portfolio Summary</h2>
        <div class="metrics-grid">
            <div class="metric"><span class="label">Total Equity</span><span class="value">${portfolio.get('total_equity', attr.final_equity):,.2f}</span></div>
            <div class="metric"><span class="label">Total P&L</span><span class="value {'positive' if attr.total_pnl >= 0 else 'negative'}">${attr.total_pnl:+,.2f}</span></div>
            <div class="metric"><span class="label">Total Trades</span><span class="value">{attr.num_trades}</span></div>
            <div class="metric"><span class="label">Net Exposure</span><span class="value">{portfolio.get('net_exposure', 0) * 100:.0f}%</span></div>
            <div class="metric"><span class="label">Gross Exposure</span><span class="value">{portfolio.get('gross_exposure', 0) * 100:.0f}%</span></div>
            <div class="metric"><span class="label">Drawdown</span><span class="value">{portfolio.get('drawdown_from_peak', 0) * 100:.1f}%</span></div>
            <div class="metric"><span class="label">Execution Grade</span><span class="value grade-{tca.grade.lower()}">{tca.grade} ({tca.score}/100)</span></div>
            <div class="metric"><span class="label">Sessions</span><span class="value">{len(sessions)}</span></div>
        </div>
    </div>
    """)

    # Strategy breakdown
    if strategies:
        html_parts.append("""
        <div class="section">
            <h2>Strategy Breakdown</h2>
            <table class="data-table">
                <tr><th>Strategy</th><th>Pair</th><th>Status</th><th>P&L %</th><th>Trades</th><th>Health</th><th>Allocation</th></tr>
        """)
        for s in strategies:
            pnl = s.get("pnl_pct", 0)
            pnl_class = "positive" if pnl >= 0 else "negative"
            html_parts.append(f"""
                <tr>
                    <td>{s.get('name', '')}</td>
                    <td>{s.get('pair', '')}</td>
                    <td>{s.get('status', '')}</td>
                    <td class="{pnl_class}">{pnl:+.2f}%</td>
                    <td>{s.get('num_trades', 0)}</td>
                    <td>{s.get('health_grade', '-')}</td>
                    <td>{(s.get('allocation', 0) * 100):.0f}%</td>
                </tr>
            """)
        html_parts.append("</table></div>")

    # Attribution
    html_parts.append(f"""
    <div class="section">
        <h2>P&L Attribution</h2>
        <table class="data-table">
            <tr><th>Component</th><th>Amount</th><th>%</th></tr>
            <tr><td>Alpha</td><td class="{'positive' if attr.alpha_pnl >= 0 else 'negative'}">${attr.alpha_pnl:+,.2f}</td><td>{attr.alpha_pct:+.1f}%</td></tr>
            <tr><td>Beta</td><td class="{'positive' if attr.beta_pnl >= 0 else 'negative'}">${attr.beta_pnl:+,.2f}</td><td>{attr.beta_pct:+.1f}%</td></tr>
            <tr><td>Funding</td><td class="{'positive' if attr.funding_pnl >= 0 else 'negative'}">${attr.funding_pnl:+,.2f}</td><td>{attr.funding_pct:+.1f}%</td></tr>
            <tr><td>Execution</td><td class="negative">${attr.execution_cost:,.2f}</td><td>{attr.execution_pct:.1f}%</td></tr>
            <tr class="total"><td><strong>Total</strong></td><td><strong>${attr.total_pnl:+,.2f}</strong></td><td></td></tr>
        </table>
    </div>
    """)

    # TCA
    html_parts.append(f"""
    <div class="section">
        <h2>Execution Quality</h2>
        <div class="metrics-grid">
            <div class="metric"><span class="label">Avg Slippage</span><span class="value">{tca.total_slippage_bps:.1f} bps</span></div>
            <div class="metric"><span class="label">Total Cost</span><span class="value negative">${tca.total_execution_cost:,.2f}</span></div>
            <div class="metric"><span class="label">Slippage</span><span class="value negative">${tca.total_slippage_cost:,.2f}</span></div>
            <div class="metric"><span class="label">Fees</span><span class="value negative">${tca.total_fee_cost:,.2f}</span></div>
        </div>
    </div>
    """)

    # Recent alerts
    if alerts:
        html_parts.append("""
        <div class="section">
            <h2>Recent Alerts</h2>
            <table class="data-table">
                <tr><th>Time</th><th>Event</th><th>Message</th></tr>
        """)
        for a in alerts[-20:]:
            html_parts.append(f"""
                <tr><td>{a.get('time', '')}</td><td>{a.get('event', '')}</td><td>{a.get('message', '')}</td></tr>
            """)
        html_parts.append("</table></div>")

    html_parts.append(_html_footer())
    output_path.write_text("\n".join(html_parts))

    return str(output_path)


# ─── HTML TEMPLATE ────────────────────────────────────────────

def _html_header(title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a0a; color: #e0e0e0; margin: 0; padding: 20px; }}
    h1 {{ color: #00e5ff; border-bottom: 1px solid #333; padding-bottom: 10px; }}
    h2 {{ color: #b0bec5; margin-top: 30px; }}
    .section {{ background: #111; border: 1px solid #222; border-radius: 8px; padding: 20px; margin: 20px 0; }}
    .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 15px; }}
    .metric {{ background: #1a1a1a; padding: 12px; border-radius: 6px; }}
    .metric .label {{ display: block; color: #888; font-size: 0.85em; margin-bottom: 4px; }}
    .metric .value {{ display: block; font-size: 1.2em; font-weight: 600; }}
    .positive {{ color: #4caf50; }}
    .negative {{ color: #f44336; }}
    .grade-a {{ color: #4caf50; font-size: 1.5em; }}
    .grade-b {{ color: #00bcd4; font-size: 1.5em; }}
    .grade-c {{ color: #ff9800; font-size: 1.5em; }}
    .grade-d, .grade-f {{ color: #f44336; font-size: 1.5em; }}
    .data-table {{ width: 100%; border-collapse: collapse; margin: 10px 0; }}
    .data-table th {{ text-align: left; padding: 8px; border-bottom: 2px solid #333; color: #888; font-weight: 500; }}
    .data-table td {{ padding: 8px; border-bottom: 1px solid #1a1a1a; }}
    .data-table .total td {{ border-top: 2px solid #333; font-weight: 600; }}
    .note {{ color: #666; font-size: 0.85em; margin-top: 10px; }}
    a {{ color: #00e5ff; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p style="color: #666;">Generated by RIFT — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
"""


def _html_footer() -> str:
    return """
<div style="margin-top: 40px; padding-top: 20px; border-top: 1px solid #222; color: #444; font-size: 0.8em;">
    Generated by RIFT (Nexstone Capital) — Research / Iteration / Forecast / Trade
</div>
</body>
</html>"""
