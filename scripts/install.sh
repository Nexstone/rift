#!/usr/bin/env bash
#
# RIFT installer for Linux + WSL2.
# macOS users should prefer:  brew install Nexstone/tap/rift
#
# Hosted at: https://nexstone.io/install.sh
# Source:    https://github.com/Nexstone/rift/blob/main/scripts/install.sh
#
# Usage:
#   curl -fsSL https://nexstone.io/install.sh | sh
#   # or, to pin a version:
#   curl -fsSL https://nexstone.io/install.sh | RIFT_VERSION=0.1.0 sh
#
# What this script does, top to bottom:
#   1. Detect OS + arch. Refuse anything other than Linux x86_64/aarch64 or
#      Darwin (with a hint to use Homebrew on Darwin).
#   2. Verify Node 20+ is present. If not, install via the system package
#      manager (apt or yum) or print clear instructions.
#   3. Verify uv is present. If not, install via the official uv installer.
#   4. Create ~/.rift if missing, lock to 0700.
#   5. Install the Python engine into ~/.rift/.venv via uv (rift-engine from PyPI).
#   6. Install the TS CLI globally via npm (@nexstone/rift-cli).
#   7. Drop a wrapper script at ~/.local/bin/rift that points at both.
#   8. Tell the user how to add ~/.local/bin to PATH if it isn't already.
#   9. Run `rift doctor` to confirm everything's healthy.

set -euo pipefail

# ─── Config ───────────────────────────────────────────────────────────
RIFT_VERSION="${RIFT_VERSION:-latest}"
RIFT_HOME="${RIFT_HOME:-$HOME/.rift}"
RIFT_VENV="$RIFT_HOME/.venv"
INSTALL_PREFIX="${INSTALL_PREFIX:-$HOME/.local}"
INSTALL_BIN="$INSTALL_PREFIX/bin"
NPM_GLOBAL_PREFIX="$INSTALL_PREFIX/share/rift-cli"

# ─── Colors (only if stdout is a terminal) ────────────────────────────
if [ -t 1 ]; then
  C_DIM="$(printf '\033[2m')"
  C_RED="$(printf '\033[31m')"
  C_GREEN="$(printf '\033[32m')"
  C_YELLOW="$(printf '\033[33m')"
  C_CYAN="$(printf '\033[36m')"
  C_BOLD="$(printf '\033[1m')"
  C_RESET="$(printf '\033[0m')"
else
  C_DIM="" C_RED="" C_GREEN="" C_YELLOW="" C_CYAN="" C_BOLD="" C_RESET=""
fi

info()  { printf '%s\n' "${C_CYAN}::${C_RESET} $*"; }
ok()    { printf '%s\n' "${C_GREEN}✓${C_RESET} $*"; }
warn()  { printf '%s\n' "${C_YELLOW}!${C_RESET} $*"; }
err()   { printf '%s\n' "${C_RED}✘${C_RESET} $*" >&2; }
die()   { err "$1"; exit 1; }

# ─── Banner ───────────────────────────────────────────────────────────
cat <<EOF
${C_BOLD}RIFT installer${C_RESET}
${C_DIM}Quant trading infrastructure for Hyperliquid${C_RESET}
${C_DIM}https://github.com/Nexstone/rift${C_RESET}

EOF

# ─── 1. OS / arch detection ───────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
  Linux)
    case "$ARCH" in
      x86_64|amd64|aarch64|arm64) ok "Detected: Linux $ARCH" ;;
      *) die "Unsupported architecture: $ARCH. RIFT supports x86_64 and aarch64 on Linux." ;;
    esac
    ;;
  Darwin)
    warn "Detected macOS — Homebrew is the recommended install path:"
    warn "    brew install Nexstone/tap/rift"
    warn "Continuing with the manual install anyway, since you ran the script."
    ;;
  MINGW*|CYGWIN*|MSYS*)
    die "Native Windows is not supported. Run this script from WSL2 (Ubuntu)."
    ;;
  *)
    die "Unsupported OS: $OS"
    ;;
esac

# ─── 2. Node 20+ ──────────────────────────────────────────────────────
need_node_install=0
if command -v node >/dev/null 2>&1; then
  NODE_V=$(node --version | sed 's/^v//' | cut -d. -f1)
  if [ "$NODE_V" -ge 20 ]; then
    ok "Node $NODE_V detected"
  else
    warn "Node $NODE_V is too old (need 20+). Will install Node 20."
    need_node_install=1
  fi
