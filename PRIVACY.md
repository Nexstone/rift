# Privacy

**TL;DR — RIFT runs entirely on your machine. Nexstone receives no data from your installation.**

This document describes what data leaves your computer when you run RIFT, and what doesn't.

## What RIFT does NOT do

- ❌ No telemetry — RIFT never reports usage, errors, crashes, or install events back to Nexstone.
- ❌ No analytics — no PostHog, no Mixpanel, no Segment, no Google Analytics, no anything.
- ❌ No phone-home checks — RIFT does not check for updates by contacting Nexstone servers.
- ❌ No third-party error reporters — no Sentry, no Rollbar, no Bugsnag.
- ❌ No license server — RIFT is Apache 2.0 OSS; there is nothing to validate.
- ❌ No A/B tests, no feature flags, no remote config.

You can verify this with:

```bash
# Python: every outbound URL in the codebase
grep -rE "https?://" --include="*.py" engine/ packages/ scrapers/

# TypeScript: every outbound URL in the codebase
grep -rE "https?://" --include="*.ts" --include="*.tsx" packages/
```

You should find exactly three categories of destinations: Hyperliquid, AWS S3, and WalletConnect — described below.

## What RIFT DOES do (and where the data goes)

### 1. Hyperliquid (api.hyperliquid.xyz, ws://api.hyperliquid.xyz)

**Purpose:** trade execution, market data, account state.

When you trade, query prices, scan opportunities, sync positions, or run an algo session, RIFT communicates with the Hyperliquid exchange — exactly as any other Hyperliquid client (the official UI, the Python SDK, third-party bots) does.

**What goes out:** orders signed by your API wallet, queries for prices and account state, subscriptions to market data streams.

**What comes back:** market data, fill confirmations, account state.

You can route this through a SOCKS5 proxy via `~/.rift/.env` if you want to obscure your IP. See `rift set-proxy`.

### 2. AWS S3 (hyperliquid-archive bucket)

**Purpose:** historical data sync.

`rift sync` downloads historical candle / funding / L2 order-book data from Hyperliquid's public S3 archive. The data is public; the access is authenticated against *your own AWS account* (you provide the access key in `~/.rift/.env`).

**Cost:** roughly $2 for a full historical pull, ~$0.30/month for incremental syncs (paid to AWS, not Nexstone).

You can skip this entirely if you only need recent live data, which comes from the Hyperliquid API and websocket directly.

### 3. WalletConnect (relay.walletconnect.com, optional)

**Purpose:** wallet pairing for non-CLI auth.

If you use `rift auth setup` with a mobile/browser wallet (Rabby, MetaMask Mobile, etc.) instead of pasting a private key, RIFT uses the WalletConnect protocol. This involves the WalletConnect relay network seeing the connection handshake metadata.

**What goes out (to WalletConnect relays, not Nexstone):**
- Connection request with app metadata: `"Nexstone — RIFT"`, the logo URL, and the methods being requested.
- Signed messages from your wallet that authorize API wallet registration and (optionally) builder fee approval.

**What does NOT go out:**
- Your private key (it never leaves your wallet app).
- The actual trade orders (those go directly to Hyperliquid).

If you don't want any WalletConnect involvement, skip the QR scan and paste your API wallet private key directly: `rift auth setup --key 0x...`.

## What's stored locally

RIFT keeps everything on your machine. Locations under `~/.rift/`:

| Path | Contents | Permissions |
|---|---|---|
| `~/.rift/.env` | AWS + HL credentials | `0600` (user read/write only) |
| `~/.rift/credentials` | API wallet private key + builder-fee state | `0600` |
| `~/.rift/tokens/` | Authorization tokens for trade gating | `0600` per file |
| `~/.rift/data/` | Cached candles / funding / L2 books | `0600` per file |
| `~/.rift/raw/` | Raw S3 download cache | `0600` per file |
| `~/.rift/algo/` | Algo daemon state (pids, sessions, logs) | `0700` dir, `0600` files |
| `~/.rift/recon/` | Recon trade logs | `0600` per file |
| `~/.rift/signal_memory.jsonl` | Append-only log of past trade outcomes for signal learning | `0600` |
| `~/.rift/lessons.json` | Captured post-trade learnings | `0600` |
| `~/.rift/validated_edge.json` | Promoted-strategy metadata | `0600` |
| `~/.rift/bundles/` | Sealed reproducibility manifests | `0600` per file |

If you want to wipe all RIFT state: `rm -rf ~/.rift/`.

If you want to back up RIFT state: tar up `~/.rift/` minus `data/` and `raw/` (those are caches that can be re-downloaded).

## On-chain visibility

This is not a privacy issue with RIFT itself, but worth knowing: every trade you place on Hyperliquid is recorded on Hyperliquid's chain. Your wallet address and trade history are public. If on-chain privacy matters to you, use a fresh wallet per identity and route via a proxy or VPN.

## If we ever change any of this

A future RIFT version will not silently add telemetry. Any change to network egress behavior will:

1. Be documented in `CHANGELOG.md` for the release that introduces it.
2. Be reflected in this `PRIVACY.md` before the release ships.
3. Be opt-in by default. Telemetry will never run without explicit user consent at install time.

## Questions

Email `nexstone@proton.me` with the subject `RIFT privacy question`.
