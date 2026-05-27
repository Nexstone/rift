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

export default class New extends GatedCommand {
  static override description = 'Scaffold a new trading strategy'

  static override examples = [
    '$ rift new my-strategy',
    '$ rift new bollinger-breakout',
  ]

  static override args = {
    name: Args.string({description: 'Strategy name (lowercase, hyphens ok)', required: true}),
  }

  async run(): Promise<void> {
    const {args} = await this.parse(New)

    const name = args.name.toLowerCase().replace(/[^a-z0-9_-]/g, '_')
    const className = name.split(/[-_]/).map(w => w.charAt(0).toUpperCase() + w.slice(1)).join('')
    const configName = `${className}Config`

    // Find strategies dir — look for the project root (where package.json + engine/ coexist)
    let strategiesDir = ''
    let dir = path.resolve(__dirname)
    for (let i = 0; i < 10; i++) {
      const hasEngine = fs.existsSync(path.join(dir, 'engine', 'pyproject.toml'))
      if (hasEngine) {
        strategiesDir = path.join(dir, 'strategies')
        break
      }
      dir = path.dirname(dir)
    }

    if (!strategiesDir) {
      strategiesDir = path.resolve('strategies')
    }

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
rift compare ${name},btc_funding_fade --pair BTC --tf 1h
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
}
