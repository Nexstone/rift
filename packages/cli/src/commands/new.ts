import {Args, Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import * as fs from 'node:fs'
import * as path from 'node:path'
import {fileURLToPath} from 'node:url'
import {runEngine} from '../lib/python-bridge.js'
import type {EngineMessage} from '../lib/python-bridge.js'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`

/**
 * Walk up from `start` to find the repo root. We identify the root by the
 * presence of `pnpm-workspace.yaml` — this file only exists at the top of
 * the monorepo. Walking up looking for `engine/pyproject.toml` (the prior
 * heuristic) was buggy because `packages/engine/pyproject.toml` also exists
 * and would match first, dropping scaffolded files into the wrong dir.
 */
function findRepoRoot(start: string): string {
  let dir = path.resolve(start)
  for (let i = 0; i < 12; i++) {
    if (fs.existsSync(path.join(dir, 'pnpm-workspace.yaml'))) return dir
    const parent = path.dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  // Fallback: cwd. Better than landing in the wrong subdirectory silently.
  return path.resolve('.')
}

export default class New extends GatedCommand {
  static override description = 'Scaffold a new trading strategy or scout signal'

  static override examples = [
    '$ rift new my-strategy',
    '$ rift new bollinger-breakout',
    '$ rift new my-momentum-signal --type signal',
  ]

  static override args = {
    name: Args.string({description: 'Name (lowercase, hyphens ok)', required: true}),
  }

  static override flags = {
    type: Flags.string({
      description: 'What to scaffold: "strategy" (default) or "signal"',
      options: ['strategy', 'signal'],
      default: 'strategy',
    }),
  }

  async run(): Promise<void> {
    const {args, flags} = await this.parse(New)

    if (flags.type === 'signal') {
      return this.scaffoldSignal(args.name)
    }
    return this.scaffoldStrategy(args.name)
  }

  // ── Strategy scaffold (existing behavior) ──────────────────────
  private async scaffoldStrategy(rawName: string): Promise<void> {

    const name = rawName.toLowerCase().replace(/[^a-z0-9_-]/g, '_')
    const className = name.split(/[-_]/).map(w => w.charAt(0).toUpperCase() + w.slice(1)).join('')
    const configName = `${className}Config`

    // Find repo root via `pnpm-workspace.yaml` (only present at the root —
    // walking up from packages/cli/dist/commands looking for engine/pyproject.toml
    // would otherwise match packages/engine/pyproject.toml first).
    const strategiesDir = path.join(findRepoRoot(__dirname), 'strategies')

    fs.mkdirSync(strategiesDir, {recursive: true})

    const stratDir = path.join(strategiesDir, name)

    if (fs.existsSync(stratDir)) {
      this.error(`Strategy "${name}" already exists at ${stratDir}`)
    }

    fs.mkdirSync(stratDir, {recursive: true})

    // strategy.py
    const strategyPy = `"""${className} strategy."""

from dataclasses import dataclass

from rift.strategy import EMA, RSI, Candle, Indicator, Signal, Strategy, StrategyState, register


@dataclass(frozen=True)
class ${configName}:
    ema_period: int = 20
    rsi_period: int = 14
    entry_threshold: float = 30.0
    exit_threshold: float = 70.0
    leverage: float = 1.0
    stop_loss_pct: float = 0.02


@register("${name}")
class ${className}(Strategy):
    """${className} — describe your strategy here."""

    config_class = ${configName}

    def on_candle(self, candle: Candle, state: StrategyState) -> Signal | None:
        ema = state.ema
        rsi = state.rsi

        if ema == 0 or rsi == 0:
            return None

        # Entry logic — customize this
        if state.position == 0 and rsi < self.config.entry_threshold and candle.close > ema:
            return Signal.long(size=self.position_size(), sl=self.config.stop_loss_pct)

        # Exit logic — customize this
        if state.position > 0 and rsi > self.config.exit_threshold:
            return Signal.close()

        return None

    def indicators(self) -> dict[str, Indicator]:
        return {
            "ema": EMA(self.config.ema_period),
            "rsi": RSI(self.config.rsi_period),
        }
`

    // config.yaml
    const configYaml = `# ${className} default configuration
strategy: ${name}

params:
  ema_period: 20
  rsi_period: 14
  entry_threshold: 30.0
  exit_threshold: 70.0
  leverage: 1.0
  stop_loss_pct: 0.02
`

    // sweep.yaml
    const sweepYaml = `# Parameter sweep configuration for ${className}
strategy: ${name}

sweep:
  ema_period: [10, 15, 20, 30, 50]
  rsi_period: [7, 14, 21]
  entry_threshold: [20.0, 25.0, 30.0, 35.0]
  exit_threshold: [60.0, 65.0, 70.0, 75.0]
  stop_loss_pct: [0.01, 0.02, 0.03]
`

    // README.md
    const readme = `# ${className}

## Description
Describe your strategy logic here.

## Parameters
| Parameter | Default | Description |
|-----------|---------|-------------|
| ema_period | 20 | EMA lookback period |
| rsi_period | 14 | RSI lookback period |
| entry_threshold | 30.0 | RSI level to enter |
| exit_threshold | 70.0 | RSI level to exit |
| leverage | 1.0 | Position leverage |
| stop_loss_pct | 0.02 | Stop loss percentage |

## Usage
\`\`\`bash
rift backtest ${name} --pair BTC --tf 1h
rift compare ${name},trend_follow --pair BTC --tf 4h
\`\`\`
`

    fs.writeFileSync(path.join(stratDir, 'strategy.py'), strategyPy)
    fs.writeFileSync(path.join(stratDir, 'config.yaml'), configYaml)
    fs.writeFileSync(path.join(stratDir, 'sweep.yaml'), sweepYaml)
    fs.writeFileSync(path.join(stratDir, 'README.md'), readme)

    this.log('')
    this.log(`  ${green('✔')} Strategy ${bold(name)} created at:`)
    this.log('')
    this.log(`    ${stratDir}/`)
    this.log(`    ├── strategy.py     ${dim('— your strategy code')}`)
    this.log(`    ├── config.yaml     ${dim('— default parameters')}`)
    this.log(`    ├── sweep.yaml      ${dim('— parameter ranges for optimization')}`)
    this.log(`    └── README.md       ${dim('— documentation')}`)
    this.log('')
    // Validate the scaffolded strategy
    const strategyFile = path.join(stratDir, 'strategy.py')
    this.log(`  ${dim('Validating...')}`)

    try {
      await runEngine('validate-strategy', [strategyFile], (msg: EngineMessage) => {
        if (msg.type === 'result') {
          if (msg.status === 'ok') {
            this.log(`  ${green('✔')} Strategy ${bold(String(msg.name))} registered with indicators: ${dim(String((msg.indicators as string[]).join(', ')))}`)
          } else if (msg.status === 'warn') {
            this.log(`  ${yellow('!')} Warning: ${msg.error || (msg.errors as string[]).join(', ')}`)
          } else {
            this.log(`  ${red('✘')} ${msg.error}`)
          }
        }
      })
    } catch {
      this.log(`  ${yellow('!')} Could not validate (Python engine issue). Strategy files are created.`)
    }

    this.log('')
    this.log(`  ${dim('Next steps:')}`)
    this.log(`    1. Edit ${bold('strategy.py')} with your trading logic`)
    this.log(`    2. Run: ${cyan('rift backtest ' + name + ' --pair BTC --tf 1h')}`)
    this.log(`    3. Optimize: ${cyan('rift sweep ' + name + ' --config sweep.yaml')}`)
    this.log('')

    function cyan(s: string) { return `\x1b[36m${s}\x1b[0m` }
    function yellow(s: string) { return `\x1b[33m${s}\x1b[0m` }
    function red(s: string) { return `\x1b[31m${s}\x1b[0m` }
  }

  // ── Signal scaffold (Custom-signal SDK) ───────────────────────
  private async scaffoldSignal(rawName: string): Promise<void> {
    const name = rawName.toLowerCase().replace(/[^a-z0-9_]/g, '_')

    const signalsDir = path.join(findRepoRoot(__dirname), 'strategies', 'signals')
    fs.mkdirSync(signalsDir, {recursive: true})

    const signalFile = path.join(signalsDir, `${name}.py`)
    if (fs.existsSync(signalFile)) {
      this.error(`Signal "${name}" already exists at ${signalFile}`)
    }

    const cyan = (s: string) => `\x1b[36m${s}\x1b[0m`

    // Signal scaffolds are intentionally single-file (no config.yaml, no
    // sweep.yaml). The signal IS the unit; users register one per file
    // and the file's @signal(...) decorator is the entire surface.
    const signalPy = `"""${name} — custom scout signal.

Register with @signal(...). \`rift scout\` discovers this file automatically
from strategies/signals/ (and ~/.rift/signals/) at scan time.

See docs/signals/AUTHORING.md for the full guide.
"""

from __future__ import annotations

from rift_strategies_sdk import signal, SignalResult


@signal(
    name="${name}",
    category="momentum",  # one of: funding, momentum, microstructure,
                          # volatility, cross_pair, seasonality
    description="${name} — describe what this signal detects",
    weight=1.0,           # higher = more influence in aggregation
)
def ${name}(coin: str, state: dict) -> SignalResult:
    """Return a SignalResult with score in [-1, +1].

    Args:
        coin: ticker symbol, e.g. "BTC"
        state: dict of available market state. Common keys:
          - mid_price, funding_rate, predicted_funding, open_interest
          - oracle_price, premium, atr_pct, day_volume
          - cvd, volume_delta, relative_volume
          - candles_1h, candles_5m (lists of {o,h,l,c,v,t})
          - indicators (dict of pre-computed indicator values)

    Returns:
        SignalResult with score=+1 (strong long), 0 (no opinion),
        or -1 (strong short). Score=0 is silently dropped by the
        aggregator, so always return a meaningful score when fired.
    """
    # ──────────────────────────────────────────────────────────────
    # Replace this with your detection logic.
    #
    # Example: extreme funding rate as a contrarian signal.
    # ──────────────────────────────────────────────────────────────
    funding = float(state.get("funding_rate") or 0.0)

    # Extreme positive funding → over-leveraged longs → fade
    if funding > 0.0005:
        return SignalResult(
            name="${name}",
            score=-0.5,
            reason=f"Extreme positive funding {funding * 100:.3f}% — fade longs",
            category="momentum",
            confidence=0.6,
        )

    # Extreme negative funding → over-leveraged shorts → fade
    if funding < -0.0005:
        return SignalResult(
            name="${name}",
            score=+0.5,
            reason=f"Extreme negative funding {funding * 100:.3f}% — fade shorts",
            category="momentum",
            confidence=0.6,
        )

    return SignalResult(
        name="${name}",
        score=0.0,
        reason="Funding within normal range",
        category="momentum",
        confidence=0.0,
    )
`

    fs.writeFileSync(signalFile, signalPy)

    this.log('')
    this.log(`  \x1b[32m✔\x1b[0m Signal ${bold(name)} created at:`)
    this.log('')
    this.log(`    ${signalFile}`)
    this.log('')
    this.log(`  ${dim('Next steps:')}`)
    this.log(`    1. Edit ${bold(name + '.py')} with your detection logic`)
    this.log(`    2. Run: ${cyan('rift scout --top 5 --min 1 --no-soak')} (your signal fires alongside the 9 built-ins)`)
    this.log(`    3. Use ${cyan('rift signal-stats')} to see how often it fires`)
    this.log('')
    this.log(`  ${dim('Place user-only signals at ~/.rift/signals/<name>.py (not committed to the repo).')}`)
    this.log('')
  }
}
