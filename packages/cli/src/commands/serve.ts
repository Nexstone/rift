import {Flags} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import {McpServer} from '@modelcontextprotocol/sdk/server/mcp.js'
import {StdioServerTransport} from '@modelcontextprotocol/sdk/server/stdio.js'
import {z} from 'zod'
import {runEngine} from '../lib/python-bridge.js'
import type {EngineMessage} from '../lib/python-bridge.js'

/**
 * Collect the final "result" message from the engine, ignoring progress messages.
 */
function collectResult(command: string, args: string[]): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let result: Record<string, unknown> | null = null

    runEngine(command, args, (msg: EngineMessage) => {
      if (msg.type === 'result') {
        result = msg as Record<string, unknown>
      } else if (msg.type === 'error') {
        reject(new Error(msg.msg as string))
      }
    })
      .then(() => {
        if (result) {
          resolve(result)
        } else {
          reject(new Error('No result returned from engine'))
        }
      })
      .catch(reject)
  })
}

/** Clean internal fields from engine result */
function cleanResult(result: Record<string, unknown>): Record<string, unknown> {
  const {type, command, ...clean} = result
  return clean
}

export default class Serve extends GatedCommand {
  static override description = 'Start RIFT as an MCP server for AI agent integration'

  static override examples = [
    '$ rift serve',
  ]

  static override flags = {
    debug: Flags.boolean({description: 'Enable debug logging to stderr', default: false}),
  }

