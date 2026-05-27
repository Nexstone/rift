#!/usr/bin/env -S node --no-warnings

// `--no-warnings` suppresses Node's process warnings. This is intentional
// for the CLI entry point:
//   1. The MCP server uses stdio for JSON-RPC; cosmetic stderr noise is
//      undesirable for that path.
//   2. oclif emits a harmless [MODULE_NOT_FOUND] warning at startup when its
//      help plugin tries to resolve a symbol-keyed single-command fallback
//      that doesn't exist for our multi-command CLI.
//   3. End users don't need to see Node internal deprecation messages.
//
// If you're debugging and want warnings back, run with:
//   node --warnings packages/cli/bin/run.js <args>

import {execute} from '@oclif/core'

// If no arguments, show the master menu
if (process.argv.length <= 2) {
  process.argv.push('home')
}

await execute({dir: import.meta.url})
