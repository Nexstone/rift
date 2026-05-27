# CLI reference

Auto-generated from `rift --help` and `rift <command> --help`.

Regenerate after CLI changes:

    bash scripts/gen_cli_reference.sh

For commands not listed here, see `rift more` — it surfaces every engine command,
including the ones without a top-level `rift <cmd>` wrapper.

---

## Top-level

```
RIFT — Research / Iteration / Forecast / Trade

VERSION
  @rift/cli/0.1.0 darwin-arm64 node-v24.14.0

USAGE
  $ rift [COMMAND]

TOPICS
  collect     Start the persistent data collector — builds historical data 24/7
  data        Fetch and cache candle data from Hyperliquid
  portfolio   Show recent portfolio alerts
  setup       Set up proxy for Hyperliquid API access
  strategies  List available trading strategies

COMMANDS
  algo                Algo trading — run automated strategies on Hyperliquid
                      with real orders
  audit               Export compliance-grade audit trail of all live trades
  auth                Set up wallet credentials for Hyperliquid live trading
  auth-status         Show RIFT auth state — API wallet + recent authorization
                      tokens.
  backtest            Run a backtest on cached candle data
  cashcarry           Backtest a cash-and-carry (spot vs perp) delta neutral
                      strategy
  compare             Compare multiple strategies head-to-head
  config              View and set RIFT configuration
  cost                Estimate pre-trade cost for a hypothetical trade: fees +
                      funding + impact + slippage
  cross-asset         Cross-asset correlation matrix + lead-lag +
                      beta-vs-benchmark
  data-inventory      Inventory of locally cached candles, funding, fills —
                      counts + freshness
  deltaneutral        Same-asset delta neutral backtest (long spot + short perp
                      on Hyperliquid)
  deposit             Deposit USDC from Arbitrum to Hyperliquid
  doctor              Check system health and diagnose issues
  funding-browser     Browse funding rates across coins — current + window stats
                      + extremes
  guide               Print the RIFT research-to-trade journey as a quick
                      reference
  help                Display help for rift.
  home                RIFT — Research · Iteration · Forecast · Trade
  init                Set up RIFT — wallet, sample data, and first backtest in
                      under 60 seconds
  install             Install a community strategy from GitHub
  interactive         Launch interactive mode
  lessons             Show captured trading lessons (post-trade learnings)
  montecarlo          Run Monte Carlo simulation to test how much of your
                      backtest was luck vs edge
  more                Discover and run every engine command — including those
                      without a top-level `rift <cmd>` wrapper
  new                 Scaffold a new trading strategy
  pairs               Backtest a pairs/spread trade between two assets (e.g.
                      BTC/ETH)
  pairs-backtest      Backtest a pairs/spread trade (e.g. BTC/ETH spread)
  portfolio-backtest  Run a multi-strategy portfolio backtest from a
                      portfolio.yaml
  portfolio-matrix    Strategy × pair P&L matrix, correlation matrix, and regime
                      analysis
  research            Research Lab — discover, test, build, optimize, and
                      compare strategies
  scout               Scan the market and find trading opportunities ranked by
                      confluence
  serve               Start RIFT as an MCP server for AI agent integration
  simulate            Paper trade on real mainnet prices — no real money, real
                      data
  sweep               Run a parameter sweep to find optimal strategy settings
  test-trade          Place a minimum-size test trade to verify exchange
                      connectivity
  trade               Place a manual trade with stop loss and live monitoring
  transfer            Transfer USDC between spot and perps on Hyperliquid
  verify              Verify a strategy beats buy-and-hold over a date range —
                      sanity check before going live
  walkforward         Run walk-forward analysis to test strategy robustness
  withdraw            Withdraw USDC from Hyperliquid to Arbitrum

```

---

## Commands

### `rift algo`