  async run(): Promise<void> {
    const {flags} = await this.parse(Serve)

    const server = new McpServer({
      name: 'rift',
      version: '0.1.0',
    })

    // Validated schemas — prevent path traversal and injection
    const safeStrategy = z.string().regex(/^[a-zA-Z_][a-zA-Z0-9_]{0,63}$/, 'Invalid strategy name: letters, numbers, underscores only')
    const safePair = z.string().regex(/^[a-zA-Z0-9/:]{1,20}(-PERP)?$/i, 'Invalid pair name')
    const safeTimeframe = z.string().regex(/^[0-9]+[mhdwM]$/, 'Invalid timeframe (e.g. 1h, 4h, 1d)')
    const safeCoin = z.string().regex(/^[a-zA-Z0-9]{1,10}$/, 'Invalid coin name')

    // ═══════════════════════════════════════════
    //  GROUP 1: Core tools (updated)
    // ═══════════════════════════════════════════

    server.tool(
      'backtest',
      'Run a backtest of a trading strategy on historical Hyperliquid data. Supports crypto perps and TradFi (SP500, TSLA, CL, GOLD). Returns performance metrics, diagnostics, and regime analysis.',
      {
        strategy: z.string().describe('Strategy name (any registered or workbench strategy)'),
        pair: z.string().default('BTC-PERP').describe('Trading pair (e.g. BTC-PERP, ETH-PERP, SOL-PERP, HYPE-PERP)'),
        timeframe: z.string().optional().describe('Candle timeframe (auto-detected from strategy if omitted). Options: 1m, 5m, 15m, 30m, 1h, 4h'),
        equity: z.number().default(10000).describe('Starting equity in USDC'),
        all_pairs: z.boolean().default(false).describe('If true, test across top 10 pairs by volume and rank results by Sharpe ratio'),
      },
      async ({strategy, pair, timeframe, equity, all_pairs}) => {
        try {
          const args = [strategy, '--pair', pair, '--equity', String(equity)]
          if (timeframe) args.push('--tf', timeframe)
          if (all_pairs) args.push('--all-pairs', '--top', '10')

          const result = await collectResult('backtest', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'compare',
      'Compare multiple trading strategies head-to-head on the same data. Returns side-by-side metrics and identifies the best by both return and Sharpe ratio.',
      {
        strategies: z.string().describe('Comma-separated strategy names'),
        pair: safePair.default('BTC-PERP').describe('Trading pair'),
        timeframe: z.string().default('1h').describe('Candle timeframe'),
        equity: z.number().default(10000).describe('Starting equity in USDC'),
      },
      async ({strategies, pair, timeframe, equity}) => {
        try {
          const result = await collectResult('compare', [
            strategies, '--pair', pair, '--tf', timeframe, '--equity', String(equity),
          ])

          const results = result.results as any[]
          const bestReturn = results.reduce((a: any, b: any) => a.total_return_pct > b.total_return_pct ? a : b)
          const bestSharpe = results.reduce((a: any, b: any) => a.sharpe_ratio > b.sharpe_ratio ? a : b)

          return {content: [{
            type: 'text' as const,
            text: JSON.stringify({
              results,
              best_by_return: {strategy: bestReturn.strategy, return_pct: bestReturn.total_return_pct},
              best_by_sharpe: {strategy: bestSharpe.strategy, sharpe: bestSharpe.sharpe_ratio},
            }, null, 2),
          }]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'list_strategies',
      'List all available trading strategies (validated + custom workbench strategies) with configurations and descriptions.',
      {},
      async () => {
        try {
          const result = await collectResult('strategies', [])

          // Also get workbench strategies
          let workbenchStrategies: any[] = []
          try {
            const wbResult = await collectResult('workbench-list', [])
            workbenchStrategies = (wbResult.strategies as any[]) || []
          } catch { /* no workbench strategies */ }

          return {content: [{
            type: 'text' as const,
            text: JSON.stringify({
              validated: result.strategies,
              custom: workbenchStrategies,
            }, null, 2),
          }]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'fetch_data',
      'Download and cache candle data + funding rates from Hyperliquid. Usually not needed — backtest and research auto-fetch. Use this to pre-cache data for a specific date range.',
      {
        pair: safePair.default('BTC-PERP').describe('Trading pair'),
        timeframe: z.string().default('1h').describe('Candle timeframe'),
        start: z.string().optional().describe('Start date YYYY-MM-DD (optional)'),
      },
      async ({pair, timeframe, start}) => {
        try {
          const args = [pair, '--tf', timeframe]
          if (start) args.push('--start', start)
          const result = await collectResult('fetch', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'cost',
      'Estimate pre-trade cost for a hypothetical trade: fees + funding + impact + slippage. Returns breakdown in bps and USD, plus ADV-utilization warning. Use to answer "what will it cost me to trade $X of <coin> right now?" without actually executing.',
      {
        pair: safePair.describe('Trading pair (BTC, ETH-PERP, etc.)'),
        notional_usd: z.number().positive().describe('Trade size in USD notional'),
        side: z.enum(['buy', 'sell', 'long', 'short']).default('buy').describe('Trade direction'),
        timeframe: z.string().default('1h').describe('Candle interval for ADV / vol calc'),
        hold_hours: z.number().nonnegative().default(0).describe('Holding period in hours (for funding accrual estimate)'),
        maker: z.boolean().default(false).describe('Treat as maker (post-only) instead of taker'),
        spot: z.boolean().default(false).describe('Treat as spot trade instead of perp'),
        include_builder_fee: z.boolean().default(true).describe('Include RIFT builder fee'),
        tier_volume_14d_usd: z.number().nonnegative().default(0).describe('Your 14d HL volume USD for fee-tier lookup'),
      },
      async ({pair, notional_usd, side, timeframe, hold_hours, maker, spot, include_builder_fee, tier_volume_14d_usd}) => {
        try {
          const args = [pair, String(notional_usd)]
          args.push('--side', side)
          args.push('--tf', timeframe)
          args.push('--hold', String(hold_hours))
          if (maker) args.push('--maker')
          if (spot) args.push('--spot')
          if (!include_builder_fee) args.push('--no-builder-fee')
          args.push('--tier-vol-14d', String(tier_volume_14d_usd))
          const result = await collectResult('cost', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'list_data',
      'List all locally cached candle datasets with pair, timeframe, candle count, and date range.',
      {},
      async () => {
        try {
          const result = await collectResult('list-data', [])
          return {content: [{type: 'text' as const, text: JSON.stringify(result.data, null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'doctor',
      'Check RIFT system health. Verifies Python, dependencies, API connectivity, cached data, and strategies.',
      {},
      async () => {
        try {
          const result = await collectResult('doctor', [])
          return {content: [{type: 'text' as const, text: JSON.stringify(result.checks, null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 2: Validation pipeline
    // ═══════════════════════════════════════════

    server.tool(
      'research',
      'Run the full validation pipeline on a strategy: backtest + walk-forward analysis + Monte Carlo simulation + multi-pair test. Returns a grade (A/B/C/D/F) with detailed metrics. This is the most comprehensive strategy assessment tool. Supports config overrides for testing optimized parameters.',
      {
        strategy: safeStrategy.describe('Strategy name'),
        pair: safePair.default('BTC-PERP').describe('Trading pair'),
        timeframe: z.string().optional().describe('Timeframe (auto-detected if omitted)'),
        equity: z.number().default(10000).describe('Starting equity'),
        config_overrides: z.string().optional().describe('JSON string of config param overrides (e.g. \'{"stop_loss_pct": 0.03, "max_hold_candles": 24}\')'),
      },
      async ({strategy, pair, timeframe, equity, config_overrides}) => {
        try {
          const args = [strategy, '--pair', pair, '--equity', String(equity)]
          if (timeframe) args.push('--tf', timeframe)
          if (config_overrides) args.push('--config-overrides', config_overrides)

          const result = await collectResult('research', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'walk_forward',
      'Run walk-forward analysis to test strategy robustness. Splits data into rolling train/test windows and measures out-of-sample performance degradation. ROBUST (>0.7) means the strategy generalizes well.',
      {
        strategy: safeStrategy.describe('Strategy name'),
        pair: safePair.default('BTC-PERP').describe('Trading pair'),
        timeframe: z.string().default('1h').describe('Timeframe'),
        config: z.string().default('3m/1m').describe('Train/test config (e.g. 3m/1m = 3 months train, 1 month test)'),
        equity: z.number().default(10000).describe('Starting equity per window'),
      },
      async ({strategy, pair, timeframe, config, equity}) => {
        try {
          const result = await collectResult('walk-forward', [
            strategy, '--pair', pair, '--tf', timeframe, '--wf', config, '--equity', String(equity),
          ])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'montecarlo',
      'Run Monte Carlo simulation — bootstrap resample the trade sequence 10,000 times to estimate probability of profit, ruin, and return distribution percentiles.',
      {
        strategy: safeStrategy.describe('Strategy name'),
        pair: safePair.default('BTC-PERP').describe('Trading pair'),
        timeframe: z.string().default('1h').describe('Timeframe'),
        runs: z.number().default(10000).describe('Number of simulations'),
        equity: z.number().default(10000).describe('Starting equity'),
      },
      async ({strategy, pair, timeframe, runs, equity}) => {
        try {
          const result = await collectResult('montecarlo', [
            strategy, '--pair', pair, '--tf', timeframe, '--runs', String(runs), '--equity', String(equity),
          ])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 3: Optimization
    // ═══════════════════════════════════════════

    server.tool(
      'sweep',
      'Run a parameter sweep to find optimal strategy settings. Tests all parameter combinations and ranks by Sharpe ratio (or return/profit factor). Returns the top results with their exact parameter values.',
      {
        strategy: safeStrategy.describe('Strategy name'),
        pair: safePair.default('BTC-PERP').describe('Trading pair'),
        timeframe: z.string().default('1h').describe('Timeframe'),
        top: z.number().default(5).describe('Number of top results to return'),
        rank_by: z.enum(['sharpe', 'return', 'profit_factor']).default('sharpe').describe('Ranking metric'),
      },
      async ({strategy, pair, timeframe, top, rank_by}) => {
        try {
          const result = await collectResult('sweep', [
            strategy, '--pair', pair, '--tf', timeframe, '--top', String(top), '--rank', rank_by,
          ])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'save_optimized',
      'Save an optimized strategy by copying a base strategy and replacing its config defaults with new parameter values. Creates a new .py file in the workbench directory that can be backtested, researched, and traded.',
      {
        base_strategy: safeStrategy.describe('Base strategy to copy from'),
        new_name: z.string().describe('Name for the new strategy (snake_case)'),
        params: z.string().describe('JSON string of optimized parameters (e.g. \'{"stop_loss_pct": 0.03, "entry_dev": 3.75}\')'),
      },
      async ({base_strategy, new_name, params}) => {
        try {
          const result = await collectResult('save-optimized', [base_strategy, new_name, params])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 4: Strategy building (workbench)
    // ═══════════════════════════════════════════

    server.tool(
      'workbench_create',
      'Create a new custom strategy from a template. Templates: "funding", "vwap_reversion", "trend_follow", "blank". 56 indicators available including cross-asset, multi-timeframe, and adaptive. No Python knowledge needed.',
      {
        name: safeStrategy.describe('Strategy name (snake_case)'),
        template: z.enum(['funding', 'vwap_reversion', 'trend_follow', 'blank']).default('blank').describe('Template to start from'),
      },
      async ({name, template}) => {
        try {
          const result = await collectResult('workbench-create', [name, '--template', template])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'workbench_update',
      'Update a custom strategy config and regenerate the Python code. Pass the full config JSON with modified entry/exit conditions, risk settings, or filters.',
      {
        name: safeStrategy.describe('Strategy name to update'),
        config: z.string().describe('Full config JSON string'),
      },
      async ({name, config}) => {
        try {
          const result = await collectResult('workbench-update', [name, config])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'workbench_show',
      'Show a custom strategy\'s current config — entry/exit conditions, risk settings, filters, and version.',
      {
        name: safeStrategy.describe('Strategy name'),
      },
      async ({name}) => {
        try {
          const result = await collectResult('workbench-show', [name])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'quick_test',
      'Fast backtest with automatic delta comparison to the last test. Every test is logged to the experiment database. Use this for rapid iteration — change a parameter, quick test, see if it improved.',
      {
        strategy: safeStrategy.describe('Strategy name'),
        pair: safePair.default('BTC-PERP').describe('Trading pair'),
        change_description: z.string().optional().describe('Description of what changed (logged for experiment tracking)'),
      },
      async ({strategy, pair, change_description}) => {
        try {
          const args = [strategy, '--pair', pair]
          if (change_description) args.push('--change', change_description)
          const result = await collectResult('quick-test', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 5: Market intelligence
    // ═══════════════════════════════════════════

    server.tool(
      'indicator_stats',
      'Get real market statistics for a trading pair from cached data. Shows distributions and recommended values for: funding rate, RSI, VWAP z-score, ADX, volume ratio, EMA distance, ATR. Use this to understand market conditions and choose appropriate strategy parameters.',
      {
        pair: z.string().default('BTC').describe('Trading pair (without -PERP suffix)'),
        timeframe: z.string().default('1h').describe('Timeframe'),
      },
      async ({pair, timeframe}) => {
        try {
          const result = await collectResult('indicator-stats', ['--pair', pair, '--tf', timeframe])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 6: Experiment tracking
    // ═══════════════════════════════════════════

    server.tool(
      'experiments',
      'View experiment history for a strategy. Shows every quick test result with config snapshot, metrics, version, and what changed. Use this to track iteration progress and avoid repeating experiments.',
      {
        strategy: safeStrategy.describe('Strategy name'),
        limit: z.number().default(20).describe('Number of experiments to return'),
      },
      async ({strategy, limit}) => {
        try {
          const result = await collectResult('experiments', [strategy, '--limit', String(limit)])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'smart_sweep',
      'Smart parameter optimization using Bayesian search (Optuna). Finds optimal strategy parameters in ~50-80 trials instead of testing every combination. 10x faster than grid sweep.',
      {
        strategy: safeStrategy.describe('Strategy name'),
        pair: safePair.default('BTC').describe('Trading pair'),
        timeframe: z.string().default('1h').describe('Timeframe'),
        trials: z.number().default(80).describe('Number of optimization trials (50-100 recommended)'),
        target: z.enum(['sharpe', 'return', 'calmar']).default('sharpe').describe('What to optimize for'),
      },
      async ({strategy, pair, timeframe, trials, target}) => {
        try {
          const result = await collectResult('smart-sweep', [strategy, '--pair', pair, '--tf', timeframe, '--trials', String(trials), '--target', target])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'feature_importance',
      'Discover which indicators predict profitable trades using XGBoost. Trains a classifier on all indicator values at trade entry points, ranks features by predictive power. Reveals hidden patterns.',
      {
        strategy: safeStrategy.describe('Strategy name'),
        pair: safePair.default('BTC').describe('Trading pair'),
        timeframe: z.string().default('1h').describe('Timeframe'),
      },
      async ({strategy, pair, timeframe}) => {
        try {
          const result = await collectResult('feature-importance', [strategy, '--pair', pair, '--tf', timeframe])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'tearsheet',
      'Generate a professional HTML performance tearsheet with 30+ charts: equity curve, drawdowns, monthly returns, rolling Sharpe, return distribution. Opens at ~/.rift/reports/.',
      {
        strategy: safeStrategy.describe('Strategy name'),
        pair: safePair.default('BTC').describe('Trading pair'),
        timeframe: z.string().default('1h').describe('Timeframe'),
      },
      async ({strategy, pair, timeframe}) => {
        try {
          const result = await collectResult('tearsheet', [strategy, '--pair', pair, '--tf', timeframe])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'health',
      'Check strategy health using institutional-grade decay detection. Returns a 0-100 score with CUSUM change detection, factor decomposition (alpha vs beta), statistical decay testing, and execution quality analysis. Grade A-F with recommendation (continue/reduce/pause/stop).',
      {
        strategy: safeStrategy.describe('Strategy name'),
        pair: safePair.default('BTC').describe('Trading pair'),
        timeframe: z.string().default('1h').describe('Timeframe'),
      },
      async ({strategy, pair, timeframe}) => {
        try {
          const result = await collectResult('health', [strategy, '--pair', pair, '--tf', timeframe])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 7: Live trading (daemon)
    // ═══════════════════════════════════════════

    server.tool(
      'algo_start',
      'Start an algo trading session as a background daemon. The trading engine runs persistently — it survives disconnects, app closes, and session changes. Returns immediately with the daemon PID. Use algo_status to monitor and algo_stop to end.',
      {
        strategy: safeStrategy.describe('Strategy name'),
        pair: z.string().default('BTC').describe('Trading pair (e.g. BTC, ETH, SOL)'),
        timeframe: z.string().optional().describe('Candle timeframe (default: strategy default)'),
        equity: z.number().default(0).describe('Starting equity in USDC (0 = auto-detect from account)'),
      },
      async ({strategy, pair, timeframe, equity}) => {
        try {
          const {spawnDaemon, getAlgoPidsDir} = await import('../lib/python-bridge.js')
          const {loadCredentials, hasFullSetup, getAccountAddress} = await import('../lib/credentials.js')
          const fs = await import('node:fs')
          const path = await import('node:path')

          if (!hasFullSetup()) {
            return {content: [{type: 'text' as const, text: 'Error: Account not set up. Run: rift auth setup'}], isError: true}
          }

          const creds = loadCredentials()
          if (!creds) {
            return {content: [{type: 'text' as const, text: 'Error: No credentials found. Run: rift auth setup'}], isError: true}
          }

          // Check if already running
          const coin = pair.replace(/-PERP/i, '').toUpperCase()
          const key = `${strategy}_${coin}`
          const pidFile = path.join(getAlgoPidsDir(), `${key}.pid`)
          if (fs.existsSync(pidFile)) {
            try {
              const pid = parseInt(fs.readFileSync(pidFile, 'utf-8').trim())
              process.kill(pid, 0)
              return {content: [{type: 'text' as const, text: JSON.stringify({status: 'already_running', key, pid}, null, 2)}]}
            } catch {
              fs.unlinkSync(pidFile)
            }
          }

          const engineArgs = [strategy, '--pair', pair, '--equity', String(equity), '--account', getAccountAddress(creds)]
          if (timeframe) engineArgs.push('--tf', timeframe)

          const {pid} = spawnDaemon('algo', engineArgs, {HYPERLIQUID_PRIVATE_KEY: creds.private_key})

          return {content: [{type: 'text' as const, text: JSON.stringify({
            status: 'started',
            key,
            pid,
            strategy,
            pair: coin,
            msg: `Algo trading daemon started. Use algo_status to monitor, algo_stop to end.`,
          }, null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'algo_status',
      'Get the current state of all running algo trading sessions. Returns equity, P&L, position details, trade count, health score, and more. Works whether the session was started from CLI, MCP, or any other interface.',
      {},
      async () => {
        try {
          const result = await collectResult('algo-status', [])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'algo_stop',
      'Stop a running algo trading session. Sends a graceful shutdown signal — the daemon closes any open position, saves the session log, and exits. Returns the final session summary.',
      {
        strategy: safeStrategy.describe('Strategy name to stop'),
        pair: safePair.default('BTC').describe('Trading pair'),
      },
      async ({strategy, pair}) => {
        try {
          const result = await collectResult('algo-stop', ['--strategy', strategy, '--pair', pair])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 8: Portfolio management
    // ═══════════════════════════════════════════

    server.tool(
      'portfolio_start',
      'Start the portfolio supervisor daemon to manage multiple algo trading strategies simultaneously. Coordinates risk across strategies, enforces scheduling, monitors health, auto-rotates decaying strategies, and fires alerts. Configure via portfolio.yaml.',
      {
        config_path: z.string().optional().describe('Path to portfolio.yaml (default: ~/.rift/algo/portfolio.yaml)'),
      },
      async ({config_path}) => {
        try {
          const {spawnDaemon, getDataDir} = await import('../lib/python-bridge.js')
          const {loadCredentials, hasFullSetup, getAccountAddress} = await import('../lib/credentials.js')
          const fs = await import('node:fs')
          const path = await import('node:path')

          if (!hasFullSetup()) {
            return {content: [{type: 'text' as const, text: 'Error: Account not set up. Run: rift auth setup'}], isError: true}
          }

          const creds = loadCredentials()
          if (!creds) {
            return {content: [{type: 'text' as const, text: 'Error: No credentials'}], isError: true}
          }

          // Check if already running
          const pidFile = path.join(getDataDir(), 'algo', 'supervisor.pid')
          if (fs.existsSync(pidFile)) {
            try {
              const pid = parseInt(fs.readFileSync(pidFile, 'utf-8').trim())
              process.kill(pid, 0)
              return {content: [{type: 'text' as const, text: JSON.stringify({status: 'already_running', pid}, null, 2)}]}
            } catch {
              fs.unlinkSync(pidFile)
            }
          }

          const cfgPath = config_path || path.join(getDataDir(), 'algo', 'portfolio.yaml')
          const args = ['--config', cfgPath, '--account', getAccountAddress(creds)]

          const {pid} = spawnDaemon('portfolio-start', args, {HYPERLIQUID_PRIVATE_KEY: creds.private_key})

          return {content: [{type: 'text' as const, text: JSON.stringify({
            status: 'started', pid,
            msg: 'Portfolio supervisor started. Use portfolio_status to monitor.',
          }, null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'portfolio_status',
      'Get the full state of the portfolio supervisor: all managed strategies, positions, aggregate risk metrics (net/gross exposure, per-asset, drawdown), health scores, and recent alerts.',
      {},
      async () => {
        try {
          const result = await collectResult('portfolio-status', [])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'portfolio_stop',
      'Stop the portfolio supervisor and all managed strategy daemons. Gracefully closes all positions, saves session logs, and returns final portfolio summary.',
      {},
      async () => {
        try {
          const result = await collectResult('portfolio-stop', [])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'portfolio_alerts',
      'Get recent portfolio alerts — trades, stop losses, health drops, drawdown warnings, session failures, and scheduling events.',
      {
        limit: z.number().default(20).describe('Number of recent alerts to return'),
      },
      async ({limit}) => {
        try {
          const fs = await import('node:fs')
          const path = await import('node:path')
          const {getDataDir} = await import('../lib/python-bridge.js')

          const alertsFile = path.join(getDataDir(), 'algo', 'alerts.log')
          if (!fs.existsSync(alertsFile)) {
            return {content: [{type: 'text' as const, text: JSON.stringify({alerts: []}, null, 2)}]}
          }

          const lines = fs.readFileSync(alertsFile, 'utf-8').trim().split('\n').filter((l: string) => l.trim())
          const alerts = lines.slice(-limit).map((l: string) => {
            try { return JSON.parse(l) } catch { return null }
          }).filter(Boolean)

          return {content: [{type: 'text' as const, text: JSON.stringify({alerts}, null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 9: Analytics & Reporting
    // ═══════════════════════════════════════════

    server.tool(
      'tca_report',
      'Transaction Cost Analysis — analyze execution quality across algo trading sessions. Shows slippage (bps), market impact, fee costs, TWAP vs IOC comparison, post-fill markouts at t+1s/10s/60s/300s (positive = trader edge, negative = adverse selection), and grades execution A-F relative to asset volatility.',
      {
        session: z.string().optional().describe('Path to specific session log (default: all sessions)'),
      },
      async ({session}) => {
        try {
          const args = session ? ['--session', session] : []
          const result = await collectResult('tca', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'pnl_attribution',
      'Decompose P&L into components: alpha (strategy edge), beta (market exposure), funding income, and execution costs (slippage + fees). Uses linear regression to separate true alpha from market drift.',
      {
        session: z.string().optional().describe('Path to specific session log (default: all sessions)'),
      },
      async ({session}) => {
        try {
          const args = session ? ['--session', session] : []
          const result = await collectResult('attribution', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'generate_report',
      'Generate an HTML performance report with equity curves, TCA summary, PnL attribution waterfall, trade log, and a substrate.stats tearsheet (bootstrap CIs, PSR). Returns the file path.',
      {
        portfolio: z.boolean().default(false).describe('Generate portfolio-level report (all strategies combined)'),
        period: z.enum(['all', 'daily', 'weekly']).default('all').describe('Report period'),
        session: z.string().optional().describe('Path to specific session log'),
      },
      async ({portfolio, period, session}) => {
        try {
          const args: string[] = []
          if (portfolio) args.push('--portfolio')
          if (period !== 'all') args.push('--period', period)
          if (session) args.push('--session', session)
          const result = await collectResult('report', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 10: REST API
    // ═══════════════════════════════════════════

    server.tool(
      'api_start',
      'Start the RIFT REST API server for dashboard and PMS integration. Returns the URL and auth token. Endpoints: /status, /positions, /trades, /alerts, /tca, /attribution, /health, /equity.',
      {
        port: z.number().default(8420).describe('Port to listen on'),
        require_auth: z.boolean().default(false).describe('Require auth token on all endpoints (not just POST)'),
      },
      async ({port, require_auth}) => {
        try {
          const {spawnDaemon} = await import('../lib/python-bridge.js')
          const fs = await import('node:fs')
          const path = await import('node:path')
          const {getDataDir} = await import('../lib/python-bridge.js')

          // Check if already running
          const pidFile = path.join(getDataDir(), 'algo', 'api.pid')
          if (fs.existsSync(pidFile)) {
            try {
              const pid = parseInt(fs.readFileSync(pidFile, 'utf-8').trim())
              process.kill(pid, 0)
              const tokenFile = path.join(getDataDir(), 'api_token')
              const token = fs.existsSync(tokenFile) ? fs.readFileSync(tokenFile, 'utf-8').trim() : ''
              return {content: [{type: 'text' as const, text: JSON.stringify({status: 'already_running', pid, url: `http://localhost:${port}`, token}, null, 2)}]}
            } catch {
              fs.unlinkSync(pidFile)
            }
          }

          const args = ['--port', String(port)]
          if (require_auth) args.push('--require-auth')
          spawnDaemon('api-start', args)

          // Wait briefly for token file
          await new Promise(r => setTimeout(r, 1000))
          const tokenFile = path.join(getDataDir(), 'api_token')
          const token = fs.existsSync(tokenFile) ? fs.readFileSync(tokenFile, 'utf-8').trim() : 'generating...'

          return {content: [{type: 'text' as const, text: JSON.stringify({
            status: 'started',
            url: `http://localhost:${port}`,
            token,
            endpoints: ['/status', '/positions', '/trades', '/alerts', '/tca', '/attribution', '/health', '/equity'],
          }, null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 11: Risk & Compliance
    // ═══════════════════════════════════════════

    server.tool(
      'var_report',
      'Compute Value at Risk — "what is the most I can lose in 24h at 95% confidence?" Uses Cornish-Fisher adjustment for crypto fat tails. Includes CVaR (Expected Shortfall).',
      {
        horizon: z.enum(['1h', '24h', '7d']).default('24h').describe('VaR time horizon'),
      },
      async ({horizon}) => {
        try {
          const result = await collectResult('var', ['--horizon', horizon])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'audit_export',
      'Export compliance-grade trade log as CSV or JSON. Two rows per trade (OPEN/CLOSE) with timestamps, order IDs, fill prices, slippage, fees, wallet addresses. Suitable for prime broker reporting.',
      {
        format: z.enum(['csv', 'json']).default('csv').describe('Export format'),
        days: z.number().default(30).describe('Days of history to include'),
        strategy: z.string().optional().describe('Filter by strategy name'),
      },
      async ({format, days, strategy}) => {
        try {
          const args = ['--export', format, '--last', String(days)]
          if (strategy) args.push('--strategy', strategy)
          const result = await collectResult('audit', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'strategy_versions',
      'Show strategy version history — config snapshots and code hashes recorded at each algo session start. Use --diff to see what changed between versions.',
      {
        strategy: z.string().optional().describe('Filter by strategy name'),
        diff: z.boolean().default(false).describe('Show changes between last two versions'),
      },
      async ({strategy, diff}) => {
        try {
          const args: string[] = []
          if (strategy) args.push('--strategy', strategy)
          if (diff) args.push('--diff')
          const result = await collectResult('versions', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 12: Retail — Scout & Trade
    // ═══════════════════════════════════════════

    server.tool(
      'scout',
      'Scan the market and rank trading opportunities by confluence. Analyzes top coins across 5 dimensions: funding, momentum, volatility, positioning, and cross-exchange signals. Returns ranked list with entry/stop/target levels.',
      {
        top: z.number().default(20).describe('Number of coins to scan'),
        timeframe: z.string().default('1h').describe('Timeframe for indicator computation'),
        min_confluence: z.number().default(2).describe('Minimum confluence score (1-5)'),
      },
      async ({top, timeframe, min_confluence}) => {
        try {
          const result = await collectResult('scout', ['--top', String(top), '--tf', timeframe, '--min', String(min_confluence)])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'manual_trade',
      'Place a manual trade with stop loss on Hyperliquid. The trade stays open until explicitly closed via algo_stop. Use scout first to find opportunities, then execute the best one.',
      {
        coin: safeCoin.describe('Coin to trade (e.g. BTC, ETH, SOL)'),
        side: z.enum(['long', 'short']).describe('Trade direction'),
        // size_usd and stop_pct are intentionally REQUIRED — no default.
        // An invisible default on a real-money trade is a footgun for AI
        // agents that don't pass every field. Leverage stays defaulted at
        // 1 because 1x (no leverage) is the conservative no-op.
        size_usd: z.number().positive().describe('Position size in USD (required — no default; this places a real trade)'),
        stop_pct: z.number().positive().describe('Stop loss percentage (required — e.g. 2 means 2%)'),
        leverage: z.number().default(1).describe('Leverage multiplier (default 1 = no leverage)'),
      },
      async ({coin, side, size_usd, stop_pct, leverage}) => {
        try {
          const {loadCredentials, hasFullSetup, getAccountAddress} = await import('../lib/credentials.js')
          if (!hasFullSetup()) {
            return {content: [{type: 'text' as const, text: 'Error: Account not set up. Run: rift auth setup'}], isError: true}
          }
          const creds = loadCredentials()
          if (!creds) {
            return {content: [{type: 'text' as const, text: 'Error: No credentials'}], isError: true}
          }

          const {spawnDaemon} = await import('../lib/python-bridge.js')
          const {getAccountAddress: getAcct} = await import('../lib/credentials.js')
          const args = [coin, side, '--size', String(size_usd), '--stop', String(stop_pct / 100), '--leverage', String(leverage), '--account', getAcct(creds)]

          const {pid} = spawnDaemon('manual-trade', args, {HYPERLIQUID_PRIVATE_KEY: creds.private_key})

          return {content: [{type: 'text' as const, text: JSON.stringify({
            status: 'placed',
            coin, side, size_usd, stop_pct, leverage, pid,
            msg: `${side.toUpperCase()} ${coin} $${size_usd} placed. Use algo_status to monitor, algo_stop to close.`,
          }, null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ── Canonical perp-namespace aliases for manual_trade. The
    // `manual_trade` tool above remains registered for back-compat;
    // perp_long / perp_short / perp_close make the verb match the
    // actual action and pair naturally with spot_buy / spot_sell.
    const perpOpen = async (side: 'long' | 'short', coin: string, size_usd: number, stop_pct: number, leverage: number) => {
      try {
        const {loadCredentials, hasFullSetup, getAccountAddress} = await import('../lib/credentials.js')
        if (!hasFullSetup()) {
          return {content: [{type: 'text' as const, text: 'Error: Account not set up. Run: rift auth setup'}], isError: true}
        }
        const creds = loadCredentials()
        if (!creds) {
          return {content: [{type: 'text' as const, text: 'Error: No credentials'}], isError: true}
        }
        const {spawnDaemon} = await import('../lib/python-bridge.js')
        const args = [coin, side, '--size', String(size_usd), '--stop', String(stop_pct / 100), '--leverage', String(leverage), '--account', getAccountAddress(creds)]
        const {pid} = spawnDaemon('manual-trade', args, {HYPERLIQUID_PRIVATE_KEY: creds.private_key})
        return {content: [{type: 'text' as const, text: JSON.stringify({
          status: 'placed',
          coin, side, size_usd, stop_pct, leverage, pid,
          msg: `${side.toUpperCase()} ${coin} $${size_usd} placed (stop ${stop_pct}%, ${leverage}x). Daemon monitors until stop hit or SIGTERM.`,
        }, null, 2)}]}
      } catch (error: any) {
        return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
      }
    }

    server.tool(
      'perp_long',
      'Open a LONG perp position with stop loss on Hyperliquid. The daemon monitors until stop hits or you call perp_close. Pairs naturally with spot_buy / spot_sell — note that "perp_long BTC" and "spot_buy BTC" are TOTALLY DIFFERENT actions even though both buy BTC.',
      {
        coin: safeCoin.describe('Coin to long (e.g. BTC, ETH, SOL)'),
        size_usd: z.number().positive().describe('Position size in USD (required)'),
        stop_pct: z.number().positive().describe('Stop loss percentage (required — e.g. 2 means 2%)'),
        leverage: z.number().default(1).describe('Leverage multiplier (default 1 = no leverage)'),
      },
      async ({coin, size_usd, stop_pct, leverage}) =>
        await perpOpen('long', coin, size_usd, stop_pct, leverage),
    )

    server.tool(
      'perp_short',
      'Open a SHORT perp position with stop loss on Hyperliquid. The daemon monitors until stop hits or you call perp_close. CAUTION: "perp_short BTC" OPENS A NEW SHORT — it does NOT close an existing long. To close a long, use perp_close.',
      {
        coin: safeCoin.describe('Coin to short (e.g. BTC, ETH, SOL)'),
        size_usd: z.number().positive().describe('Position size in USD (required)'),
        stop_pct: z.number().positive().describe('Stop loss percentage (required — e.g. 2 means 2%)'),
        leverage: z.number().default(1).describe('Leverage multiplier (default 1 = no leverage)'),
      },
      async ({coin, size_usd, stop_pct, leverage}) =>
        await perpOpen('short', coin, size_usd, stop_pct, leverage),
    )

    server.tool(
      'perp_close',
      'Close an open perp position (and cancel any orders for the coin) via reduce-only IOC market order. Pass coin to close a specific position; omit to close ALL perp positions. Use this for manual-trade lifecycle exit or emergency cleanup.',
      {
        coin: z.string().default('').describe('Coin to close (e.g. BTC) — omit/empty to close ALL'),
      },
      async ({coin}) => {
        try {
          const {loadCredentials, hasFullSetup, getAccountAddress} = await import('../lib/credentials.js')
          if (!hasFullSetup()) {
            return {content: [{type: 'text' as const, text: 'Error: Account not set up. Run: rift auth setup'}], isError: true}
          }
          const creds = loadCredentials()
          if (!creds) {
            return {content: [{type: 'text' as const, text: 'Error: No credentials'}], isError: true}
          }
          // close-all reads HL_PRIVATE_KEY from env (for security — not from
          // CLI args). The subprocess inherits process.env so set it here.
          // Also needs --account for HL queries (agent address ≠ main address).
          process.env.HYPERLIQUID_PRIVATE_KEY = creds.private_key
          try {
            const args: string[] = ['--account', getAccountAddress(creds)]
            if (coin) args.push('--coin', coin)
            const result = await collectResult('close-all', args)
            return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
          } finally {
            delete process.env.HYPERLIQUID_PRIVATE_KEY
          }
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 13: Spot Trading
    // ═══════════════════════════════════════════

    server.tool(
      'buy',
      'Buy a token on the Hyperliquid spot market. Simple spot purchase — no leverage, no margin. 1% builder fee on sell side only.',
      {
        coin: safeCoin.describe('Token to buy (e.g. HYPE, ETH, BTC)'),
        amount: z.number().default(0).describe('USDC amount to spend'),
        size: z.number().default(0).describe('Token amount to buy (alternative to amount)'),
      },
      async ({coin, amount, size}) => {
        try {
          const args = [coin]
          if (amount > 0) args.push('--amount', String(amount))
          if (size > 0) args.push('--size', String(size))
          const result = await collectResult('buy', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'sell',
      'Sell a token from spot holdings. 1% builder fee applies on sell side.',
      {
        coin: safeCoin.describe('Token to sell (e.g. HYPE, ETH, BTC)'),
        amount: z.number().default(0).describe('Token amount to sell (0 = all)'),
        pct: z.number().default(0).describe('Percentage to sell (e.g. 50 = half)'),
      },
      async ({coin, amount, pct}) => {
        try {
          const args = [coin]
          if (amount > 0) args.push('--amount', String(amount))
          if (pct > 0) args.push('--pct', String(pct))
          const result = await collectResult('sell', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ── Canonical spot-namespace aliases. The `buy` / `sell` tools above
    // remain registered for back-compat with existing AI prompts; the
    // `spot_buy` / `spot_sell` names disambiguate vs perp trading on the
    // AI agent surface (`spot_sell BTC` = exit BTC holdings; `perp_short
    // BTC` = open a short — two very different actions).
    server.tool(
      'spot_buy',
      'Buy a token on the Hyperliquid SPOT market. Same as the legacy `buy` tool — disambiguates vs perp trading. Simple purchase, no leverage. 1% builder fee on sell side only.',
      {
        coin: safeCoin.describe('Token to buy (e.g. HYPE, ETH, BTC — auto-resolves to UBTC/UETH on HL spot)'),
        amount: z.number().default(0).describe('USDC amount to spend'),
        size: z.number().default(0).describe('Token amount to buy (alternative to amount)'),
      },
      async ({coin, amount, size}) => {
        try {
          const args = [coin]
          if (amount > 0) args.push('--amount', String(amount))
          if (size > 0) args.push('--size', String(size))
          const result = await collectResult('buy', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'spot_sell',
      'Sell a token from SPOT holdings back to USDC. Same as the legacy `sell` tool — disambiguates vs perp trading. 1% builder fee applies.',
      {
        coin: safeCoin.describe('Token to sell (e.g. HYPE, ETH, BTC)'),
        amount: z.number().default(0).describe('Token amount to sell (0 = all)'),
        pct: z.number().default(0).describe('Percentage to sell (e.g. 50 = half)'),
      },
      async ({coin, amount, pct}) => {
        try {
          const args = [coin]
          if (amount > 0) args.push('--amount', String(amount))
          if (pct > 0) args.push('--pct', String(pct))
          const result = await collectResult('sell', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'holdings',
      'View spot token holdings with current prices, USD values, and P&L.',
      {},
      async () => {
        try {
          const result = await collectResult('holdings', [])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'balance',
      'Show combined spot and perps wallet balances — total equity across both accounts.',
      {},
      async () => {
        try {
          const result = await collectResult('balance', [])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'transfer',
      'Transfer USDC between spot and perps on Hyperliquid. Requires wallet approval via push notification.',
      {
        amount: z.number().describe('USDC amount to transfer'),
        direction: z.enum(['to-perps', 'to-spot']).default('to-perps').describe('Transfer direction'),
      },
      async ({amount, direction}) => {
        try {
          const args = [String(amount)]
          if (direction === 'to-spot') args.push('--to-spot')
          else args.push('--to-perps')
          const result = await collectResult('transfer', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'withdraw',
      'Withdraw USDC from Hyperliquid to Arbitrum. Requires wallet approval via push notification. $1 fee.',
      {
        amount: z.number().describe('USDC amount to withdraw'),
      },
      async ({amount}) => {
        try {
          const result = await collectResult('withdraw', [String(amount)])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'deposit',
      'Deposit USDC from Arbitrum to Hyperliquid via bridge. Requires 2 wallet approvals (permit + transaction). Minimum 5 USDC.',
      {
        amount: z.number().describe('USDC amount to deposit (minimum 5)'),
      },
      async ({amount}) => {
        try {
          const result = await collectResult('deposit', [String(amount)])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 14: AI Intelligence Layer
    // ═══════════════════════════════════════════

    server.tool(
      'state',
      'Full project snapshot — registered strategies, running algo sessions, validated edge, recent lessons, alerts, auth status, and data inventory. Call this first in any new conversation for instant context.',
      {},
      async () => {
        try {
          const result = await collectResult('state', [])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'lessons',
      'Query lessons learned from past research and trading. Prevents repeating failed experiments. Auto-recorded after research and walk-forward.',
      {
        coin: safeCoin.default('').describe('Filter by coin'),
        strategy: safeStrategy.default('').describe('Filter by strategy'),
        limit: z.number().default(20).describe('Number of lessons'),
      },
      async ({coin, strategy, limit}) => {
        try {
          const args = ['--limit', String(limit)]
          if (coin) args.push('--coin', coin)
          if (strategy) args.push('--strategy', strategy)
          const result = await collectResult('lessons', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'add_lesson',
      'Manually record a lesson learned from research or trading.',
      {
        coin: z.string().describe('Coin tested'),
        approach: z.string().describe('What was tried'),
        result: z.enum(['pass', 'fail']).describe('Outcome'),
        reason: z.string().default('').describe('Why it passed/failed'),
      },
      async ({coin, approach, result, reason}) => {
        try {
          const args = ['--coin', coin, '--approach', approach, '--result', result]
          if (reason) args.push('--reason', reason)
          const res = await collectResult('add-lesson', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(res), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'verify',
      'Compare strategy performance vs buy-and-hold on a specific date range. Shows alpha (excess return) and verdict.',
      {
        strategy: safeStrategy.describe('Strategy name'),
        pair: safePair.default('BTC').describe('Trading pair'),
        from: z.string().default('').describe('Start date YYYY-MM-DD'),
        to: z.string().default('').describe('End date YYYY-MM-DD'),
      },
      async ({strategy, pair, from: fromDate, to: toDate}) => {
        try {
          const args = [strategy, '--pair', pair]
          if (fromDate) args.push('--from', fromDate)
          if (toDate) args.push('--to', toDate)
          const result = await collectResult('verify', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'scan',
      'Scan all indicators for predictive power against forward returns. Ranks by information coefficient (Spearman). Discovers edge from raw data without writing a strategy.',
      {
        pair: safePair.default('BTC').describe('Trading pair'),
        timeframe: z.string().default('1h').describe('Timeframe'),
        forward: z.string().default('4h').describe('Forward return horizon (e.g. 1h, 4h, 24h)'),
      },
      async ({pair, timeframe, forward}) => {
        try {
          const result = await collectResult('scan', ['--pair', pair, '--tf', timeframe, '--forward', forward])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'data_inventory',
      'Show all available data — coins, timeframes, candle counts, date ranges. Includes crypto and TradFi tickers.',
      {},
      async () => {
        try {
          const result = await collectResult('data-inventory', [])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'history',
      'List past algo trading sessions with P&L, trades, and outcomes.',
      {
        limit: z.number().default(10).describe('Number of sessions'),
        strategy: safeStrategy.default('').describe('Filter by strategy'),
      },
      async ({limit, strategy}) => {
        try {
          const args = ['--limit', String(limit)]
          if (strategy) args.push('--strategy', strategy)
          const result = await collectResult('history', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 15: Position Management
    // ═══════════════════════════════════════════

    server.tool(
      'close_position',
      'Close position on a running algo trading session. Sends command via IPC — daemon closes at market.',
      {
        strategy: safeStrategy.describe('Strategy name'),
        pair: safePair.default('BTC-PERP').describe('Trading pair'),
      },
      async ({strategy, pair}) => {
        try {
          const result = await collectResult('close-position', [strategy, '--pair', pair])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'tighten_stop',
      'Update stop loss price on a running algo session.',
      {
        strategy: safeStrategy.describe('Strategy name'),
        pair: safePair.default('BTC-PERP').describe('Trading pair'),
        price: z.number().describe('New stop loss price'),
      },
      async ({strategy, pair, price}) => {
        try {
          const result = await collectResult('tighten-stop', [strategy, '--pair', pair, '--price', String(price)])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'reduce_position',
      'Reduce position size on a running algo session (e.g. close half).',
      {
        strategy: safeStrategy.describe('Strategy name'),
        pair: safePair.default('BTC-PERP').describe('Trading pair'),
        pct: z.number().default(50).describe('Percentage to close (50 = half)'),
      },
      async ({strategy, pair, pct}) => {
        try {
          const result = await collectResult('reduce-position', [strategy, '--pair', pair, '--pct', String(pct)])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // ═══════════════════════════════════════════
    //  GROUP 16: Watchdog
    // ═══════════════════════════════════════════

    server.tool(
      'watchdog_events',
      'Query recent market watchdog events — funding extremes, premium divergence, volume spikes.',
      {
        since: z.string().default('24h').describe('Time window (e.g. 1h, 24h, 7d)'),
        coin: safeCoin.default('').describe('Filter by coin'),
      },
      async ({since, coin}) => {
        try {
          const args = ['--since', since]
          if (coin) args.push('--coin', coin)
          const result = await collectResult('watchdog-events', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'guide',
      'Print the 9-step research-to-trade journey. Start here if new to RIFT.',
      {},
      async () => {
        try {
          const result = await collectResult('guide', [])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'auth_setup',
      'Set up wallet authentication non-interactively. Works from any interface (MCP, mobile, web).',
      {
        key: z.string().describe('Hyperliquid API wallet private key (0x-prefixed)'),
        account: z.string().default('').describe('Main wallet address (optional, derived from key if empty)'),
      },
      async ({key, account}) => {
        try {
          const args = ['setup', '--key', key]
          if (account) args.push('--account', account)
          const result = await collectResult('auth', args)
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    server.tool(
      'auth_status',
      'Check wallet authentication status.',
      {},
      async () => {
        try {
          const result = await collectResult('auth', ['status'])
          return {content: [{type: 'text' as const, text: JSON.stringify(cleanResult(result), null, 2)}]}
        } catch (error: any) {
          return {content: [{type: 'text' as const, text: `Error: ${error.message}`}], isError: true}
        }
      },
    )

    // Connect via stdio transport
    const transport = new StdioServerTransport()

    if (flags.debug) {
      console.error('RIFT MCP server starting on stdio')
    }

    await server.connect(transport)
  }
}
