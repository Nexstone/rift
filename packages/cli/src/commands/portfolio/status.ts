import {GatedCommand} from '../../lib/base-command.js'
import * as fs from 'node:fs'
import * as path from 'node:path'
import {getDataDir} from '../../lib/python-bridge.js'
import {
  green, red, yellow, cyan, bold, dim, greenBg,
  visLen, padEndVis, colorPnl, bar, gradeColor,
  boldBoxTop, boldBoxBottom, boldBoxDivider, boldBoxRow,
} from '../../lib/tui.js'

export default class PortfolioStatus extends GatedCommand {
  static override description = 'Show portfolio supervisor status and all managed strategies'

  static override examples = [
    '$ rift portfolio status',
  ]

  async run(): Promise<void> {
    const stateFile = path.join(getDataDir(), 'algo', 'supervisor.json')
    if (!fs.existsSync(stateFile)) {
      this.log(`\n  ${dim('No portfolio supervisor running.')}`)
      this.log(`  ${dim(`Run ${cyan('rift portfolio start')} to begin.`)}\n`)
      return
    }

    let state: Record<string, any>
    try {
      state = JSON.parse(fs.readFileSync(stateFile, 'utf-8'))
    } catch {
      this.log(`\n  ${red('✘')} Could not read supervisor state.\n`)
      return
    }

    // Verify supervisor is alive
    const pidFile = path.join(getDataDir(), 'algo', 'supervisor.pid')
    let alive = false
    if (fs.existsSync(pidFile)) {
      try {
        const pid = parseInt(fs.readFileSync(pidFile, 'utf-8').trim())
        process.kill(pid, 0)
        alive = true
      } catch {}
    }

    const portfolio = state.portfolio || {}
    const strategies = state.strategies || []
    const iw = 60
    const row = boldBoxRow(iw)

    this.log('')
    this.log(boldBoxTop(iw))

    const statusBadge = alive ? greenBg(' ● PORTFOLIO ') : dim(' ○ STOPPED ')
    const name = state.name || 'portfolio'
    const started = state.started_at || ''
    this.log(row(`  ${statusBadge}  ${bold(name)}  ${dim(started)}`))
    this.log(boldBoxDivider(iw))

    // Portfolio-level metrics
    const equity = portfolio.total_equity || 0
    const dd = (portfolio.drawdown_from_peak || 0) * 100
    const netExp = (portfolio.net_exposure || 0) * 100
    const grossExp = (portfolio.gross_exposure || 0) * 100

    this.log(row(''))
    this.log(row(`  ${dim('EQUITY')}  ${bold('$' + equity.toLocaleString())}  ${dim('DD')} ${dd > 5 ? red(dd.toFixed(1) + '%') : dim(dd.toFixed(1) + '%')}`))
    this.log(row(''))

    // Risk gauges
    this.log(row(`  ${dim('RISK')}`))
    this.log(row(`  Net exposure    ${bar(Math.abs(netExp), 100, 20)}  ${padEndVis(netExp.toFixed(0) + '%', 6)} / 100%`))
    this.log(row(`  Gross exposure  ${bar(grossExp, 150, 20)}  ${padEndVis(grossExp.toFixed(0) + '%', 6)} / 150%`))

    // Per-asset
    const perAsset = portfolio.per_asset || {}
    for (const [asset, exp] of Object.entries(perAsset)) {
      const expPct = ((exp as number) * 100)
      this.log(row(`  ${asset.padEnd(16)} ${bar(Math.abs(expPct), 80, 20)}  ${padEndVis(expPct.toFixed(0) + '%', 6)} / 80%`))
    }

    this.log(row(''))
    this.log(boldBoxDivider(iw))

    // Strategies
    this.log(row(`  ${dim('STRATEGIES')}`))
    this.log(row(''))

    for (const strat of strategies) {
      const statusIcon = strat.status === 'running' ? green('●')
        : strat.status === 'paused' ? yellow('●')
        : strat.status === 'scheduled_off' ? dim('○')
        : red('●')

      const name = (strat.name || '').padEnd(18)
      const pair = (strat.pair || '').padEnd(4)
      const grade = strat.health_grade ? gradeColor(strat.health_grade) : dim('-')
      const alloc = ((strat.allocation || 0) * 100).toFixed(0) + '%'

      if (strat.status === 'running' && strat.position) {
        const pos = strat.position
        const sideStr = pos.side === 'long' ? green('LONG') : red('SHORT')
        this.log(row(`  ${statusIcon} ${dim(name)} ${pair} ${sideStr}  ${colorPnl(strat.pnl_pct || 0, '%')}  ${grade}  ${dim(alloc)}`))
      } else if (strat.status === 'running') {
        this.log(row(`  ${statusIcon} ${dim(name)} ${pair} ${dim('FLAT')}  ${colorPnl(strat.pnl_pct || 0, '%')}  ${grade}  ${dim(alloc)}`))
      } else if (strat.status === 'scheduled_off') {
        const sched = typeof strat.schedule === 'object'
          ? `${strat.schedule.start}-${strat.schedule.stop}`
          : String(strat.schedule || '')
        this.log(row(`  ${statusIcon} ${dim(name)} ${pair} ${dim('OFF')}  ${dim(sched)}`))
      } else {
        this.log(row(`  ${statusIcon} ${dim(name)} ${pair} ${dim(strat.status || 'stopped')}`))
      }
    }

    this.log(row(''))

    // Recent alerts
    const alertsFile = path.join(getDataDir(), 'algo', 'alerts.log')
    if (fs.existsSync(alertsFile)) {
      try {
        const lines = fs.readFileSync(alertsFile, 'utf-8').trim().split('\n').filter(l => l.trim())
        const recent = lines.slice(-5)
        if (recent.length > 0) {
          this.log(boldBoxDivider(iw))
          this.log(row(`  ${dim('RECENT ALERTS')}`))
          this.log(row(''))
          for (const line of recent) {
            try {
              const alert = JSON.parse(line)
              const eventColor = alert.event === 'trade' ? cyan
                : alert.event.includes('health') ? yellow
                : alert.event.includes('drawdown') ? red
                : dim
              this.log(row(`  ${dim(alert.time)}  ${eventColor(alert.event.padEnd(16))} ${dim(alert.message?.slice(0, 30) || '')}`))
            } catch {}
          }
          this.log(row(''))
        }
      } catch {}
    }

    this.log(boldBoxBottom(iw))
    this.log('')
  }
}