```
Algo trading — run automated strategies on Hyperliquid with real orders

USAGE
  $ rift algo [STRATEGY] [--pair <value>] [--tf <value>] [--equity
    <value>] [--all]

ARGUMENTS
  [STRATEGY]  Strategy name, or "status"/"stop"

FLAGS
  --all             Stop all running sessions
  --equity=<value>  Starting equity (0 = auto)
  --pair=<value>    [default: BTC] Ticker symbol (e.g. BTC, ETH, SOL)
  --tf=<value>      Timeframe

DESCRIPTION
  Algo trading — run automated strategies on Hyperliquid with real orders

EXAMPLES
  $ rift algo btc_funding_fade --pair BTC

  $ rift algo status

  $ rift algo stop

```

### `rift audit`

```
Export compliance-grade audit trail of all live trades

USAGE
  $ rift audit [--export <value>] [--last <value>] [--strategy
    <value>] [--output <value>] [--json]

FLAGS
  --export=<value>    [default: csv] Export format: csv or json
  --json              Emit raw JSON only
  --last=<value>      [default: 30] Days of history to include
  --output=<value>    Custom output path
  --strategy=<value>  Filter by strategy name

DESCRIPTION
  Export compliance-grade audit trail of all live trades

EXAMPLES
  $ rift audit

  $ rift audit --export json --last 90

  $ rift audit --strategy trend_follow --output ./audit.csv

```

### `rift auth`

```
Set up wallet credentials for Hyperliquid live trading

USAGE
  $ rift auth [ACTION]

ARGUMENTS
  [ACTION]  Action: setup, status, or reset

DESCRIPTION
  Set up wallet credentials for Hyperliquid live trading

EXAMPLES
  $ rift auth setup

  $ rift auth status

  $ rift auth reset

```

### `rift auth-status`

```
Show RIFT auth state — API wallet + recent authorization tokens.

USAGE
  $ rift auth-status

DESCRIPTION
  Show RIFT auth state — API wallet + recent authorization tokens.

EXAMPLES
  $ rift auth-status

```

### `rift backtest`

```
Run a backtest on cached candle data

USAGE
  $ rift backtest STRATEGY [--pair <value>] [--tf <value>] [--equity
    <value>] [--leverage <value>] [--export csv|json] [--analyze] [--all-pairs]
    [--top <value>]

ARGUMENTS
  STRATEGY  Strategy name

FLAGS
  --all-pairs         Run across top pairs and rank results
  --analyze           AI-powered analysis of backtest results
  --equity=<value>    [default: 10000] Starting equity in USDC
  --export=<option>   Export results to file (csv or json)
                      <options: csv|json>
  --leverage=<value>  [default: 1] Leverage multiplier
  --pair=<value>      [default: BTC-PERP] Trading pair
  --tf=<value>        Timeframe (auto-detected from strategy if omitted)
  --top=<value>       [default: 10] Number of top pairs for --all-pairs

DESCRIPTION
  Run a backtest on cached candle data

EXAMPLES
  $ rift backtest btc_funding_fade --pair BTC --tf 1h

  $ rift backtest my_strategy --pair BTC --tf 1h --equity 50000

```

### `rift cashcarry`

```
Backtest a cash-and-carry (spot vs perp) delta neutral strategy

USAGE
  $ rift cashcarry [--asset <value>] [--tf <value>] [--equity <value>]
    [--entry-premium <value>] [--exit-premium <value>]

FLAGS
  --asset=<value>          [default: BTC] Asset
  --entry-premium=<value>  [default: 0] Enter when premium > this
  --equity=<value>         [default: 10000] Starting equity
  --exit-premium=<value>   [default: -0.0005] Exit when premium < this
  --tf=<value>             [default: 1h] Timeframe

DESCRIPTION
  Backtest a cash-and-carry (spot vs perp) delta neutral strategy

EXAMPLES
  $ rift cashcarry --asset BTC --tf 1h

  $ rift cashcarry --asset ETH --tf 1h --equity 50000

```

### `rift compare`

```
Compare multiple strategies head-to-head

USAGE
  $ rift compare STRATEGIES [--pair <value>] [--tf <value>] [--equity
    <value>] [--leverage <value>]

ARGUMENTS
  STRATEGIES  Comma-separated strategy names

FLAGS
  --equity=<value>    [default: 10000] Starting equity in USDC
  --leverage=<value>  [default: 1] Leverage multiplier
  --pair=<value>      [default: BTC-PERP] Trading pair
  --tf=<value>        [default: 15m] Timeframe

DESCRIPTION
  Compare multiple strategies head-to-head

EXAMPLES
  $ rift compare btc_funding_fade,my_strategy --pair BTC

```

