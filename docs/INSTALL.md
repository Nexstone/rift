# Install

Full install instructions for the three supported platforms.

| Platform | Status | Notes |
|---|---|---|
| **macOS** (Apple Silicon + Intel) | ✅ Primary dev environment | All releases tested here |
| **Ubuntu 24.04 / Debian** | ✅ CI-tested | Recommended for servers |
| **WSL2 on Windows** | ⚠️ Should work, untested by maintainers | Use Ubuntu 24.04 inside WSL2 |
| **Native Windows** | ❌ Not supported | Use WSL2 |

---

## 1. Prerequisites

### macOS

```bash
# Homebrew (if you don't have it)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Toolchain
brew install uv node@20 pnpm
```

### Ubuntu / Debian

```bash
# uv — Python package manager that bundles its own Python 3.13
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc   # or open a new shell

# Node 20 via nodesource (Ubuntu's default is too old as of 24.04)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# pnpm
npm install -g pnpm
```

### WSL2

Inside an Ubuntu 24.04 WSL2 shell, follow the Ubuntu instructions above. Don't run RIFT from PowerShell or cmd.exe.

### Verify

```bash
uv --version    # >= 0.4
node --version  # v20.x or v22.x
pnpm --version  # >= 9
python3 --version  # >= 3.13 (uv will install this for you)
```

---

## 2. Clone + build

```bash
git clone <repo-url> rift
cd rift

# Python — installs Python 3.13 + all 9 rift_* packages in an isolated venv
cd engine && uv sync && cd ..

# TypeScript CLI
pnpm install
cd packages/cli && pnpm build && cd ../..
```

This takes 2–4 minutes total on a fresh machine, mostly waiting on `uv sync` to resolve and download Python deps.

---

## 3. Make `rift` callable

The TS CLI binary lives at `packages/cli/bin/run.js`. You can:

**Option A — add to PATH** (zsh / bash):

```bash
echo 'export PATH="'"$PWD"'/packages/cli/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

**Option B — symlink into an existing PATH directory**:

```bash
mkdir -p ~/bin
ln -s "$PWD/packages/cli/bin/run.js" ~/bin/rift
# ensure ~/bin is on PATH (most shells have this by default, check with `echo $PATH`)
```

**Option C — call it by path every time**: `./packages/cli/bin/run.js doctor`. Works but verbose.

Verify:

```bash
rift --version    # @rift/cli/0.1.0 ...
rift doctor       # run the install verifier
```

---

## 4. Configure secrets (when ready for live trading)

The repo's `.env.example` is the template. Copy it to `~/.rift/.env` and edit:

```bash
mkdir -p ~/.rift
cp .env.example ~/.rift/.env
chmod 600 ~/.rift/.env
$EDITOR ~/.rift/.env
```

Three optional secret blocks:

- **AWS credentials** — needed only for `rift sync` (downloads historical data from Hyperliquid's public S3 archive). Cost: ~$2 for a full pull, ~$0.30/month after. Skip if you only need live market data.
- **`HYPERLIQUID_PRIVATE_KEY`** — your API wallet private key. **DON'T set this directly** — use `rift auth setup` instead, which generates a fresh API wallet locally and pairs it via Hyperliquid's on-chain `approveAgent` flow. The API wallet cannot withdraw funds; main-wallet compromise is the only catastrophic key loss.
- **`HYPERLIQUID_ACCOUNT_ADDRESS`** — your main wallet address (also written by `rift auth setup`).

See [`AUTH_AND_EXECUTION.md`](AUTH_AND_EXECUTION.md) for the full trust architecture before paying real money.

---

## 5. Verify install

```bash
rift doctor
```

What you should see on a clean install with no wallet yet:

```
✔ Node.js v20.x
✘ Builder fee not approved — run: rift auth setup
! No wallet configured — run: rift auth setup
✔ Python 3.13.x
✔ Engine 0.1.0
✔ Polars / NumPy / PyArrow
◦ Proxy not configured (direct connection)
✔ Hyperliquid API <Nms> latency, ~600 pairs
! Cached Data — no data cached yet
✔ Strategies — 1 available
✔ RIFT home dir /Users/<you>/.rift
! .env permissions ... — fix if it warns 0644
◦ AWS credentials not set
✔ Disk space <N>GB free
```

The two `✘` / `!` lines for wallet + builder fee are expected until you run `rift auth setup`. Everything else should be green.

---

## Known install failures

### `uv sync` fails on `hyperliquid-python-sdk`

Some networks block PyPI mirrors. Re-run with a different mirror:

```bash
UV_INDEX_URL=https://pypi.org/simple uv sync
```

### `pnpm install` warns about Node version

If your Node is older than 20, install Node 20 explicitly:

```bash
# macOS
brew uninstall node && brew install node@20 && brew link --force node@20

# Linux
sudo apt-get remove nodejs
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
```

### `tsc` fails with "Cannot find module ..."

`pnpm install` didn't complete. Delete `node_modules` and retry:

```bash
rm -rf node_modules packages/*/node_modules
pnpm install
```

### `rift: command not found` after install

You skipped step 3. Either add `packages/cli/bin` to PATH or symlink.

### `Hyperliquid API ... fail` in doctor

Either:
- Your network blocks HL's IP. Try `rift set-proxy socks5://<host>:<port>`.
- HL is having an outage (check status.hyperliquid.xyz).
- A firewall is blocking outbound HTTPS to api.hyperliquid.xyz.

### Python 3.14 vs 3.13

`pyproject.toml` declares `>=3.13`. Both work, but **CI tests against 3.13**. If you hit a bug on 3.14 and not 3.13, file an issue. Pin to 3.13 if you want to match CI exactly:

```bash
cd engine && uv python install 3.13 && uv sync --python 3.13
```

---

## Updating

```bash
cd rift
git pull
cd engine && uv sync && cd ..
pnpm install
cd packages/cli && pnpm build && cd ../..
rift doctor
```

If `~/.rift/credentials` format ever changes (rare), `rift doctor` will tell you what to do.

---

## Uninstall

```bash
# Remove the repo
cd .. && rm -rf rift

# Remove user state (loses wallet + history + cache)
rm -rf ~/.rift

# Optional: remove the toolchain if you installed it just for RIFT
brew uninstall uv pnpm node@20    # macOS
# or remove the uv install dir on Linux: rm -rf ~/.local/bin/uv ~/.local/share/uv
```
