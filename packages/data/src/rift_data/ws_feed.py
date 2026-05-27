"""LiveMarketFeed — real-time websocket data for live/sim/recon daemons.

Subscribes to Hyperliquid trades + l2Book for a single coin,
aggregates in-memory, and provides derived fields for StrategyState.

Usage:
    feed = LiveMarketFeed(coin="BTC")
    feed.start()
    # ... later, on each tick:
    derived = feed.get_derived()
    # derived = {"cvd": ..., "volume_delta": ..., "relative_volume": ..., "tape": {...}, "orderflow": {...}}
    feed.stop()

No disk I/O. No external collector dependency. Works for any user on any machine.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import websocket

WS_URL = "wss://api.hyperliquid.xyz/ws"

logger = logging.getLogger(__name__)


@dataclass
class _TradeBucket:
    """One minute of aggregated trades."""
    timestamp: int = 0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    large_buy_volume: float = 0.0
    large_sell_volume: float = 0.0
    notional: float = 0.0  # sum of price * size for VWAP


class LiveMarketFeed:
    """Real-time market data feed for a single coin via Hyperliquid websocket."""

    def __init__(self, coin: str):
        self.coin = coin
        self._lock = threading.Lock()

        # Trade aggregation
        self._current_bucket = _TradeBucket()
        self._bucket_start = 0
        self._trade_sizes: list[float] = []  # for large trade detection
        self._volume_history: deque[float] = deque(maxlen=60)  # last 60 minutes for relative_volume

        # CVD tracking (cumulative across session)
        self._cvd: float = 0.0
        self._volume_delta: float = 0.0  # latest 1-min delta

        # Order book tracking
        self._prev_bid_depth: float = 0.0
        self._prev_ask_depth: float = 0.0
        self._book_diffs: list[dict] = []
        self._book_diff_start: int = 0

        # Latest snapshots for state injection
        self._latest_tape: dict = {}
        self._latest_orderflow: dict = {}

        # Vault data (polled via REST, not websocket)
        self._vault_positions: dict = {}
        self._last_vault_poll: float = 0.0

        # Connection
        self._ws: websocket.WebSocketApp | None = None
        self._ws_thread: threading.Thread | None = None
        self._running = False
        self._connected = False

    def start(self) -> None:
        """Start websocket connection in background thread."""
        if self._running:
            return
        self._running = True
        self._bucket_start = int(time.time())
        self._book_diff_start = int(time.time())

        self._ws_thread = threading.Thread(target=self._connect_loop, daemon=True)
        self._ws_thread.start()

        # Start vault polling in separate thread
        self._vault_thread = threading.Thread(target=self._vault_poll_loop, daemon=True)
        self._vault_thread.start()

    def stop(self) -> None:
        """Stop websocket and flush."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def get_derived(self) -> dict:
        """Get derived fields for StrategyState injection.

        Returns dict with keys that map directly to StrategyState fields:
            cvd, volume_delta, relative_volume, tape, orderflow, vault_positions
        """
        with self._lock:
            # Flush current trade bucket if >60s old
            now = int(time.time())
            if now - self._bucket_start >= 60 and self._bucket_start > 0:
                self._flush_trade_bucket()

            # Flush order book diffs if >300s old
            if now - self._book_diff_start >= 300 and self._book_diff_start > 0:
                self._flush_orderflow()

            # Compute relative volume
            if len(self._volume_history) >= 5:
                avg_vol = sum(self._volume_history) / len(self._volume_history)
                current_vol = self._current_bucket.buy_volume + self._current_bucket.sell_volume
                if current_vol == 0 and self._latest_tape:
                    current_vol = self._latest_tape.get("total_volume", 0)
                relative_volume = current_vol / avg_vol if avg_vol > 0 else 1.0
            else:
                relative_volume = 1.0

            return {
                "cvd": self._cvd,
                "volume_delta": self._volume_delta,
                "relative_volume": relative_volume,
                "tape": dict(self._latest_tape),
                "orderflow": dict(self._latest_orderflow),
                "vault_positions": dict(self._vault_positions),
            }

    # ──────────────────────────────────────────────────────────
    #  WebSocket connection
    # ──────────────────────────────────────────────────────────
    def _connect_loop(self) -> None:
        """Connect with auto-reconnect."""
        reconnect_delay = 1
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=50, ping_timeout=10)
            except Exception as e:
                logger.warning(f"WS connection error: {e}")

            if not self._running:
                break
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30)

    def _on_open(self, ws) -> None:
        self._connected = True
        # Subscribe to trades and l2Book for our coin only
        ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "trades", "coin": self.coin},
        }))
        ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "l2Book", "coin": self.coin},
        }))
        logger.info(f"LiveMarketFeed connected: {self.coin} trades + l2Book")

    def _on_message(self, ws, message: str) -> None:
        if message == "Websocket connection established.":
            return
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return

        channel = msg.get("channel", "")
        data = msg.get("data")
        if not data:
            return

        if channel == "trades":
            self._handle_trades(data)
        elif channel == "l2Book":
            self._handle_l2book(data)

    def _on_error(self, ws, error) -> None:
        logger.warning(f"WS error ({self.coin}): {error}")
        self._connected = False

    def _on_close(self, ws, code, msg) -> None:
        self._connected = False

    # ──────────────────────────────────────────────────────────
    #  Trade handling
    # ──────────────────────────────────────────────────────────
    def _handle_trades(self, trades: list[dict]) -> None:
        if not isinstance(trades, list):
            return

        with self._lock:
            now = int(time.time())

            # Check if we need to rotate the bucket
            if now - self._bucket_start >= 60:
                self._flush_trade_bucket()
                self._bucket_start = now
                self._current_bucket = _TradeBucket(timestamp=(now // 60) * 60 * 1000)
                self._trade_sizes = []

            for t in trades:
                price = float(t.get("px", 0))
                size = float(t.get("sz", 0))
                side = t.get("side", "")  # A = buy aggressor, B = sell aggressor

                self._trade_sizes.append(size)

                if side == "A":  # buyer is aggressor
                    self._current_bucket.buy_volume += size
                    self._current_bucket.buy_count += 1
                elif side == "B":  # seller is aggressor
                    self._current_bucket.sell_volume += size
                    self._current_bucket.sell_count += 1

                self._current_bucket.notional += price * size

    def _flush_trade_bucket(self) -> None:
        """Finalize current trade bucket and update derived fields. Must hold lock."""
        b = self._current_bucket
        total_vol = b.buy_volume + b.sell_volume

        if total_vol == 0:
            return

        # Large trade detection
        if self._trade_sizes:
            self._trade_sizes.sort()
            median = self._trade_sizes[len(self._trade_sizes) // 2]
            threshold = max(median * 10, 0.001)
            # Recalculate from trade sizes — approximate since we don't store per-trade side
            # Use the ratio of buy/sell to split large trades proportionally
            buy_ratio = b.buy_volume / total_vol if total_vol > 0 else 0.5
            large_total = sum(s for s in self._trade_sizes if s > threshold)
            b.large_buy_volume = large_total * buy_ratio
            b.large_sell_volume = large_total * (1 - buy_ratio)

        # Volume delta for this bucket
        delta = b.buy_volume - b.sell_volume
        self._volume_delta = delta
        self._cvd += delta

        # Track volume history for relative_volume
        self._volume_history.append(total_vol)

        # Imbalance
        imbalance = delta / total_vol if total_vol > 0 else 0

        # VWAP
        vwap = b.notional / total_vol if total_vol > 0 else 0

        trade_count = b.buy_count + b.sell_count

        self._latest_tape = {
            "buy_volume": round(b.buy_volume, 6),
            "sell_volume": round(b.sell_volume, 6),
            "buy_count": b.buy_count,
            "sell_count": b.sell_count,
            "total_volume": round(total_vol, 6),
            "trade_count": trade_count,
            "large_buy_volume": round(b.large_buy_volume, 6),
            "large_sell_volume": round(b.large_sell_volume, 6),
            "vwap": round(vwap, 6),
            "imbalance": round(imbalance, 4),
            "tape_speed": trade_count,
        }

    # ──────────────────────────────────────────────────────────
    #  Order book handling
    # ──────────────────────────────────────────────────────────
    def _handle_l2book(self, book_data: dict) -> None:
        with self._lock:
            levels = book_data.get("levels", [[], []])
            bids = levels[0] if len(levels) > 0 else []
            asks = levels[1] if len(levels) > 1 else []

            bid_depth = sum(float(b.get("sz", 0)) for b in bids[:10])
            ask_depth = sum(float(a.get("sz", 0)) for a in asks[:10])

            best_bid = float(bids[0]["px"]) if bids else 0
            best_ask = float(asks[0]["px"]) if asks else 0
            spread = (best_ask - best_bid) / best_bid if best_bid > 0 and best_ask > 0 else 0

            # Track depth vanishing (phantom orders)
            bid_vanished = max(0, self._prev_bid_depth - bid_depth) if self._prev_bid_depth > 0 else 0
            ask_vanished = max(0, self._prev_ask_depth - ask_depth) if self._prev_ask_depth > 0 else 0

            self._book_diffs.append({
                "bid_depth": bid_depth,
                "ask_depth": ask_depth,
                "bid_vanished": bid_vanished,
                "ask_vanished": ask_vanished,
                "spread": spread,
            })

            self._prev_bid_depth = bid_depth
            self._prev_ask_depth = ask_depth

            now = int(time.time())
            if now - self._book_diff_start >= 300:
                self._flush_orderflow()
                self._book_diff_start = now

    def _flush_orderflow(self) -> None:
        """Aggregate order book diffs into 5-min summary. Must hold lock."""
        diffs = self._book_diffs
        self._book_diffs = []

        if not diffs:
            return

        n = len(diffs)
        avg_bid = sum(d["bid_depth"] for d in diffs) / n
        avg_ask = sum(d["ask_depth"] for d in diffs) / n
        total_bid_vanished = sum(d["bid_vanished"] for d in diffs)
        total_ask_vanished = sum(d["ask_vanished"] for d in diffs)
        avg_spread = sum(d["spread"] for d in diffs) / n

        total_depth = avg_bid + avg_ask
        bid_ratio = avg_bid / total_depth if total_depth > 0 else 0.5

        phantom_bid = total_bid_vanished / (avg_bid * n) if avg_bid > 0 else 0
        phantom_ask = total_ask_vanished / (avg_ask * n) if avg_ask > 0 else 0

        self._latest_orderflow = {
            "avg_bid_depth": round(avg_bid, 4),
            "avg_ask_depth": round(avg_ask, 4),
            "bid_ratio": round(bid_ratio, 4),
            "bid_vanished": round(total_bid_vanished, 4),
            "ask_vanished": round(total_ask_vanished, 4),
            "phantom_bid_ratio": round(phantom_bid, 4),
            "phantom_ask_ratio": round(phantom_ask, 4),
            "avg_spread_bps": round(avg_spread * 10000, 2),
            "snapshots": n,
        }

    # ──────────────────────────────────────────────────────────
    #  Vault polling (REST, not websocket)
    # ──────────────────────────────────────────────────────────
    def _vault_poll_loop(self) -> None:
        """Poll vault positions every 15 minutes."""
        while self._running:
            try:
                self._poll_vaults()
            except Exception as e:
                logger.warning(f"Vault poll error: {e}")
            # Sleep in 5s increments so we can check _running
            for _ in range(180):  # 15 minutes = 180 × 5s
                if not self._running:
                    return
                time.sleep(5)

    def _poll_vaults(self) -> None:
        """Fetch top vault positions for our coin."""
        import requests

        # Get vault summaries
        r = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "vaultSummaries"},
            timeout=30,
        )
        if r.status_code != 200:
            return

        summaries = r.json()
        if not summaries:
            return

        # Sort by equity, take top 20
        vaults = []
        for v in summaries:
            try:
                addr = v.get("vaultAddress", "")
                equity = float(v.get("equity", 0))
                if equity > 0 and addr:
                    vaults.append((addr, v.get("name", ""), equity))
            except (ValueError, TypeError):
                continue

        vaults.sort(key=lambda x: x[2], reverse=True)
        top = vaults[:20]

        num_long = 0
        num_short = 0
        total_vaults_with_position = 0
        net_notional = 0.0

        for addr, name, equity in top:
            try:
                r2 = requests.post(
                    "https://api.hyperliquid.xyz/info",
                    json={"type": "vaultDetails", "vaultAddress": addr},
                    timeout=15,
                )
                if r2.status_code != 200:
                    continue

                details = r2.json()
                portfolio = details.get("portfolio", [])

                if isinstance(portfolio, list):
                    for pos in portfolio:
                        asset_pos = pos.get("position", {})
                        coin_name = asset_pos.get("coin", "")
                        szi = float(asset_pos.get("szi", 0))
                        if coin_name == self.coin and szi != 0:
                            total_vaults_with_position += 1
                            entry_px = float(asset_pos.get("entryPx", 0))
                            notional = szi * entry_px
                            net_notional += notional
                            if szi > 0:
                                num_long += 1
                            else:
                                num_short += 1

                time.sleep(0.3)
            except Exception:
                continue

        with self._lock:
            self._vault_positions = {
                "num_long": num_long,
                "num_short": num_short,
                "total_vaults": total_vaults_with_position,
                "net_notional": round(net_notional, 2),
                "position_change_pct": 0.0,  # would need prev snapshot to compute
            }

    @property
    def is_connected(self) -> bool:
        return self._connected


# ══════════════════════════════════════════════════════════════
#  MultiCoinFeed — ephemeral multi-coin websocket for Scout soak
# ══════════════════════════════════════════════════════════════

class MultiCoinFeed:
    """Multi-coin websocket feed for Scout's soak phase.

    One websocket connection subscribes to trades + bbo + activeAssetCtx
    for N coins simultaneously. Aggregates per-coin trade flow, order book
    imbalance, and live market context. Designed to be ephemeral — spin up,
    soak for 2 minutes, extract data, tear down.

    Uses bbo (best bid/offer) instead of full l2Book for lighter weight.
    Uses activeAssetCtx for live funding/OI/premium instead of REST polling.

    Usage:
        feed = MultiCoinFeed(["BTC", "ETH", "SOL"])
        feed.start()
        feed.soak(120)  # block for 2 minutes
        data = feed.get_derived("BTC")
        feed.stop()
    """

    def __init__(self, coins: list[str]):
        self.coins = coins
        self._lock = threading.Lock()

        # Per-coin trade aggregation
        self._buckets: dict[str, _TradeBucket] = {c: _TradeBucket() for c in coins}
        self._trade_sizes: dict[str, list[float]] = {c: [] for c in coins}
        self._volume_history: dict[str, deque[float]] = {c: deque(maxlen=60) for c in coins}
        self._cvd: dict[str, float] = {c: 0.0 for c in coins}
        self._volume_delta: dict[str, float] = {c: 0.0 for c in coins}
        self._latest_tape: dict[str, dict] = {c: {} for c in coins}

        # Per-coin BBO tracking (best bid/offer — lighter than l2Book)
        self._latest_bbo: dict[str, dict] = {c: {} for c in coins}

        # Per-coin market context from activeAssetCtx (live push, no REST needed)
        self._asset_ctx: dict[str, dict] = {c: {} for c in coins}

        # Vault positions (one REST call covers all coins)
        self._vault_positions: dict[str, dict] = {}

        # Timing
        self._bucket_start = 0
        self._total_trades = 0

        # Connection
        self._ws: websocket.WebSocketApp | None = None
        self._ws_thread: threading.Thread | None = None
        self._running = False
        self._connected = False

    def start(self) -> None:
        """Start websocket connection in background."""
        if self._running:
            return
        self._running = True
        self._bucket_start = int(time.time())

        self._ws_thread = threading.Thread(target=self._connect_loop, daemon=True)
        self._ws_thread.start()

        # Vault polling in background (one call, partitioned by coin)
        self._vault_thread = threading.Thread(target=self._poll_vaults, daemon=True)
        self._vault_thread.start()

    def stop(self) -> None:
        """Tear down websocket and all threads."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def soak(self, seconds: int = 120, progress_cb=None) -> None:
        """Block until soak is complete, flushing trade buckets periodically.

        Args:
            seconds: How long to collect data (default 120s / 2 min)
            progress_cb: Optional callback(elapsed, total, trades) for progress updates
        """
        start = time.time()
        last_flush = start

        while time.time() - start < seconds and self._running:
            elapsed = time.time() - start

            # Flush trade buckets every 60 seconds
            if time.time() - last_flush >= 60:
                self._flush_all_buckets()
                last_flush = time.time()

            # Progress callback
            if progress_cb:
                progress_cb(int(elapsed), seconds, self._total_trades)

            time.sleep(1)

        # Final flush
        self._flush_all_buckets()

    def get_derived(self, coin: str) -> dict:
        """Get derived fields for a specific coin after soaking.

        Returns dict compatible with Scout's _build_state soak_data parameter.
        """
        with self._lock:
            # Relative volume
            vol_hist = self._volume_history.get(coin, deque())
            if len(vol_hist) >= 2:
                avg_vol = sum(vol_hist) / len(vol_hist)
                current_vol = vol_hist[-1] if vol_hist else 0
                relative_volume = current_vol / avg_vol if avg_vol > 0 else 1.0
            else:
                relative_volume = 1.0

            tape = dict(self._latest_tape.get(coin, {}))
            bbo = dict(self._latest_bbo.get(coin, {}))
            ctx = dict(self._asset_ctx.get(coin, {}))
            vaults = dict(self._vault_positions.get(coin, {}))

            return {
                "cvd": self._cvd.get(coin, 0.0),
                "volume_delta": self._volume_delta.get(coin, 0.0),
                "relative_volume": relative_volume,
                "tape": tape,
                "orderflow": {
                    "avg_bid_depth": bbo.get("bid_sz", 0),
                    "avg_ask_depth": bbo.get("ask_sz", 0),
                    "bid_ratio": bbo.get("bid_ratio", 0.5),
                    "phantom_bid_ratio": 0,  # bbo doesn't track phantoms
                    "phantom_ask_ratio": 0,
                    "avg_spread_bps": bbo.get("spread_bps", 0),
                },
                "vault_positions": vaults,
                # Live market context from activeAssetCtx
                "funding_rate": ctx.get("funding", 0),
                "open_interest": ctx.get("openInterest", 0),
                "oracle_price": ctx.get("oraclePx", 0),
                "mark_price": ctx.get("markPx", 0),
                "day_volume": ctx.get("dayNtlVlm", 0),
                "premium": 0,  # computed from mark vs oracle below
            }

    def get_all_derived(self) -> dict[str, dict]:
        """Get derived data for all coins at once."""
        return {coin: self.get_derived(coin) for coin in self.coins}

    # ──────────────────────────────────────────────────────────
    #  WebSocket connection
    # ──────────────────────────────────────────────────────────
    def _connect_loop(self) -> None:
        reconnect_delay = 1
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=50, ping_timeout=10)
            except Exception as e:
                logger.warning(f"MultiCoinFeed WS error: {e}")

            if not self._running:
                break
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30)

    def _on_open(self, ws) -> None:
        self._connected = True
        logger.info(f"MultiCoinFeed connected — subscribing {len(self.coins)} coins × 3 channels")

        for coin in self.coins:
            # Trade flow — per-trade buy/sell with sizes
            ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": coin},
            }))
            # Best bid/offer — lightweight order book signal
            ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "bbo", "coin": coin},
            }))
            # Live market context — funding, OI, oracle price pushed on change
            ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "activeAssetCtx", "coin": coin},
            }))

    def _on_message(self, ws, message: str) -> None:
        if message == "Websocket connection established.":
            return
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return

        channel = msg.get("channel", "")
        data = msg.get("data")
        if not data:
            return

        if channel == "trades":
            self._handle_trades(data)
        elif channel == "bbo":
            self._handle_bbo(data)
        elif channel in ("activeAssetCtx", "activeSpotAssetCtx"):
            self._handle_asset_ctx(data)

    def _on_error(self, ws, error) -> None:
        logger.warning(f"MultiCoinFeed error: {error}")
        self._connected = False

    def _on_close(self, ws, code, msg) -> None:
        self._connected = False

    # ──────────────────────────────────────────────────────────
    #  Trade handling (same logic as LiveMarketFeed, per-coin)
    # ──────────────────────────────────────────────────────────
    def _handle_trades(self, trades: list[dict]) -> None:
        if not isinstance(trades, list) or not trades:
            return

        coin = trades[0].get("coin", "")
        if coin not in self._buckets:
            return

        with self._lock:
            bucket = self._buckets[coin]
            sizes = self._trade_sizes[coin]

            for t in trades:
                price = float(t.get("px", 0))
                size = float(t.get("sz", 0))
                side = t.get("side", "")

                sizes.append(size)
                self._total_trades += 1

                if side == "A":
                    bucket.buy_volume += size
                    bucket.buy_count += 1
                elif side == "B":
                    bucket.sell_volume += size
                    bucket.sell_count += 1

                bucket.notional += price * size

    # ──────────────────────────────────────────────────────────
    #  BBO handling (best bid/offer — lighter than l2Book)
    # ──────────────────────────────────────────────────────────
    def _handle_bbo(self, data: dict) -> None:
        coin = data.get("coin", "")
        if coin not in self._latest_bbo:
            return

        bbo = data.get("bbo", [None, None])
        bid = bbo[0] if len(bbo) > 0 else None
        ask = bbo[1] if len(bbo) > 1 else None

        bid_px = float(bid["px"]) if bid else 0
        bid_sz = float(bid["sz"]) if bid else 0
        ask_px = float(ask["px"]) if ask else 0
        ask_sz = float(ask["sz"]) if ask else 0

        total = bid_sz + ask_sz
        bid_ratio = bid_sz / total if total > 0 else 0.5
        spread_bps = ((ask_px - bid_px) / bid_px * 10000) if bid_px > 0 and ask_px > 0 else 0

        with self._lock:
            self._latest_bbo[coin] = {
                "bid_px": bid_px,
                "bid_sz": bid_sz,
                "ask_px": ask_px,
                "ask_sz": ask_sz,
                "bid_ratio": round(bid_ratio, 4),
                "spread_bps": round(spread_bps, 2),
            }

    # ──────────────────────────────────────────────────────────
    #  activeAssetCtx — live funding, OI, oracle, volume
    # ──────────────────────────────────────────────────────────
    def _handle_asset_ctx(self, data: dict) -> None:
        coin = data.get("coin", "")
        ctx = data.get("ctx", {})
        if not coin or not ctx:
            return

        with self._lock:
            self._asset_ctx[coin] = {
                "funding": float(ctx.get("funding", 0)),
                "openInterest": float(ctx.get("openInterest", 0)),
                "oraclePx": float(ctx.get("oraclePx", 0)),
                "markPx": float(ctx.get("markPx", 0)),
                "dayNtlVlm": float(ctx.get("dayNtlVlm", 0)),
                "prevDayPx": float(ctx.get("prevDayPx", 0)),
            }

    # ──────────────────────────────────────────────────────────
    #  Trade bucket flush (all coins)
    # ──────────────────────────────────────────────────────────
    def _flush_all_buckets(self) -> None:
        with self._lock:
            for coin in self.coins:
                bucket = self._buckets[coin]
                total_vol = bucket.buy_volume + bucket.sell_volume

                if total_vol == 0:
                    continue

                # Large trade detection
                sizes = self._trade_sizes[coin]
                large_buy = 0.0
                large_sell = 0.0
                if sizes:
                    sizes.sort()
                    median = sizes[len(sizes) // 2]
                    threshold = max(median * 10, 0.001)
                    buy_ratio = bucket.buy_volume / total_vol if total_vol > 0 else 0.5
                    large_total = sum(s for s in sizes if s > threshold)
                    large_buy = large_total * buy_ratio
                    large_sell = large_total * (1 - buy_ratio)

                # Volume delta
                delta = bucket.buy_volume - bucket.sell_volume
                self._volume_delta[coin] = delta
                self._cvd[coin] += delta

                # Track volume history
                self._volume_history[coin].append(total_vol)

                # Imbalance and VWAP
                imbalance = delta / total_vol if total_vol > 0 else 0
                vwap = bucket.notional / total_vol if total_vol > 0 else 0
                trade_count = bucket.buy_count + bucket.sell_count

                self._latest_tape[coin] = {
                    "buy_volume": round(bucket.buy_volume, 6),
                    "sell_volume": round(bucket.sell_volume, 6),
                    "buy_count": bucket.buy_count,
                    "sell_count": bucket.sell_count,
                    "total_volume": round(total_vol, 6),
                    "trade_count": trade_count,
                    "large_buy_volume": round(large_buy, 6),
                    "large_sell_volume": round(large_sell, 6),
                    "vwap": round(vwap, 6),
                    "imbalance": round(imbalance, 4),
                    "tape_speed": trade_count,
                }

                # Reset bucket
                self._buckets[coin] = _TradeBucket()
                self._trade_sizes[coin] = []

    # ──────────────────────────────────────────────────────────
    #  Vault polling (single REST call, partitioned by coin)
    # ──────────────────────────────────────────────────────────
    def _poll_vaults(self) -> None:
        """Fetch vault positions once and partition by coin."""
        import requests

        try:
            r = requests.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "vaultSummaries"},
                timeout=30,
            )
            if r.status_code != 200:
                return

            summaries = r.json()
            if not summaries:
                return

            # Sort by equity, take top 20
            vaults = []
            for v in summaries:
                try:
                    addr = v.get("vaultAddress", "")
                    equity = float(v.get("equity", 0))
                    if equity > 0 and addr:
                        vaults.append((addr, v.get("name", ""), equity))
                except (ValueError, TypeError):
                    continue

            vaults.sort(key=lambda x: x[2], reverse=True)
            top = vaults[:20]

            # Per-coin vault aggregation
            coin_vaults: dict[str, dict] = {c: {"num_long": 0, "num_short": 0,
                "total_vaults": 0, "net_notional": 0.0} for c in self.coins}

            for addr, name, equity in top:
                try:
                    r2 = requests.post(
                        "https://api.hyperliquid.xyz/info",
                        json={"type": "vaultDetails", "vaultAddress": addr},
                        timeout=15,
                    )
                    if r2.status_code != 200:
                        continue

                    details = r2.json()
                    portfolio = details.get("portfolio", [])

                    if isinstance(portfolio, list):
                        for pos in portfolio:
                            asset_pos = pos.get("position", {})
                            coin_name = asset_pos.get("coin", "")
                            szi = float(asset_pos.get("szi", 0))
                            if coin_name in coin_vaults and szi != 0:
                                cv = coin_vaults[coin_name]
                                cv["total_vaults"] += 1
                                entry_px = float(asset_pos.get("entryPx", 0))
                                cv["net_notional"] += szi * entry_px
                                if szi > 0:
                                    cv["num_long"] += 1
                                else:
                                    cv["num_short"] += 1

                    time.sleep(0.3)
                except Exception:
                    continue

            with self._lock:
                for coin, cv in coin_vaults.items():
                    if cv["total_vaults"] > 0:
                        cv["net_notional"] = round(cv["net_notional"], 2)
                        cv["position_change_pct"] = 0.0
                        self._vault_positions[coin] = cv

        except Exception as e:
            logger.warning(f"MultiCoinFeed vault poll error: {e}")

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def total_trades(self) -> int:
        return self._total_trades