### `rift config`

```
View and set RIFT configuration

USAGE
  $ rift config [ACTION...] [KEY...] [VALUE...]

ARGUMENTS
  [ACTION...]  (list|get|set) Action: list, get, set
  [KEY...]     Config key (dot-notation, e.g. ai.api_key)
  [VALUE...]   Value to set

DESCRIPTION
  View and set RIFT configuration

EXAMPLES
  $ rift config list

  $ rift config set ai.api_key sk-ant-...

  $ rift config get ai.api_key

  $ rift config set ai.model claude-sonnet-4-20250514

```

### `rift cost`

```
Estimate pre-trade cost for a hypothetical trade: fees + funding + impact + slippage

USAGE
  $ rift cost PAIR NOTIONAL [--side <value>] [--tf <value>]
    [--hold <value>] [--maker] [--spot] [--no-builder-fee] [--tier-vol-14d
    <value>] [--json]

ARGUMENTS
  PAIR      Trading pair (e.g. BTC, ETH-PERP)
  NOTIONAL  Trade size in USD notional (e.g. 50000)

FLAGS
  --hold=<value>          [default: 0] Holding period in hours (for funding
                          accrual)
  --json                  Emit raw JSON instead of human format
  --maker                 Treat as maker (post-only)
  --no-builder-fee        Exclude RIFT builder fee
  --side=<value>          [default: buy] buy / sell / long / short
  --spot                  Treat as spot trade instead of perp
  --tf=<value>            [default: 1h] Candle interval for ADV / vol calc
  --tier-vol-14d=<value>  [default: 0] Your 14d HL volume USD (fee-tier lookup)

DESCRIPTION
  Estimate pre-trade cost for a hypothetical trade: fees + funding + impact +
  slippage

EXAMPLES
  $ rift cost BTC 50000

  $ rift cost ETH 10000 --side sell --hold 24

  $ rift cost BTC 100000 --maker --tier-vol-14d 5000000

```

### `rift cross-asset`

```
Cross-asset correlation matrix + lead-lag + beta-vs-benchmark

USAGE
  $ rift cross-asset [--coins <value>] [--tf <value>] [--lookback
    <value>] [--benchmark <value>] [--max-lag <value>] [--json]

FLAGS
  --benchmark=<value>  [default: BTC] Beta-vs-benchmark coin
  --coins=<value>      [default: BTC,ETH,SOL,SUI,AVAX,NEAR,LINK,DOGE]
                       Comma-separated coin list
  --json               Emit raw JSON only
  --lookback=<value>   [default: 720] Candles to use (720 = 30d at 1h)
  --max-lag=<value>    [default: 6] Lead-lag search window (candles)
  --tf=<value>         [default: 1h] Timeframe

DESCRIPTION
  Cross-asset correlation matrix + lead-lag + beta-vs-benchmark

EXAMPLES
  $ rift cross-asset

  $ rift cross-asset --coins BTC,ETH,SOL --tf 4h --benchmark BTC

```

### `rift data-inventory`

```
Inventory of locally cached candles, funding, fills — counts + freshness

USAGE
  $ rift data-inventory [--json]

FLAGS
  --json  Emit raw JSON only

DESCRIPTION
  Inventory of locally cached candles, funding, fills — counts + freshness

EXAMPLES
  $ rift data-inventory

  $ rift data-inventory --json

```

### `rift deltaneutral`

