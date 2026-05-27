import {Args} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import * as fs from 'node:fs'
import * as path from 'node:path'
import {execSync} from 'node:child_process'
import {fileURLToPath} from 'node:url'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const red = (s: string) => `\x1b[31m${s}\x1b[0m`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`
const cyan = (s: string) => `\x1b[36m${s}\x1b[0m`

function findProjectRoot(): string {
  let dir = path.resolve(__dirname)
  for (let i = 0; i < 10; i++) {
    if (fs.existsSync(path.join(dir, 'engine', 'pyproject.toml'))) return dir
    dir = path.dirname(dir)
  }
  throw new Error('Cannot find project root')
}

export default class Install extends GatedCommand {
  static override description = 'Install a community strategy from GitHub'

  static override examples = [
    '$ rift install https://github.com/user/rift-strategy-bollinger',
    '$ rift install user/rift-strategy-macd',
  ]

  static override args = {
    source: Args.string({
      description: 'GitHub repo URL or user/repo shorthand',
      required: true,
    }),
  }

  async run(): Promise<void> {
    const {args} = await this.parse(Install)
    const source = args.source

    // Normalize to full URL
    let repoUrl = source
    if (!source.startsWith('http')) {
      repoUrl = `https://github.com/${source}`
    }

    // Extract repo name for strategy directory name
    const repoName = repoUrl.split('/').pop()?.replace(/\.git$/, '') || 'unknown'
    const strategyName = repoName.replace(/^rift-strategy-/, '').replace(/^rift-/, '')

    const projectRoot = findProjectRoot()
    const strategiesDir = path.join(projectRoot, 'strategies')
    const targetDir = path.join(strategiesDir, strategyName)

    if (fs.existsSync(targetDir)) {
      this.log(`  ${red('✘')} Strategy "${strategyName}" already exists at ${targetDir}`)
      this.log(`  ${dim('Remove it first: rm -rf ' + targetDir)}`)
      return
    }

    this.log('')
    this.log(`  Installing ${bold(strategyName)} from ${dim(repoUrl)}...`)
    this.log('')

    try {
      // Clone into strategies dir
      execSync(`git clone --depth 1 ${repoUrl} ${targetDir}`, {stdio: 'pipe'})

      // Remove .git dir (it's now part of our project)
      const gitDir = path.join(targetDir, '.git')
      if (fs.existsSync(gitDir)) {
        fs.rmSync(gitDir, {recursive: true})
      }

      // Check for strategy.py
      const hasStrategy = fs.existsSync(path.join(targetDir, 'strategy.py'))
      const hasPy = fs.readdirSync(targetDir).some(f => f.endsWith('.py'))

      if (hasStrategy || hasPy) {
        this.log(`  ${green('✔')} Strategy ${bold(strategyName)} installed`)
        this.log('')

        // List files
        const files = fs.readdirSync(targetDir).filter(f => !f.startsWith('.'))
        for (const f of files) {
          this.log(`    ${f}`)
        }

        this.log('')
        this.log(`  ${dim('Run:')} ${cyan(`rift backtest ${strategyName} --pair BTC-PERP --tf 1h`)}`)
      } else {
        this.log(`  ${red('✘')} No .py strategy files found in the repo`)
        fs.rmSync(targetDir, {recursive: true})
      }
    } catch (error: any) {
      this.log(`  ${red('✘')} Failed to install: ${error.message.split('\n')[0]}`)
    }

    this.log('')
  }
}
