import {Args} from '@oclif/core'
import {GatedCommand} from '../lib/base-command.js'
import * as fs from 'node:fs'
import * as path from 'node:path'

const green = (s: string) => `\x1b[32m${s}\x1b[0m`
const dim = (s: string) => `\x1b[2m${s}\x1b[0m`
const bold = (s: string) => `\x1b[1m${s}\x1b[0m`
const cyan = (s: string) => `\x1b[36m${s}\x1b[0m`

function getConfigPath(): string {
  const dir = path.join(process.env.HOME || '~', '.rift')
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, {recursive: true})
  return path.join(dir, 'config.json')
}

function loadConfig(): Record<string, any> {
  const p = getConfigPath()
  if (!fs.existsSync(p)) return {}
  return JSON.parse(fs.readFileSync(p, 'utf-8'))
}

function saveConfig(config: Record<string, any>): void {
  fs.writeFileSync(getConfigPath(), JSON.stringify(config, null, 2), {mode: 0o600})
}

function getNestedValue(obj: Record<string, any>, keyPath: string): any {
  const parts = keyPath.split('.')
  let current: any = obj
  for (const part of parts) {
    if (current == null || typeof current !== 'object') return undefined
    current = current[part]
  }
  return current
}

function setNestedValue(obj: Record<string, any>, keyPath: string, value: any): void {
  const parts = keyPath.split('.')
  let current = obj
  for (let i = 0; i < parts.length - 1; i++) {
    if (!(parts[i] in current) || typeof current[parts[i]] !== 'object') {
      current[parts[i]] = {}
    }
    current = current[parts[i]]
  }
  current[parts[parts.length - 1]] = value
}

function maskSensitive(key: string, value: string): string {
  if (key.includes('key') || key.includes('secret') || key.includes('token') || key.includes('password')) {
    if (value.length <= 10) return '****'
    return value.slice(0, 8) + '...' + value.slice(-4)
  }
  return value
}

export default class Config extends GatedCommand {
  static override description = 'View and set RIFT configuration'

  static override examples = [
    '$ rift config list',
    '$ rift config set ai.api_key sk-ant-...',
    '$ rift config get ai.api_key',
    '$ rift config set ai.model claude-sonnet-4-20250514',
  ]

  static override strict = false

  static override args = {
    action: Args.string({
      description: 'Action: list, get, set',
      required: false,
      options: ['list', 'get', 'set'],
    }),
    key: Args.string({description: 'Config key (dot-notation, e.g. ai.api_key)', required: false}),
    value: Args.string({description: 'Value to set', required: false}),
  }

  async run(): Promise<void> {
    const {args} = await this.parse(Config)

    if (!args.action || args.action === 'list') {
      return this.listConfig()
    }

    if (args.action === 'get') {
      if (!args.key) {
        this.log(`  Usage: rift config get <key>`)
        return
      }
      return this.getConfig(args.key)
    }

    if (args.action === 'set') {
      if (!args.key || args.value === undefined) {
        this.log(`  Usage: rift config set <key> <value>`)
        this.log('')
        this.log(`  ${dim('Common keys:')}`)
        this.log(`    ${cyan('ai.api_key')}       ${dim('Anthropic API key for --analyze')}`)
        this.log(`    ${cyan('ai.model')}         ${dim('Model to use (default: claude-sonnet-4-20250514)')}`)
        this.log(`    ${cyan('network.proxy')}    ${dim('Proxy URL for Hyperliquid API')}`)
        return
      }
      return this.setConfig(args.key, args.value)
    }
  }

  private listConfig(): void {
    const config = loadConfig()

    if (Object.keys(config).length === 0) {
      this.log('')
      this.log(`  ${dim('No configuration set.')}`)
      this.log('')
      this.log(`  ${dim('Get started:')}`)
      this.log(`    ${cyan('rift config set ai.api_key sk-ant-...')}`)
      this.log('')
      return
    }

    this.log('')
    this.log(`  ${bold('RIFT Configuration')} ${dim('(~/.rift/config.json)')}`)
    this.log('')
    this.printObject(config, '  ')
    this.log('')
  }

  private printObject(obj: Record<string, any>, indent: string): void {
    for (const [key, value] of Object.entries(obj)) {
      if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
        this.log(`${indent}${bold(key)}:`)
        this.printObject(value, indent + '  ')
      } else {
        const display = typeof value === 'string' ? maskSensitive(key, value) : String(value)
        this.log(`${indent}${key}: ${dim(display)}`)
      }
    }
  }

  private getConfig(key: string): void {
    const config = loadConfig()
    const value = getNestedValue(config, key)

    if (value === undefined) {
      this.log(`  ${dim('Not set:')} ${key}`)
    } else {
      const display = typeof value === 'string' ? maskSensitive(key, value) : JSON.stringify(value, null, 2)
      this.log(`  ${key}: ${display}`)
    }
  }

  private setConfig(key: string, value: string): void {
    const config = loadConfig()
    setNestedValue(config, key, value)
    saveConfig(config)

    const display = maskSensitive(key, value)
    this.log(`  ${green('✔')} ${key} = ${dim(display)}`)
  }
}