```
Same-asset delta neutral backtest (long spot + short perp on Hyperliquid)

USAGE
  $ rift deltaneutral [--asset <value>] [--tf <value>] [--equity <value>]
    [--entry-basis <value>] [--exit-basis <value>] [--always-on]

FLAGS
  --always-on            Always hold the carry position
  --asset=<value>        [default: HYPE] Asset with spot + perp on Hyperliquid
  --entry-basis=<value>  [default: 0.001] Enter when basis > this
  --equity=<value>       [default: 10000] Starting equity
  --exit-basis=<value>   [default: -0.0005] Exit when basis < this
  --tf=<value>           [default: 1h] Timeframe

DESCRIPTION
  Same-asset delta neutral backtest (long spot + short perp on Hyperliquid)

EXAMPLES
  $ rift deltaneutral --asset HYPE

  $ rift deltaneutral --asset HYPE --always-on

  $ rift deltaneutral --asset HYPE --entry-basis 0.002

```

### `rift deposit`

```
Deposit USDC from Arbitrum to Hyperliquid

USAGE
  $ rift deposit AMOUNT

ARGUMENTS
  AMOUNT  USDC amount to deposit (minimum 5)

DESCRIPTION
  Deposit USDC from Arbitrum to Hyperliquid

EXAMPLES
  $ rift deposit 100

```

### `rift doctor`

```
Check system health and diagnose issues

USAGE
  $ rift doctor

DESCRIPTION
  Check system health and diagnose issues

EXAMPLES
  $ rift doctor

```

### `rift funding-browser`

```
Browse funding rates across coins — current + window stats + extremes

USAGE
  $ rift funding-browser [--coins <value>] [--top <value>] [--days <value>]
    [--json]

FLAGS
  --coins=<value>  Comma-separated coin list (default: all cached)
  --days=<value>   [default: 7] History window in days
  --json           Emit raw JSON only
  --top=<value>    [default: 20] Number of coins to show, ranked by current
                   funding

DESCRIPTION
  Browse funding rates across coins — current + window stats + extremes

EXAMPLES
  $ rift funding-browser

  $ rift funding-browser --top 50

  $ rift funding-browser --coins BTC,ETH,SOL --days 30

```

### `rift guide`

```
Print the RIFT research-to-trade journey as a quick reference

USAGE
  $ rift guide

DESCRIPTION
  Print the RIFT research-to-trade journey as a quick reference

EXAMPLES
  $ rift guide

```

### `rift help`

```
RIFT — Research / Iteration / Forecast / Trade

VERSION
  @rift/cli/0.1.0 darwin-arm64 node-v24.14.0

USAGE
  $ rift [COMMAND]

TOPICS
  collect     Start the persistent data collector — builds historical data 24/7
  data        Fetch and cache candle data from Hyperliquid
  portfolio   Show recent portfolio alerts
  setup       Set up proxy for Hyperliquid API access
  strategies  List available trading strategies

COMMANDS
  algo                Algo trading — run automated strategies on Hyperliquid
                      with real orders
  audit               Export compliance-grade audit trail of all live trades
  auth                Set up wallet credentials for Hyperliquid live trading
  auth-status         Show RIFT auth state — API wallet + recent authorization
                      tokens.
  backtest            Run a backtest on cached candle data
  cashcarry           Backtest a cash-and-carry (spot vs perp) delta neutral
                      strategy
  compare             Compare multiple strategies head-to-head
  config              View and set RIFT configuration
  cost                Estimate pre-trade cost for a hypothetical trade: fees +
                      funding + impact + slippage
  cross-asset         Cross-asset correlation matrix + lead-lag +
                      beta-vs-benchmark
  data-inventory      Inventory of locally cached candles, funding, fills —
                      counts + freshness
  deltaneutral        Same-asset delta neutral backtest (long spot + short perp
                      on Hyperliquid)
  deposit             Deposit USDC from Arbitrum to Hyperliquid
  doctor              Check system health and diagnose issues
  funding-browser     Browse funding rates across coins — current + window stats
                      + extremes
  guide               Print the RIFT research-to-trade journey as a quick
                      reference
  help                Display help for rift.
  home                RIFT — Research · Iteration · Forecast · Trade
  init                Set up RIFT — wallet, sample data, and first backtest in
                      under 60 seconds
  install             Install a community strategy from GitHub
  interactive         Launch interactive mode
  lessons             Show captured trading lessons (post-trade learnings)
  montecarlo          Run Monte Carlo simulation to test how much of your
                      backtest was luck vs edge
  more                Discover and run every engine command — including those
                      without a top-level `rift <cmd>` wrapper
  new                 Scaffold a new trading strategy
  pairs               Backtest a pairs/spread trade between two assets (e.g.
                      BTC/ETH)
  pairs-backtest      Backtest a pairs/spread trade (e.g. BTC/ETH spread)
  portfolio-backtest  Run a multi-strategy portfolio backtest from a
                      portfolio.yaml
  portfolio-matrix    Strategy × pair P&L matrix, correlation matrix, and regime
                      analysis
  research            Research Lab — discover, test, build, optimize, and
                      compare strategies
  scout               Scan the market and find trading opportunities ranked by
                      confluence
  serve               Start RIFT as an MCP server for AI agent integration
  simulate            Paper trade on real mainnet prices — no real money, real
                      data
  sweep               Run a parameter sweep to find optimal strategy settings
  test-trade          Place a minimum-size test trade to verify exchange
                      connectivity
  trade               Place a manual trade with stop loss and live monitoring
  transfer            Transfer USDC between spot and perps on Hyperliquid
  verify              Verify a strategy beats buy-and-hold over a date range —
                      sanity check before going live
  walkforward         Run walk-forward analysis to test strategy robustness
  withdraw            Withdraw USDC from Hyperliquid to Arbitrum

```

