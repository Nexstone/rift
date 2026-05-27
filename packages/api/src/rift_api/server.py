"""REST API server for RIFT.

Lightweight HTTP API that reads the same state files the TUI and MCP
server read. No new logic — just an HTTP wrapper for institutional
dashboards, monitoring systems, and PMS integrations.

Usage:
    python -m rift.cli api-start --port 8420
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import sys
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse, parse_qs

ALGO_DIR = Path.home() / ".rift" / "algo"
SESSIONS_DIR = LIVE_DIR / "sessions"
PIDS_DIR = LIVE_DIR / "pids"
SESSION_LOGS_DIR = Path.home() / ".rift" / "algo_sessions"
API_PID_FILE = LIVE_DIR / "api.pid"
API_TOKEN_FILE = Path.home() / ".rift" / "api_token"


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _get_or_create_token() -> str:
    """Get existing API token or generate a new one."""
    if API_TOKEN_FILE.exists():
        return API_TOKEN_FILE.read_text().strip()
    token = secrets.token_urlsafe(32)
    API_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    API_TOKEN_FILE.write_text(token)
    os.chmod(str(API_TOKEN_FILE), 0o600)
    return token


class RiftAPIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the RIFT REST API."""

    server_version = "RIFT/1.0"
    api_token: str = ""
    require_auth_all: bool = False

    def log_message(self, format, *args):
        pass  # Suppress default logging

    def _send_json(self, data: dict | list, status: int = 200) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, msg: str) -> None:
        self._send_json({"error": msg}, status)

    def _check_auth(self, require: bool = False) -> bool:
        """Check bearer token auth. Returns True if authorized."""
        if not require and not self.require_auth_all:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {self.api_token}":
            return True
        self._send_error(401, "Unauthorized — include Authorization: Bearer <token>")
        return False

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        if not self._check_auth():
            return

        if path == "" or path == "/":
            self._handle_root()
        elif path == "/status":
            self._handle_status()
        elif path == "/positions":
            self._handle_positions()
        elif path == "/trades":
            self._handle_trades(params)
        elif path == "/alerts":
            self._handle_alerts(params)
        elif path == "/tca":
            self._handle_tca()
        elif path == "/attribution":
            self._handle_attribution()
        elif path == "/health":
            self._handle_health()
        elif path == "/equity":
            self._handle_equity()
        elif path == "/scout":
            self._handle_scout(params)
        elif path == "/var":
            self._handle_var(params)
        elif path == "/audit":
            self._handle_audit(params)
        elif path.startswith("/session/"):
            key = path.split("/session/", 1)[1]
            self._handle_session(key)
        else:
            self._send_error(404, f"Not found: {path}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # POST endpoints always require auth
        if not self._check_auth(require=True):
            return

        if path == "/stop":
            self._handle_stop_all()
        elif path.startswith("/stop/"):
            key = path.split("/stop/", 1)[1]
            self._handle_stop_session(key)
        else:
            self._send_error(404, f"Not found: {path}")

    # ─── GET Handlers ───

    def _handle_root(self):
        self._send_json({
            "service": "RIFT REST API",
            "version": "1.0",
            "endpoints": [
                "GET /status", "GET /positions", "GET /trades",
                "GET /alerts", "GET /tca", "GET /attribution",
                "GET /health", "GET /equity", "GET /scout", "GET /var", "GET /audit",
                "GET /session/:key",
                "POST /stop", "POST /stop/:key",
            ],
        })

    def _handle_status(self):
        # Portfolio supervisor state
        sup = _load_json(LIVE_DIR / "supervisor.json")
        if sup:
            pid = sup.get("pid")
            if pid and not _is_pid_alive(pid):
                sup["running"] = False
            self._send_json(sup)
            return

        # Fall back to individual sessions
        sessions = self._list_sessions()
        self._send_json({"supervisor": None, "sessions": sessions})

    def _handle_positions(self):
        positions = []
        if not SESSIONS_DIR.exists():
            self._send_json({"positions": []})
            return
        for f in SESSIONS_DIR.glob("*.json"):
            snapshot = _load_json(f)
            if snapshot and snapshot.get("state", {}).get("position"):
                state = snapshot["state"]
                positions.append({
                    "key": f.stem,
                    "strategy": state.get("strategy"),
                    "pair": state.get("pair"),
                    "position": state["position"],
                    "equity": state.get("total_equity"),
                    "unrealized_pnl": state.get("unrealized_pnl"),
                })
        self._send_json({"positions": positions})

    def _handle_trades(self, params):
        limit = int(params.get("limit", ["50"])[0])
        all_trades: list[dict] = []
        if SESSION_LOGS_DIR.exists():
            for f in sorted(SESSION_LOGS_DIR.glob("LIVE_*.json"), reverse=True):
                data = _load_json(f)
                if data and "trades" in data:
                    for t in data["trades"]:
                        t["session"] = f.stem
                        t["strategy"] = data.get("strategy")
                        t["pair"] = data.get("pair")
                    all_trades.extend(data["trades"])
                if len(all_trades) >= limit:
                    break
        self._send_json({"trades": all_trades[:limit], "total": len(all_trades)})

    def _handle_alerts(self, params):
        limit = int(params.get("limit", ["20"])[0])
        alerts_file = LIVE_DIR / "alerts.log"
        alerts: list[dict] = []
        if alerts_file.exists():
            for line in alerts_file.read_text().strip().split("\n"):
                if line.strip():
                    try:
                        alerts.append(json.loads(line))
                    except Exception:
                        pass
        self._send_json({"alerts": alerts[-limit:]})

    def _handle_tca(self):
        try:
            from rift_engine.tca import analyze_all_sessions
            import dataclasses
            report = analyze_all_sessions()
            self._send_json(dataclasses.asdict(report))
        except Exception as e:
            self._send_error(500, str(e))

    def _handle_attribution(self):
        try:
            from rift_engine.attribution import attribute_all_sessions
            import dataclasses
            report = attribute_all_sessions()
            self._send_json(dataclasses.asdict(report))
        except Exception as e:
            self._send_error(500, str(e))

    def _handle_health(self):
        health: list[dict] = []
        if SESSIONS_DIR.exists():
            for f in SESSIONS_DIR.glob("*.json"):
                snapshot = _load_json(f)
                if snapshot and snapshot.get("state"):
                    state = snapshot["state"]
                    health.append({
                        "key": f.stem,
                        "strategy": state.get("strategy"),
                        "pair": state.get("pair"),
                        "health_score": state.get("health_score"),
                        "health_grade": state.get("health_grade"),
                        "health_paused": state.get("health_paused"),
                    })
        self._send_json({"strategies": health})

    def _handle_equity(self):
        equities: list[dict] = []
        if SESSIONS_DIR.exists():
            for f in SESSIONS_DIR.glob("*.json"):
                snapshot = _load_json(f)
                if snapshot and snapshot.get("state"):
                    state = snapshot["state"]
                    equities.append({
                        "key": f.stem,
                        "strategy": state.get("strategy"),
                        "pair": state.get("pair"),
                        "equity": state.get("total_equity"),
                        "initial_equity": state.get("initial_equity"),
                        "pnl_pct": state.get("total_pnl_pct"),
                        "peak_equity": state.get("peak_equity"),
                    })
        self._send_json({"equities": equities})

    def _handle_scout(self, params):
        top = int(params.get("top", ["20"])[0])
        try:
            from rift_research.scout import scan_market
            import dataclasses
            opps = scan_market(top_n=top)
            self._send_json({"opportunities": [dataclasses.asdict(o) for o in opps]})
        except Exception as e:
            self._send_error(500, str(e))

    def _handle_var(self, params):
        horizon = params.get("horizon", ["24h"])[0]
        try:
            from rift_portfolio.var import var_from_sessions
            import dataclasses
            report = var_from_sessions()
            report.horizon = horizon
            self._send_json(dataclasses.asdict(report))
        except Exception as e:
            self._send_error(500, str(e))

    def _handle_audit(self, params):
        fmt = params.get("format", ["json"])[0]
        days = int(params.get("days", ["30"])[0])
        strategy = params.get("strategy", [""])[0]
        try:
            from rift_trade.audit import export_audit_trail
            path = export_audit_trail(output_format=fmt, last_days=days, strategy=strategy)
            if fmt == "csv" and path:
                # Return CSV content directly
                content = Path(path).read_text()
                self._send_json({"format": "csv", "path": path, "rows": content.count("\n") - 1})
            elif path:
                data = json.loads(Path(path).read_text())
                self._send_json(data)
            else:
                self._send_json({"audit_trail": [], "count": 0})
        except Exception as e:
            self._send_error(500, str(e))

    def _handle_session(self, key: str):
        state_file = SESSIONS_DIR / f"{key}.json"
        snapshot = _load_json(state_file)
        if snapshot:
            self._send_json(snapshot)
        else:
            self._send_error(404, f"Session {key} not found")

    # ─── POST Handlers ───

    def _handle_stop_all(self):
        from rift_trade.supervisor import stop_supervisor, is_supervisor_running
        if is_supervisor_running():
            result = stop_supervisor()
            self._send_json(result)
        else:
            self._send_json({"status": "no_supervisor", "msg": "No portfolio supervisor running"})

    def _handle_stop_session(self, key: str):
        parts = key.rsplit("_", 1)
        if len(parts) != 2:
            self._send_error(400, f"Invalid session key: {key}. Expected format: strategy_COIN")
            return
        strategy, coin = parts
        from rift_trade.algo import stop_algo_session
        result = stop_algo_session(strategy, coin)
        self._send_json(result)

    def _list_sessions(self) -> list[dict]:
        sessions = []
        if not PIDS_DIR.exists():
            return sessions
        for f in PIDS_DIR.glob("*.pid"):
            key = f.stem
            try:
                pid = int(f.read_text().strip())
            except (ValueError, FileNotFoundError):
                continue
            if _is_pid_alive(pid):
                snapshot = _load_json(SESSIONS_DIR / f"{key}.json")
                state = snapshot.get("state", {}) if snapshot else {}
                sessions.append({
                    "key": key, "pid": pid,
                    "strategy": state.get("strategy", ""),
                    "pair": state.get("pair", ""),
                    "equity": state.get("total_equity", 0),
                    "pnl_pct": state.get("total_pnl_pct", 0),
                })
        return sessions


def run_api(port: int = 8420, require_auth: bool = False) -> None:
    """Start the REST API server."""
    token = _get_or_create_token()

    RiftAPIHandler.api_token = token
    RiftAPIHandler.require_auth_all = require_auth

    # Write PID
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    API_PID_FILE.write_text(str(os.getpid()))

    server = HTTPServer(("0.0.0.0", port), RiftAPIHandler)

    # Graceful shutdown
    def handle_shutdown(signum, frame):
        server.shutdown()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    print(json.dumps({
        "type": "result",
        "command": "api-start",
        "status": "started",
        "port": port,
        "url": f"http://localhost:{port}",
        "token": token,
        "require_auth": require_auth,
    }), flush=True)

    try:
        server.serve_forever()
    finally:
        API_PID_FILE.unlink(missing_ok=True)


def stop_api() -> dict:
    """Stop a running API server."""
    if not API_PID_FILE.exists():
        return {"status": "not_running"}
    try:
        pid = int(API_PID_FILE.read_text().strip())
        if not _is_pid_alive(pid):
            API_PID_FILE.unlink(missing_ok=True)
            return {"status": "not_running"}
        os.kill(pid, signal.SIGTERM)
        API_PID_FILE.unlink(missing_ok=True)
        return {"status": "stopped", "pid": pid}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def is_api_running() -> bool:
    if not API_PID_FILE.exists():
        return False
    try:
        pid = int(API_PID_FILE.read_text().strip())
        return _is_pid_alive(pid)
    except (ValueError, FileNotFoundError):
        return False