else
  need_node_install=1
fi

if [ "$need_node_install" = "1" ]; then
  info "Installing Node 20 via NodeSource..."
  if command -v apt-get >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
  elif command -v yum >/dev/null 2>&1; then
    curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash -
    sudo yum install -y nodejs
  elif command -v dnf >/dev/null 2>&1; then
    curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash -
    sudo dnf install -y nodejs
  elif command -v brew >/dev/null 2>&1; then
    brew install node@20
  else
    die "Couldn't find apt/yum/dnf/brew to install Node. Install Node 20+ manually, then re-run this script."
  fi
  ok "Node installed: $(node --version)"
fi

# ─── 3. uv (Python package manager) ───────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  info "Installing uv (Python toolchain)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installer adds ~/.local/bin to PATH, but only for new shells.
  # Add it to this shell so we can run uv immediately.
  export PATH="$HOME/.local/bin:$PATH"
fi
ok "uv: $(uv --version)"

# ─── 4. ~/.rift directory ─────────────────────────────────────────────
mkdir -p "$RIFT_HOME"
chmod 0700 "$RIFT_HOME"
ok "RIFT home: $RIFT_HOME"

# ─── 5. Python engine via uv ──────────────────────────────────────────
info "Setting up Python 3.13 + engine venv at $RIFT_VENV..."
uv python install 3.13
uv venv --python 3.13 "$RIFT_VENV"
if [ "$RIFT_VERSION" = "latest" ]; then
  PIP_SPEC="rift-engine"
else
  PIP_SPEC="rift-engine==$RIFT_VERSION"
fi
"$RIFT_VENV/bin/pip" install --upgrade pip wheel >/dev/null
"$RIFT_VENV/bin/pip" install "$PIP_SPEC"
ok "Python engine installed ($PIP_SPEC)"

# ─── 6. TS CLI via npm ────────────────────────────────────────────────
info "Installing @nexstone/rift-cli into $NPM_GLOBAL_PREFIX..."
mkdir -p "$NPM_GLOBAL_PREFIX"
if [ "$RIFT_VERSION" = "latest" ]; then
  NPM_SPEC="@nexstone/rift-cli"
else
  NPM_SPEC="@nexstone/rift-cli@$RIFT_VERSION"
fi
NPM_CONFIG_PREFIX="$NPM_GLOBAL_PREFIX" npm install -g "$NPM_SPEC"
ok "CLI installed"

# ─── 7. Wrapper script ────────────────────────────────────────────────
mkdir -p "$INSTALL_BIN"
cat >"$INSTALL_BIN/rift" <<EOF
#!/usr/bin/env bash
# Auto-generated by the RIFT installer. Do not edit by hand.
# Re-generate by re-running https://nexstone.io/install.sh
export PATH="$RIFT_VENV/bin:\$PATH"
exec "$NPM_GLOBAL_PREFIX/bin/rift" "\$@"
EOF
chmod 0755 "$INSTALL_BIN/rift"
ok "Wrapper at $INSTALL_BIN/rift"

# ─── 8. PATH check ────────────────────────────────────────────────────
case ":$PATH:" in
  *":$INSTALL_BIN:"*)
    ok "$INSTALL_BIN is on PATH"
    ;;
  *)
    warn "$INSTALL_BIN is not on your PATH."
    warn "Add this to your shell rc (~/.bashrc, ~/.zshrc):"
    warn ""
    warn "    export PATH=\"$INSTALL_BIN:\$PATH\""
    warn ""
    warn "Or run with full path: $INSTALL_BIN/rift doctor"
    ;;
esac

# ─── 9. Doctor ────────────────────────────────────────────────────────
echo ""
info "Running rift doctor to verify install..."
"$INSTALL_BIN/rift" doctor || true

cat <<EOF

${C_GREEN}${C_BOLD}Install complete.${C_RESET}

Next steps:
  • Read:  https://github.com/Nexstone/rift/blob/main/docs/QUICKSTART.md
  • Auth:  rift auth setup    ${C_DIM}(only needed for live trading)${C_RESET}
  • Risk:  https://github.com/Nexstone/rift/blob/main/KNOWN_ISSUES.md

${C_DIM}RIFT is software, not financial advice. Trade at your own risk.${C_RESET}

EOF