### `rift home`

```
RIFT — Research · Iteration · Forecast · Trade

USAGE
  $ rift home

DESCRIPTION
  RIFT — Research · Iteration · Forecast · Trade

```

### `rift init`

```
Set up RIFT — wallet, sample data, and first backtest in under 60 seconds

USAGE
  $ rift init

DESCRIPTION
  Set up RIFT — wallet, sample data, and first backtest in under 60 seconds

EXAMPLES
  $ rift init

```

### `rift install`

```
Install a community strategy from GitHub

USAGE
  $ rift install SOURCE

ARGUMENTS
  SOURCE  GitHub repo URL or user/repo shorthand

DESCRIPTION
  Install a community strategy from GitHub

EXAMPLES
  $ rift install https://github.com/user/rift-strategy-bollinger

  $ rift install user/rift-strategy-macd

```

### `rift interactive`

```
Launch interactive mode

USAGE
  $ rift interactive

DESCRIPTION
  Launch interactive mode

EXAMPLES
  $ rift interactive

```

### `rift lessons`

```
Show captured trading lessons (post-trade learnings)

USAGE
  $ rift lessons [--coin <value>] [--strategy <value>] [--limit
    <value>] [--json]

FLAGS
  --coin=<value>      Filter by coin
  --json              Emit raw JSON only
  --limit=<value>     [default: 20] Number of lessons to show
  --strategy=<value>  Filter by strategy name

DESCRIPTION
  Show captured trading lessons (post-trade learnings)

EXAMPLES
  $ rift lessons

  $ rift lessons --strategy trend_follow

```

### `rift montecarlo`

```
Run Monte Carlo simulation to test how much of your backtest was luck vs edge

USAGE
  $ rift montecarlo STRATEGY [--pair <value>] [--tf <value>] [--runs
    <value>] [--equity <value>] [--leverage <value>]

ARGUMENTS
  STRATEGY  Strategy name

FLAGS
  --equity=<value>    [default: 10000] Starting equity
  --leverage=<value>  [default: 1] Leverage multiplier
  --pair=<value>      [default: BTC-PERP] Trading pair
  --runs=<value>      [default: 10000] Number of simulations
  --tf=<value>        [default: 1h] Timeframe

DESCRIPTION
  Run Monte Carlo simulation to test how much of your backtest was luck vs edge

EXAMPLES
  $ rift montecarlo btc_funding_fade --pair BTC --tf 1h

  $ rift montecarlo btc_funding_fade --pair BTC --runs 50000

```

### `rift more`

