# Security Policy

## Supported versions

RIFT is pre-1.0. Only the latest tagged release receives security fixes.

| Version | Supported |
|---------|-----------|
| latest (`main` + most recent tag) | ✅ |
| anything else | ❌ |

## Reporting a vulnerability

**Do not open public GitHub issues for security vulnerabilities.** Public disclosure of an unpatched issue puts every running RIFT user at risk.

Instead, report via either:

1. **GitHub Security Advisories** — preferred. Use the "Report a vulnerability" button on the repository's Security tab. This creates a private discussion with maintainers.
2. **Email** — `nexstone@proton.me`. Subject line: `RIFT security report`. PGP welcome but not required.

Include in your report:

- A description of the vulnerability and its impact
- Reproduction steps, ideally with a minimal repro case
- Affected version(s) — RIFT git SHA or release tag
- Whether the issue is currently being exploited (yes/no)
- Whether you've shared the issue with anyone else

## What to expect

- **Acknowledgment within 72 hours.**
- **Triage within 7 days** — we'll tell you whether we've confirmed the issue and roughly how soon we expect to ship a fix.
- **Coordinated disclosure** — we'll work with you on a disclosure timeline. Default is 90 days from initial report; we may move faster or slower based on severity and complexity.
- **Credit** — if you'd like to be named in the release notes once the fix ships, say so in your initial report.

## Out of scope

The following are not security issues:

- **You lost money trading.** Trading risk is not a vulnerability. RIFT ships disclaimers; trade at your own risk.
- **The advertised backtest numbers don't match your live results.** Past performance does not predict future returns. Models drift. This is the nature of markets, not a bug.
- **Hyperliquid is down.** Exchange outages are out of scope. RIFT will surface the error; it cannot fix the exchange.
- **Your private key was stolen by malware on your machine.** RIFT stores keys at `~/.rift/credentials` with `0600` permissions. If your operating system is compromised, anything on it is compromised.

## In scope

- Authentication / authorization bypasses
- Builder fee or integrity-seal tampering
- Code paths that leak private keys, auth tokens, or wallet addresses to logs, the network, or other processes
- Supply-chain risks in our published packages (PyPI, npm, Homebrew)
- Path traversal, command injection, or other RCE vectors in CLI argument handling
- Anything that would cause RIFT to silently place trades the operator did not authorize

## Hall of fame

Security researchers who have responsibly disclosed issues will be listed here once we have any to credit.
