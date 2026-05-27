/**
 * Bridge between the TypeScript CLI and the Python engine.
 * Spawns Python processes and reads NDJSON output line by line.
 */

import {spawn, execSync} from 'node:child_process'
import {createInterface} from 'node:readline'
import * as path from 'node:path'
import * as fs from 'node:fs'
import {fileURLToPath} from 'node:url'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

export interface EngineMessage {
  type: 'progress' | 'result' | 'error' | 'status' | 'trade' | 'candle' | 'heartbeat' | 'shutdown' | 'step' | 'step_done' | 'soak'
  [key: string]: unknown
}

function getEngineDir(): string {
  let dir = path.resolve(__dirname)
  for (let i = 0; i < 10; i++) {
    const engineDir = path.join(dir, 'engine')
    // Require both pyproject.toml AND the rift.cli entry point.
    // Prevents matching packages/engine (the new rift_engine library)
    // before reaching the legacy engine/ host that owns -m rift.cli.
    if (
      fs.existsSync(path.join(engineDir, 'pyproject.toml')) &&
      fs.existsSync(path.join(engineDir, 'src', 'rift', 'cli.py'))
    ) return engineDir
    dir = path.dirname(dir)
  }

  throw new Error('Cannot find RIFT engine directory')
}

function findPython(): string {
  // Check engine's uv-managed venv first
  const engineVenv = path.join(getEngineDir(), '.venv', 'bin', 'python3')
  if (fs.existsSync(engineVenv)) return engineVenv

  // Check for managed venv in data dir
  const dataVenv = path.join(getDataDir(), 'venv', 'bin', 'python3')
  if (fs.existsSync(dataVenv)) return dataVenv

  // Fall back to system Python
  for (const cmd of ['python3.14', 'python3.13', 'python3']) {
    try {
      execSync(`${cmd} --version`, {stdio: 'ignore'})
      return cmd
    } catch {
      continue
    }
  }

  throw new Error('Python 3.13+ not found. Install Python or run: rift setup')
}

function getStrategiesDir(): string {
  let dir = path.resolve(__dirname)
  for (let i = 0; i < 10; i++) {
    const strategiesDir = path.join(dir, 'strategies')
    if (fs.existsSync(strategiesDir)) return strategiesDir
    dir = path.dirname(dir)
  }

  const projectRoot = path.resolve(__dirname, '..', '..', '..', '..')
  return path.join(projectRoot, 'strategies')
}

export function getDataDir(): string {
  return path.join(process.env.HOME || '~', '.rift')
}

export async function runEngine(
  command: string,
  args: string[],
  onMessage: (msg: EngineMessage) => void,
  extraEnv?: Record<string, string>,
): Promise<void> {
  const python = findPython()
  const engineDir = getEngineDir()
  const strategiesDir = getStrategiesDir()

  const fullArgs = [
    '-m', 'rift.cli',
    command,
    ...args,
  ]

  // Only add --strategies-dir for commands that accept it
  if (['backtest', 'strategies', 'compare', 'walk-forward', 'sweep', 'montecarlo', 'portfolio-backtest', 'research', 'quick-test', 'algo'].includes(command)) {
    fullArgs.push('--strategies-dir', strategiesDir)
  }

  const proc = spawn(python, fullArgs, {
    cwd: engineDir,
    env: {
      ...process.env,
      ...extraEnv,
      PYTHONPATH: path.join(engineDir, 'src'),
      PYTHONUNBUFFERED: '1',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  const promise = new Promise<void>((resolve, reject) => {
    const rl = createInterface({input: proc.stdout!})
    rl.on('line', (line: string) => {
      try {
        const msg = JSON.parse(line) as EngineMessage
        onMessage(msg)
      } catch {
        // Non-JSON output — ignore
      }
    })

    let stderr = ''
    proc.stderr!.on('data', (chunk: Buffer) => {
      stderr += chunk.toString()
    })

    proc.on('close', (code: number | null) => {
      if (code === 0 || code === null) {
        resolve()
      } else {
        const lines = stderr.trim().split('\n').filter(l => l.trim())
        const lastLine = lines[lines.length - 1] || ''
        let errorLine = lines.find(l => /^Error:/.test(l.trim()))
          || lines.find(l => /^(KeyError|ValueError|TypeError|RuntimeError|FileNotFoundError|ImportError):/.test(l.trim()))
          || lastLine
        errorLine = errorLine.trim().replace(/^Error:\s*/, '')
        reject(new Error(errorLine || `Engine exited with code ${code}`))
      }
    })

    proc.on('error', (err: Error) => {
      reject(new Error(`Failed to start engine: ${err.message}`))
    })
  })

  // Expose the child process for signal control (used by raw mode ESC handler)
  ;(promise as any)._proc = proc

  return promise
}

/** Get the child process from a runEngine promise (for sending signals) */
export function getEngineProcess(promise: Promise<void>): import('node:child_process').ChildProcess | null {
  return (promise as any)?._proc ?? null
}

/**
 * Spawn the Python engine as a detached daemon process.
 * Returns immediately after the process is started.
 * The daemon writes its own PID file and log file.
 */
export function spawnDaemon(
  command: string,
  args: string[],
  extraEnv?: Record<string, string>,
): {pid: number} {
  const python = findPython()
  const engineDir = getEngineDir()

  const fullArgs = [
    '-m', 'rift.cli',
    command,
    ...args,
    '--daemon',
  ]

  // Open /dev/null for stdio — daemon writes to its own log file
  const devNull = fs.openSync('/dev/null', 'r+')

  const proc = spawn(python, fullArgs, {
    cwd: engineDir,
    env: {
      ...process.env,
      ...extraEnv,
      PYTHONPATH: path.join(engineDir, 'src'),
      PYTHONUNBUFFERED: '1',
    },
    stdio: [devNull, devNull, devNull],
    detached: true,
  })

  // Let the daemon run independently of this process
  proc.unref()

  const pid = proc.pid ?? 0
  fs.closeSync(devNull)

  return {pid}
}

/** Session directory paths (must match Python's ALGO_DIR layout) */
export function getAlgoSessionsDir(): string {
  return path.join(getDataDir(), 'algo', 'sessions')
}

export function getAlgoPidsDir(): string {
  return path.join(getDataDir(), 'algo', 'pids')
}

export function getAlgoLogsDir(): string {
  return path.join(getDataDir(), 'algo', 'logs')
}