```
Discover and run every engine command — including those without a top-level `rift <cmd>` wrapper

USAGE
  $ rift more [COMMAND...]

ARGUMENTS
  [COMMAND...]  Engine command name (omit to list)

DESCRIPTION
  Discover and run every engine command — including those without a top-level
  `rift <cmd>` wrapper

EXAMPLES
  $ rift more                          # list all engine commands by category

  $ rift more funding-browser BTC      # run the funding-browser command

  $ rift more verify <bundle-id>       # verify a sealed bundle

```

### `rift new`

```
Scaffold a new trading strategy

USAGE
  $ rift new NAME

ARGUMENTS
  NAME  Strategy name (lowercase, hyphens ok)

DESCRIPTION
  Scaffold a new trading strategy

EXAMPLES
  $ rift new my-strategy

  $ rift new bollinger-breakout

```

### `rift pairs`

```
Backtest a pairs/spread trade between two assets (e.g. BTC/ETH)

USAGE
  $ rift pairs [--a <value>] [--b <value>] [--tf <value>] [--equity
    <value>] [--lookback <value>] [--entry-z <value>] [--exit-z <value>]
    [--stop-z <value>] [--max-hold <value>]

FLAGS
  --a=<value>         [default: BTC] First asset
  --b=<value>         [default: ETH] Second asset
  --entry-z=<value>   [default: 2.0] Z-score entry threshold
  --equity=<value>    [default: 10000] Starting equity
  --exit-z=<value>    [default: 0.5] Z-score exit threshold
  --lookback=<value>  [default: 168] Rolling z-score window (candles)
  --max-hold=<value>  [default: 72] Max hold time (candles)
  --stop-z=<value>    [default: 4.0] Z-score stop loss
  --tf=<value>        [default: 1h] Timeframe

DESCRIPTION
  Backtest a pairs/spread trade between two assets (e.g. BTC/ETH)

EXAMPLES
  $ rift pairs --a BTC --b ETH --tf 1h

  $ rift pairs --a BTC --b ETH --entry-z 2.5 --lookback 336

```

### `rift pairs-backtest`

```
Backtest a pairs/spread trade (e.g. BTC/ETH spread)

USAGE
  $ rift pairs-backtest [--a <value>] [--b <value>] [--tf <value>] [--equity
    <value>] [--lookback <value>] [--entry-z <value>] [--exit-z <value>]
    [--stop-z <value>] [--max-hold <value>] [--json]

FLAGS
  --a=<value>         [default: BTC] First asset
  --b=<value>         [default: ETH] Second asset
  --entry-z=<value>   [default: 2.0] Z-score entry threshold
  --equity=<value>    [default: 10000] Starting equity USD
  --exit-z=<value>    [default: 0.5] Z-score exit threshold
  --json              Emit raw JSON only
  --lookback=<value>  [default: 168] Rolling window for z-score (hours)
  --max-hold=<value>  [default: 72] Max hold time in candles
  --stop-z=<value>    [default: 4.0] Z-score stop loss
  --tf=<value>        [default: 1h] Candle interval

DESCRIPTION
  Backtest a pairs/spread trade (e.g. BTC/ETH spread)

EXAMPLES
  $ rift pairs-backtest

  $ rift pairs-backtest --a BTC --b ETH --tf 1h --entry-z 2.0

```

### `rift portfolio-backtest`

```
Run a multi-strategy portfolio backtest from a portfolio.yaml

USAGE
  $ rift portfolio-backtest CONFIG [--strategies-dir <value>] [--json]

ARGUMENTS
  CONFIG  Path to portfolio.yaml

FLAGS
  --json                    Emit raw JSON only
  --strategies-dir=<value>  Directory with strategy .py files

DESCRIPTION
  Run a multi-strategy portfolio backtest from a portfolio.yaml

EXAMPLES
  $ rift portfolio-backtest portfolio.yaml

  $ rift portfolio-backtest config.yaml --strategies-dir ./strategies

```

### `rift portfolio-matrix`

