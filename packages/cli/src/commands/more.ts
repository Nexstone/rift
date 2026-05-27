import {Args} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {runEngine} from '../lib/python-bridge.js'
import type {EngineMessage} from '../lib/python-bridge.js'

const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
const cyan = (s: string) => `\x1b[36m${s}\x1b[0m`
const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`
const red = (s: string) => `\x1b[31m${s}\x1b[0m`

/**
 * Catalog of every engine command, grouped by purpose. Hand-curated so we
 * can mark which are first-class `rift <cmd>` versus only reachable via
 * `rift more <cmd>`. A regression test verifies this stays in sync with
 * the engine's actual command list (see test_more_catalog_complete in
 * engine/tests/regression/).
 *
 * Each entry: {name, desc, promoted}
 *   promoted=true  → also exists as `rift <name>` (or close enough)
 *   promoted=false → only reachable via `rift more <name>` or `rift-engine`
 */
type CmdEntry = {name: string; desc: string; promoted: boolean}

const CATALOG: Record<string, CmdEntry[]> = {
  'Research & backtesting': [
    {name: 'backtest', desc: 'Run a backtest on cached candle data', promoted: true},
    {name: 'compare', desc: 'Compare multiple strategies head-to-head', promoted: true},
    {name: 'sweep', desc: 'Run a parameter sweep', promoted: true},
    {name: 'smart-sweep', desc: 'Bayesian-optimized parameter sweep', promoted: false},
    {name: 'montecarlo', desc: 'Monte Carlo simulation', promoted: true},
    {name: 'walk-forward', desc: 'Walk-forward validation', promoted: true},
    {name: 'research', desc: 'Full research pipeline (backtest + WF + MC + gates)', promoted: true},
    {name: 'quick-test', desc: 'Quick sanity backtest', promoted: true},
    {name: 'portfolio-backtest', desc: 'Multi-strategy portfolio backtest', promoted: true},
    {name: 'portfolio-matrix', desc: 'Strategy-pair correlation matrix', promoted: true},
    {name: 'cross-asset', desc: 'Cross-pair / cross-asset correlation research', promoted: true},
    {name: 'feature-importance', desc: 'Permutation feature importance for a strategy', promoted: false},
    {name: 'indicator-stats', desc: 'Indicator IC / decay statistics', promoted: false},
    {name: 'signal-stats', desc: 'Signal hit rate, IC, decay', promoted: false},
    {name: 'signal-decay', desc: 'Alpha-decay curve for a signal', promoted: false},
    {name: 'signal-backfill', desc: 'Backfill signal scores over history', promoted: false},
    {name: 'history', desc: 'Show backtest history', promoted: false},
  ],
  'Validation & promotion': [
    {name: 'validate-strategy', desc: 'Lint a strategy file (SDK validator)', promoted: false},
    {name: 'verify', desc: 'Verify strategy vs buy-and-hold over a date range', promoted: true},
    {name: 'tearsheet', desc: 'Generate a strategy tearsheet (md)', promoted: false},
    {name: 'audit', desc: 'Export compliance-grade audit trail (csv/json)', promoted: true},
    {name: 'experiments', desc: 'List recent experiment runs', promoted: false},
    {name: 'experiment-revert', desc: 'Revert a strategy to an earlier experiment snapshot', promoted: false},
    {name: 'save-optimized', desc: 'Save a parameter-optimized strategy config', promoted: false},
    {name: 'versions', desc: 'List strategy version history', promoted: false},
  ],
  'Workbench (strategy authoring)': [
    {name: 'workbench-create', desc: 'Create a new workbench strategy', promoted: true},
    {name: 'workbench-list', desc: 'List workbench strategies', promoted: false},
    {name: 'workbench-show', desc: 'Show a workbench strategy', promoted: false},
    {name: 'workbench-update', desc: 'Update workbench config', promoted: false},
    {name: 'workbench-delete', desc: 'Delete a workbench strategy', promoted: false},
    {name: 'workbench-generate', desc: 'Generate a strategy from a workbench config', promoted: false},
    {name: 'workbench-templates', desc: 'List workbench templates', promoted: false},
    {name: 'workbench-components', desc: 'List workbench signal components', promoted: false},
  ],
  'Data & discovery': [
    {name: 'sync', desc: 'Sync HL S3 archive into local cache', promoted: true},
    {name: 'fetch', desc: 'Fetch candles from HL REST API', promoted: false},
    {name: 'fetch-multi', desc: 'Fetch multiple pairs', promoted: false},
    {name: 'list-pairs', desc: 'List all HL trading pairs', promoted: false},
    {name: 'list-data', desc: 'Inventory of cached data', promoted: false},
    {name: 'data-inventory', desc: 'Detailed cache inventory + freshness', promoted: true},
    {name: 'diff', desc: 'Diff two backtest runs', promoted: false},
    {name: 'collect', desc: 'Start persistent data collector daemon', promoted: false},
    {name: 'collect-status', desc: 'Collector daemon status', promoted: false},
    {name: 'indicators', desc: 'Compute an indicator on cached data', promoted: false},
    {name: 'funding-browser', desc: 'Browse funding-rate history across pairs', promoted: true},
    {name: 'order-flow', desc: 'Inspect order-flow microstructure', promoted: false},
    {name: 'regime', desc: 'Detect market regime (HMM / changepoints)', promoted: false},
  ],
  'Trading & execution': [
    {name: 'algo', desc: 'Run an automated strategy', promoted: true},
    {name: 'algo-status', desc: 'Algo session status', promoted: false},
    {name: 'algo-stop', desc: 'Stop a running algo', promoted: false},
    {name: 'recon', desc: 'Reconnaissance / dry-run trading', promoted: false},
    {name: 'manual-trade', desc: 'Place a manual trade (engine-side)', promoted: false},
    {name: 'test-trade', desc: 'Minimum-size test trade for connectivity', promoted: true},
    {name: 'buy', desc: 'Direct buy order', promoted: false},
    {name: 'sell', desc: 'Direct sell order', promoted: false},
    {name: 'scan', desc: 'Scan multiple pairs for entry signals', promoted: false},
    {name: 'scout', desc: 'Confluence-ranked opportunity scout', promoted: true},
    {name: 'close-position', desc: 'Close a single position', promoted: false},
    {name: 'close-all', desc: 'Close every open position', promoted: false},
    {name: 'tighten-stop', desc: 'Move a stop loss closer to price', promoted: false},
    {name: 'reduce-position', desc: 'Reduce position size', promoted: false},
  ],
  'Portfolio': [
    {name: 'portfolio-start', desc: 'Launch portfolio supervisor + strategies', promoted: false},
    {name: 'portfolio-status', desc: 'Portfolio runtime status', promoted: false},
    {name: 'portfolio-stop', desc: 'Stop portfolio + all strategies', promoted: false},
    {name: 'tca', desc: 'Transaction cost analysis on session log', promoted: false},
    {name: 'attribution', desc: 'PnL attribution (alpha vs costs vs funding)', promoted: false},
    {name: 'report', desc: 'Generate portfolio performance report', promoted: false},
    {name: 'var', desc: 'Portfolio Value-at-Risk', promoted: false},
    {name: 'pairs-backtest', desc: 'Pairs-trading portfolio backtest', promoted: true},
  ],
  'Account & wallet': [
    {name: 'balance', desc: 'Show HL account balance', promoted: false},
    {name: 'holdings', desc: 'Show open positions', promoted: false},
    {name: 'state', desc: 'Full account state snapshot', promoted: false},
    {name: 'transfer', desc: 'Transfer USDC between HL spot and perp', promoted: true},
    {name: 'agent-pair', desc: 'Pair an API wallet to your main wallet', promoted: false},
    {name: 'agent-rotate', desc: 'Rotate the API wallet', promoted: false},
    {name: 'agent-status', desc: 'Show paired API wallet status', promoted: false},
    {name: 'account-mode-status', desc: 'Show HL account mode (isolated/cross/etc.)', promoted: false},
    {name: 'account-mode-set', desc: 'Set HL account mode', promoted: false},
    {name: 'token-issue', desc: 'Issue an authorization token', promoted: false},
    {name: 'token-list', desc: 'List authorization tokens', promoted: false},
    {name: 'token-revoke', desc: 'Revoke an authorization token', promoted: false},
    {name: 'token-show', desc: 'Show a specific token', promoted: false},
  ],
  'Learning & lessons': [
    {name: 'lessons', desc: 'Show captured trading lessons', promoted: true},
    {name: 'add-lesson', desc: 'Add a manual lesson entry', promoted: false},
    {name: 'guide', desc: 'Interactive learning guide', promoted: true},
  ],
  'System & admin': [
    {name: 'doctor', desc: 'System health check', promoted: true},
    {name: 'health', desc: 'Lightweight health snapshot', promoted: false},
    {name: 'version', desc: 'Show RIFT version', promoted: false},
    {name: 'strategies', desc: 'List registered strategies', promoted: false},
    {name: 'check-api', desc: 'Verify HL API reachability', promoted: false},
    {name: 'set-proxy', desc: 'Configure HTTP proxy', promoted: false},
    {name: 'clear-proxy', desc: 'Clear HTTP proxy setting', promoted: false},
    {name: 'auth', desc: 'Wallet auth setup', promoted: true},
    {name: 'approve-builder-fee', desc: 'On-chain builder-fee approval', promoted: false},
    {name: 'check-builder-fee', desc: 'Verify builder-fee approval state', promoted: false},
    {name: 'api-start', desc: 'Start the HTTP API server', promoted: false},
    {name: 'api-stop', desc: 'Stop the HTTP API server', promoted: false},
    {name: 'watchdog', desc: 'Start the kill-switch watchdog', promoted: false},
    {name: 'watchdog-stop', desc: 'Stop the watchdog', promoted: false},
    {name: 'watchdog-events', desc: 'Show recent watchdog events', promoted: false},
    {name: 'cost', desc: 'Pre-trade cost estimate', promoted: true},
  ],
}

export default class More extends GatedCommand {
  static override description =
    'Discover and run every engine command — including those without a top-level `rift <cmd>` wrapper'

  static override examples = [
    '$ rift more                          # list all engine commands by category',
    '$ rift more funding-browser BTC      # run the funding-browser command',
    '$ rift more verify <bundle-id>       # verify a sealed bundle',
  ]

  // Pure passthrough — `rift more` reads raw argv directly so unknown flags
  // intended for the engine command (e.g. `--local-main-key`) aren't
  // intercepted by oclif's flag parser. The first arg after `more` is the
  // engine command name; everything else is forwarded verbatim.
  static override args = {
    command: Args.string({description: 'Engine command name (omit to list)'}),
  }

  static override strict = false

  async run(): Promise<void> {
    // Bypass oclif's flag parser. process.argv is:
    //   [node, run.js, "more", <engineCmd>, <...passthroughArgs>]
    const rawArgs = process.argv.slice(3)

    // No command → render the catalog.
    if (rawArgs.length === 0) {
      this.renderCatalog()
      return
    }

    const [command, ...passthrough] = rawArgs

    // Track whether the engine surfaced its own error message. If it did,
    // we suppress the generic "Engine exited with code N" footer on
    // non-zero exit — the user already saw the helpful message.
    let surfacedError = false

    try {
      await runEngine(command, passthrough, (msg: EngineMessage) => {
        // Mirror the engine's NDJSON to the user. For programmatic use,
        // they can pipe `rift more <cmd> --json …`; here we just surface
        // what the engine emits in human-readable form.
        const type = msg.type as string
        if (type === 'progress' && msg.msg) {
          this.log(dim(`  ${msg.msg}`))
        } else if (type === 'status' && msg.msg) {
          this.log(`  ${msg.msg}`)
        } else if (type === 'error' && msg.msg) {
          // Log + flag, but do NOT call this.error() here — it throws
          // a CLIError synchronously inside an async readline callback,
          // which oclif's lifecycle does not catch reliably. We let the
          // engine's non-zero exit propagate via runEngine's rejection
          // path, and exit(1) silently from the catch below.
          this.log(`  ${red('Error:')} ${msg.msg}`)
          surfacedError = true
        } else if (type === 'result') {
          // The engine emits a structured result — print as pretty JSON
          // so the user can pipe it or read it.
          const {type: _t, ...rest} = msg
          this.log(JSON.stringify(rest, null, 2))
        } else if (msg.msg) {
          this.log(`  ${msg.msg}`)
        }
      })
    } catch (err) {
      if (surfacedError) {
        // The engine already printed a human-readable error above.
        // Exit non-zero silently — no second confusing footer.
        this.exit(1)
      }
      // Genuine engine crash with no structured error — re-raise via
      // this.error so the user sees the stderr-extracted message.
      throw err
    }
  }

  private renderCatalog(): void {
    this.log('')
    this.log(`  ${bold('RIFT — Full engine command catalog')}`)
    this.log(`  ${dim('─'.repeat(64))}`)
    this.log('')
    this.log(`  Commands marked ${green('●')} have a first-class ${cyan('rift <name>')} wrapper.`)
    this.log(`  All others are runnable via ${cyan('rift more <name> [args...]')}.`)
    this.log('')

    let total = 0
    let promoted = 0

    for (const [section, entries] of Object.entries(CATALOG)) {
      this.log(`  ${bold(section)}`)
      const maxName = Math.max(...entries.map(e => e.name.length))
      for (const e of entries) {
        total++
        if (e.promoted) promoted++
        const marker = e.promoted ? green('●') : ' '
        const name = e.name.padEnd(maxName + 2)
        this.log(`    ${marker} ${cyan(name)} ${dim(e.desc)}`)
      }
      this.log('')
    }

    this.log(`  ${dim(`${total} engine commands · ${promoted} promoted as rift <cmd>`)}`)
    this.log('')
  }
}
