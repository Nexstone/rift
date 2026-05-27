# Algo daemon monitoring runbook

You have a `trend_follow` daemon running on the $11.88 test wallet. trend_follow on a 4h timeframe with 50/200 EMA crossover fires rarely — typical cadence is one trade every 2–4 weeks during trending markets, and weeks of nothing during chop. The daemon is more useful as a stability soak than as a trader.

This runbook tells you what to check daily, what's normal, and when to intervene.

---

## Daily 30-second check

```bash
rift algo status
```

Expected output during quiet periods:

```
● ALGO  trend_follow on BTC  PID <N>
  Equity: $11.88  +0%  Trades: 0  Win: 0%
  Position: FLAT
  Started: 2026-05-26 20:13:51
```

If the daemon is gone or you see `No algo sessions running.`:

1. Check the log: `tail -50 ~/.rift/algo/logs/trend_follow_BTC.log`
2. Check for an open HL position the daemon may have left: `rift more balance` and `rift more holdings`
3. If a position exists, the daemon crashed mid-trade. Close manually via HL UI or `rift more close-position BTC`. Then restart and the post-Phase-1 orphan check will surface it.

## Weekly 5-minute check

```bash
# Daemon still alive?
rift algo status

# Recent state
tail -30 ~/.rift/algo/logs/trend_follow_BTC.log

# Memory / CPU sanity (daemon should be <100MB, near-idle CPU)
ps -p $(cat ~/.rift/algo/pids/trend_follow_BTC.pid) -o pid,vsz,rss,pcpu,etime,command

# Any errors in the log this week?
grep '"type": "error"' ~/.rift/algo/logs/trend_follow_BTC.log | tail -10

# Any actual trades happened?
grep '"type": "trade"' ~/.rift/algo/logs/trend_follow_BTC.log | tail -10
```

## What's normal

- **Position stays FLAT for days**: that's the strategy. EMA crossovers on 4h are rare.
- **Equity unchanged**: no trades = no P&L. Expected.
- **The session snapshot at `~/.rift/algo/sessions/trend_follow_BTC.json` updates roughly every tick**: confirms the daemon is alive and the state file is fresh.
- **Occasional `"type": "error"` lines from transient HL API hiccups**: the daemon catches them and continues. One every few hours is fine; one every minute means something's wrong.
- **WebSocket reconnects logged occasionally**: HL closes long-lived sockets every few hours; the feed reconnects automatically. Normal.

## What's NOT normal

| Sign | What it means | Action |
|---|---|---|
| Daemon process gone, no clean shutdown line in log | Crash or OOM | Restart with `echo "LIVE" \| rift algo trend_follow --pair BTC` and check the orphan warning if a position exists |
| Memory growing (RSS climbing daily) | Memory leak | Stop daemon (`rift algo stop --all`), file an issue with `ps` output, restart |
| Many `"type": "error"` lines per minute | Persistent API/network issue | Run `rift doctor`. Check HL status. Stop the daemon if it's making noise without making progress |
| Position open in HL but `rift algo status` shows FLAT | Orphan from a prior crash | Close via `rift more close-position BTC` (the post-Phase-1 orphan detector will catch this on next restart) |
| Position open and daemon thinks position is open, but stop_proximity > 0.9 for hours | Price hovering near stop — the dynamic stop policy may have tightened things too aggressively | Watch closely; consider `rift more close-position BTC` if you don't want the stop to fire |

## When a trade fires

EMA cross triggers an entry. Daemon emits:

```
{"type": "trade", "action": "open", "side": "long", "price": ..., "size": ...}
```

After that you'll see heartbeats showing the open position. The daemon will exit on:
- **Target hit** (unrealized PnL reaches the strategy's target)
- **Stop loss** (price crosses the stop — either exchange-side or local-monitor)
- **Reverse cross** (EMA flips the other way — trend strategies close+flip)

Exit emits:

```
{"type": "trade", "action": "close", "exit_reason": "..."}
{"type": "shutdown", "msg": "..."}
```

Recorded artifacts:
- Live state: `~/.rift/algo/sessions/trend_follow_BTC.json`
- Final summary: `~/.rift/algo_sessions/ALGO_trend_follow_BTC_<timestamp>.json`
- Append-only log: `~/.rift/algo/logs/trend_follow_BTC.log`

## Graceful shutdown

```bash
rift algo stop --all
```

This sends SIGTERM, the daemon closes any open position via reduce-only IOC, writes the final session summary, removes its pid file, and exits clean.

## Kill if needed (last resort)

```bash
kill -9 $(cat ~/.rift/algo/pids/trend_follow_BTC.pid)
```

This is a SIGKILL — no chance for the daemon to close cleanly. If a position was open, it stays open on HL. On next start the orphan detector will refuse to start until you close the position manually.

## What I'm watching for over the next 1–2 weeks

- **Memory creep** (Node 24 + Python 3.14 + websocket-client may have unknown long-running quirks)
- **Reconnect failures** (HL websocket drops + RIFT's reconnect loop)
- **First real trade** (validates the algo entry path in production conditions)
- **Funding-time edge cases** (HL settles funding hourly; the daemon should handle epoch boundaries cleanly)

If we hit two weeks with zero errors and the daemon's still humming, the long-running stability box gets a green check.