```
Strategy × pair P&L matrix, correlation matrix, and regime analysis

USAGE
  $ rift portfolio-matrix [--pairs <value>] [--strategies <value>] [--equity
    <value>] [--json]

FLAGS
  --equity=<value>      [default: 10000] Starting equity per strategy
  --json                Emit raw JSON only
  --pairs=<value>       [default: BTC,ETH,SOL] Comma-separated coins
  --strategies=<value>  Comma-separated strategies (auto-discovers if empty)

DESCRIPTION
  Strategy × pair P&L matrix, correlation matrix, and regime analysis

EXAMPLES
  $ rift portfolio-matrix

  $ rift portfolio-matrix --pairs BTC,ETH,SOL --strategies trend_follow,vol_breakout

```

### `rift research`

```
Research Lab — discover, test, build, optimize, and compare strategies

USAGE
  $ rift research [STRATEGY] [--pair <value>] [--tf <value>] [--equity
    <value>]

ARGUMENTS
  [STRATEGY]  Strategy name (interactive if omitted)

FLAGS
  --equity=<value>  [default: 10000] Starting equity
  --pair=<value>    [default: BTC] Ticker symbol (e.g. BTC, ETH, SOL)
  --tf=<value>      Timeframe (auto-detected if omitted)

DESCRIPTION
  Research Lab — discover, test, build, optimize, and compare strategies

EXAMPLES
  $ rift research

  $ rift research my_strategy --pair SUI

```

### `rift scout`

```
Scan the market and find trading opportunities ranked by confluence

USAGE
  $ rift scout [--top <value>] [--tf <value>] [--min <value>]
    [--soak <value>] [--no-soak]

FLAGS
  --min=<value>   [default: 2] Minimum confluence score
  --no-soak       Skip the websocket soak phase entirely (fastest, less
                  accurate)
  --soak=<value>  [default: 120] Seconds to collect live websocket data (default
                  120, lower = faster)
  --tf=<value>    [default: 1h] Timeframe (bias)
  --top=<value>   [default: 20] Number of coins to scan

DESCRIPTION
  Scan the market and find trading opportunities ranked by confluence

EXAMPLES
  $ rift scout

  $ rift scout --top 10

  $ rift scout --no-soak           # skip 120s websocket soak (faster, less accurate)

  $ rift scout --soak 30           # shorter soak window

```

### `rift serve`

```
Start RIFT as an MCP server for AI agent integration

USAGE
  $ rift serve [--debug]

FLAGS
  --debug  Enable debug logging to stderr

DESCRIPTION
  Start RIFT as an MCP server for AI agent integration

EXAMPLES
  $ rift serve

```

### `rift simulate`

```
Paper trade on real mainnet prices — no real money, real data

USAGE
  $ rift simulate [STRATEGY] [--pair <value>] [--tf <value>] [--equity
    <value>]

ARGUMENTS
  [STRATEGY]  Strategy name (interactive if omitted)

FLAGS
  --equity=<value>  [default: 10000] Starting equity in USDC
  --pair=<value>    [default: BTC] Ticker symbol (e.g. BTC, ETH, SOL)
  --tf=<value>      Timeframe (auto-detected if omitted)

DESCRIPTION
  Paper trade on real mainnet prices — no real money, real data

EXAMPLES
  $ rift simulate

  $ rift simulate btc_funding_fade --pair BTC --duration 4h

```

### `rift sweep`

```
Run a parameter sweep to find optimal strategy settings

USAGE
  $ rift sweep STRATEGY [--pair <value>] [--tf <value>] [--config
    <value>] [--equity <value>] [--leverage <value>] [--top <value>] [--rank
    sharpe|return|profit_factor]

ARGUMENTS
  STRATEGY  Strategy name

FLAGS
  --config=<value>    Path to sweep.yaml config file
  --equity=<value>    [default: 10000] Starting equity
  --leverage=<value>  [default: 1] Leverage multiplier
  --pair=<value>      [default: BTC-PERP] Trading pair
  --rank=<option>     [default: sharpe] Rank by: sharpe, return, or
                      profit_factor
                      <options: sharpe|return|profit_factor>
  --tf=<value>        [default: 1h] Timeframe
  --top=<value>       [default: 10] Number of top results to show

DESCRIPTION
  Run a parameter sweep to find optimal strategy settings

EXAMPLES
  $ rift sweep btc_funding_fade --pair BTC --tf 1h

  $ rift sweep btc_funding_fade --config strategies/btc_funding_fade/sweep.yaml

  $ rift sweep btc_funding_fade --pair BTC --rank sharpe --top 5

```

