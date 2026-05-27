#!/bin/bash
#
# Regenerate docs/CLI_REFERENCE.md by introspecting `rift --help` and per-command help.
#
# Run from the repo root after CLI changes:
#     bash scripts/gen_cli_reference.sh
#
# Adds new top-level commands automatically by reading `rift --help`.
# Commands hidden behind `rift more` are intentionally not listed here —
# they exist (see `rift more` for the full catalog) but aren't first-class.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RIFT="$REPO_ROOT/packages/cli/bin/run.js"
OUT="$REPO_ROOT/docs/CLI_REFERENCE.md"

if [ ! -x "$RIFT" ]; then
  echo "✘ $RIFT not executable. Did you forget to \`pnpm build\` in packages/cli/?" >&2
  exit 1
fi

# Strip ANSI color codes from CLI output (so the markdown stays readable on GitHub)
strip_ansi() {
  sed 's/\x1b\[[0-9;]*m//g'
}

# Pull the list of top-level commands directly from `rift --help`.
# oclif's format is:
#   COMMANDS
#     name1   description...
#             continuation of name1's description
#     name2   description...
# We want only the first column, only when the line starts with exactly 2 spaces
# followed by a letter (continuation lines start with more whitespace).
extract_commands() {
  "$RIFT" --help 2>&1 \
    | strip_ansi \
    | awk '/^COMMANDS$/{flag=1; next} flag' \
    | grep -E '^  [a-z][a-z-]+ ' \
    | awk '{print $1}'
}

{
  echo "# CLI reference"
  echo ""
  echo "Auto-generated from \`rift --help\` and \`rift <command> --help\`."
  echo ""
  echo "Regenerate after CLI changes:"
  echo ""
  echo "    bash scripts/gen_cli_reference.sh"
  echo ""
  echo "For commands not listed here, see \`rift more\` — it surfaces every engine command,"
  echo "including the ones without a top-level \`rift <cmd>\` wrapper."
  echo ""
  echo "---"
  echo ""
  echo "## Top-level"
  echo ""
  echo '```'
  "$RIFT" --help 2>&1 | strip_ansi
  echo '```'
  echo ""
  echo "---"
  echo ""
  echo "## Commands"
  echo ""

  for cmd in $(extract_commands); do
    echo "### \`rift $cmd\`"
    echo ""
    echo '```'
    "$RIFT" help "$cmd" 2>&1 | strip_ansi || echo "(help unavailable)"
    echo '```'
    echo ""
  done
} > "$OUT"

echo "✓ wrote $OUT ($(wc -l < "$OUT") lines, $(extract_commands | wc -l | tr -d ' ') commands)"