### `rift test-trade`

```
Place a minimum-size test trade to verify exchange connectivity

USAGE
  $ rift test-trade

DESCRIPTION
  Place a minimum-size test trade to verify exchange connectivity

EXAMPLES
  $ rift test-trade

```

### `rift trade`

```
Place a manual trade with stop loss and live monitoring

USAGE
  $ rift trade [PAIR] [DIRECTION] [--size <value>] [--stop <value>]
    [--leverage <value>]

ARGUMENTS
  [PAIR]       Coin (e.g. BTC, ETH, SOL)
  [DIRECTION]  long or short

FLAGS
  --leverage=<value>  [default: 1] Leverage
  --size=<value>      Position size in USD
  --stop=<value>      [default: 2] Stop loss % (default: 2)

DESCRIPTION
  Place a manual trade with stop loss and live monitoring

EXAMPLES
  $ rift trade ETH long --size 500

  $ rift trade SOL short --size 1000 --stop 3

  $ rift trade

```

### `rift transfer`

```
Transfer USDC between spot and perps on Hyperliquid

USAGE
  $ rift transfer AMOUNT [--to-perps] [--to-spot]

ARGUMENTS
  AMOUNT  USDC amount to transfer

FLAGS
  --to-perps  Transfer from Spot → Perps
  --to-spot   Transfer from Perps → Spot

DESCRIPTION
  Transfer USDC between spot and perps on Hyperliquid

EXAMPLES
  $ rift transfer 100 --to-perps

  $ rift transfer 50 --to-spot

```

### `rift verify`

```
Verify a strategy beats buy-and-hold over a date range — sanity check before going live

USAGE
  $ rift verify STRATEGY [--pair <value>] [--tf <value>] [--from
    <value>] [--to <value>] [--json]

ARGUMENTS
  STRATEGY  Strategy name

FLAGS
  --from=<value>  Start date YYYY-MM-DD
  --json          Emit raw JSON only
  --pair=<value>  [default: BTC] Trading pair
  --tf=<value>    Timeframe
  --to=<value>    End date YYYY-MM-DD

DESCRIPTION
  Verify a strategy beats buy-and-hold over a date range — sanity check before
  going live

EXAMPLES
  $ rift verify trend_follow

  $ rift verify trend_follow --pair BTC --tf 4h --from 2024-01-01 --to 2024-12-31

```

### `rift walkforward`

```
Run walk-forward analysis to test strategy robustness

USAGE
  $ rift walkforward STRATEGY [--pair <value>] [--tf <value>] [--wf
    <value>] [--equity <value>] [--leverage <value>]

ARGUMENTS
  STRATEGY  Strategy name

FLAGS
  --equity=<value>    [default: 10000] Starting equity per window
  --leverage=<value>  [default: 1] Leverage multiplier
  --pair=<value>      [default: BTC-PERP] Trading pair
  --tf=<value>        [default: 1h] Timeframe
  --wf=<value>        [default: 3m/1m] Walk-forward config: train/test (e.g.
                      3m/1m)

DESCRIPTION
  Run walk-forward analysis to test strategy robustness

EXAMPLES
  $ rift walkforward btc_funding_fade --pair BTC --tf 1h --wf 3m/1m

  $ rift walkforward btc_funding_fade --pair BTC --tf 1h --wf 6m/2m

```

### `rift withdraw`

```
Withdraw USDC from Hyperliquid to Arbitrum

USAGE
  $ rift withdraw AMOUNT [--destination <value>]

ARGUMENTS
  AMOUNT  USDC amount to withdraw

FLAGS
  --destination=<value>  Arbitrum address to receive USDC (defaults to main
                         wallet)

DESCRIPTION
  Withdraw USDC from Hyperliquid to Arbitrum

EXAMPLES
  $ rift withdraw 100

  $ rift withdraw 50 --destination 0x1234...

```

