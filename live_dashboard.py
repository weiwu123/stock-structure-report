#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# live_dashboard.py
# Step 2:
# - Reads structure_data.json
# - Serves local dashboard at http://127.0.0.1:8765
# - Connects to Finnhub WebSocket for live trades
# - Fetches previous close once at startup
# - Calculates live price, day %, 5m momentum, 10m momentum
#
# Requirement:
#   pip install websocket-client
#
# Environment:
#   FINNHUB_API_KEY=<your key>

import json
import os
import ssl
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    import websocket
except ImportError:
    websocket = None

HOST = "127.0.0.1"
PORT = 8765

BASE_DIR = Path(__file__).resolve().parent
STRUCTURE_JSON = BASE_DIR / "structure_data.json"

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
FINNHUB_WS_URL = (
    f"wss://ws.finnhub.io?token={urllib.parse.quote(FINNHUB_API_KEY)}"
    if FINNHUB_API_KEY
    else None
)

BENCHMARKS = ["QQQ", "SPY", "SMH"]

STATE_LOCK = threading.Lock()

LIVE = defaultdict(lambda: {
    "price": None,
    "trade_ts": None,
    "prev_close": None,
    "day_pct": None,
    "mom_5m": None,
    "mom_10m": None,
})

HISTORY = defaultdict(lambda: deque(maxlen=10000))

WS_STATUS = {
    "connected": False,
    "message": "not started",
    "last_message_ts": None,
}


def safe_float(v):
    try:
        x = float(v)
        if x != x or x in (float("inf"), float("-inf")):
            return None
        return x
    except Exception:
        return None


def load_structure_data():
    if not STRUCTURE_JSON.exists():
        return {
            "ok": False,
            "error": f"找不到 {STRUCTURE_JSON.name}",
            "path": str(STRUCTURE_JSON),
            "generated_at": None,
            "timezone": None,
            "symbols": {},
        }

    try:
        with STRUCTURE_JSON.open("r", encoding="utf-8") as f:
            data = json.load(f)

        symbols = data.get("symbols")
        if not isinstance(symbols, dict):
            raise ValueError("structure_data.json 缺少 symbols 物件")

        return {
            "ok": True,
            "error": None,
            "path": str(STRUCTURE_JSON),
            "generated_at": data.get("generated_at"),
            "timezone": data.get("timezone"),
            "symbols": symbols,
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "path": str(STRUCTURE_JSON),
            "generated_at": None,
            "timezone": None,
            "symbols": {},
        }


def all_symbols():
    data = load_structure_data()
    symbols = list(data.get("symbols", {}).keys())
    for s in BENCHMARKS:
        if s not in symbols:
            symbols.append(s)
    return symbols


def finnhub_quote(symbol):
    if not FINNHUB_API_KEY:
        return None

    url = (
        "https://finnhub.io/api/v1/quote?"
        + urllib.parse.urlencode({"symbol": symbol, "token": FINNHUB_API_KEY})
    )

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "stock-structure-live-dashboard/1.0"},
    )

    with urllib.request.urlopen(req, timeout=8) as resp:
        obj = json.loads(resp.read().decode("utf-8"))

    return {
        "current": safe_float(obj.get("c")),
        "prev_close": safe_float(obj.get("pc")),
    }


def bootstrap_quotes(symbols):
    if not FINNHUB_API_KEY:
        print("WARN : FINNHUB_API_KEY 未設定，live feed 不會啟動。")
        return

    print(f"Quote bootstrap: {len(symbols)} symbols")

    for idx, symbol in enumerate(symbols, 1):
        try:
            q = finnhub_quote(symbol)
            if not q:
                continue

            now = time.time()

            with STATE_LOCK:
                if q["prev_close"] is not None:
                    LIVE[symbol]["prev_close"] = q["prev_close"]

                if q["current"] is not None and q["current"] > 0:
                    LIVE[symbol]["price"] = q["current"]
                    LIVE[symbol]["trade_ts"] = now
                    HISTORY[symbol].append((now, q["current"]))

                    pc = LIVE[symbol]["prev_close"]
                    if pc:
                        LIVE[symbol]["day_pct"] = (
                            (q["current"] / pc) - 1.0
                        ) * 100.0

            print(f"  [{idx:02d}/{len(symbols)}] {symbol}: ok")

        except Exception as exc:
            print(f"  [{idx:02d}/{len(symbols)}] {symbol}: quote error: {exc}")

        time.sleep(0.25)


def price_at_or_before(history, target_ts):
    if not history:
        return None

    for ts, price in reversed(history):
        if ts <= target_ts:
            return price

    return None


def calculate_momentum(symbol, now_ts):
    hist = HISTORY[symbol]

    if not hist:
        return None, None

    current = hist[-1][1]
    p5 = price_at_or_before(hist, now_ts - 5 * 60)
    p10 = price_at_or_before(hist, now_ts - 10 * 60)

    mom5 = ((current / p5) - 1.0) * 100.0 if p5 and p5 > 0 else None
    mom10 = ((current / p10) - 1.0) * 100.0 if p10 and p10 > 0 else None

    return mom5, mom10


def cleanup_history(now_ts):
    cutoff = now_ts - 20 * 60

    for hist in HISTORY.values():
        while hist and hist[0][0] < cutoff:
            hist.popleft()


def process_trade(symbol, price, trade_ts):
    if not symbol or price is None or price <= 0:
        return

    now_ts = trade_ts if trade_ts else time.time()

    with STATE_LOCK:
        HISTORY[symbol].append((now_ts, price))
        cleanup_history(now_ts)

        LIVE[symbol]["price"] = price
        LIVE[symbol]["trade_ts"] = now_ts

        pc = LIVE[symbol]["prev_close"]
        if pc and pc > 0:
            LIVE[symbol]["day_pct"] = ((price / pc) - 1.0) * 100.0

        mom5, mom10 = calculate_momentum(symbol, now_ts)
        LIVE[symbol]["mom_5m"] = mom5
        LIVE[symbol]["mom_10m"] = mom10


def ws_on_open(ws):
    symbols = all_symbols()

    with STATE_LOCK:
        WS_STATUS["connected"] = True
        WS_STATUS["message"] = f"connected / subscribed {len(symbols)} symbols"

    print(f"Finnhub WebSocket connected. Subscribing {len(symbols)} symbols...")

    for symbol in symbols:
        try:
            ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
        except Exception as exc:
            print(f"{symbol}: subscribe error: {exc}")


def ws_on_message(ws, message):
    try:
        obj = json.loads(message)
    except Exception:
        return

    with STATE_LOCK:
        WS_STATUS["last_message_ts"] = time.time()

    if obj.get("type") != "trade":
        return

    for trade in obj.get("data", []):
        symbol = trade.get("s")
        price = safe_float(trade.get("p"))
        ts_ms = safe_float(trade.get("t"))
        trade_ts = ts_ms / 1000.0 if ts_ms else time.time()

        process_trade(symbol, price, trade_ts)


def ws_on_error(ws, error):
    with STATE_LOCK:
        WS_STATUS["connected"] = False
        WS_STATUS["message"] = f"error: {error}"
    print(f"Finnhub WebSocket error: {error}")


def ws_on_close(ws, status_code, msg):
    with STATE_LOCK:
        WS_STATUS["connected"] = False
        WS_STATUS["message"] = f"closed: {status_code} {msg or ''}".strip()
    print(f"Finnhub WebSocket closed: {status_code} {msg}")


def websocket_loop():
    if websocket is None:
        with STATE_LOCK:
            WS_STATUS["message"] = (
                "websocket-client 未安裝。執行: pip install websocket-client"
            )
        return

    if not FINNHUB_API_KEY:
        with STATE_LOCK:
            WS_STATUS["message"] = "FINNHUB_API_KEY 未設定"
        return

    while True:
        try:
            app = websocket.WebSocketApp(
                FINNHUB_WS_URL,
                on_open=ws_on_open,
                on_message=ws_on_message,
                on_error=ws_on_error,
                on_close=ws_on_close,
            )

            app.run_forever(
                ping_interval=20,
                ping_timeout=10,
                sslopt={"cert_reqs": ssl.CERT_REQUIRED},
            )

        except Exception as exc:
            with STATE_LOCK:
                WS_STATUS["connected"] = False
                WS_STATUS["message"] = f"reconnect error: {exc}"
            print(f"WebSocket loop error: {exc}")

        time.sleep(5)


def live_snapshot():
    with STATE_LOCK:
        live = {symbol: dict(d) for symbol, d in LIVE.items()}
        ws = dict(WS_STATUS)

    return {
        "ok": True,
        "websocket": ws,
        "symbols": live,
        "server_time": time.time(),
    }


HTML = r'''<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Structure Live Dashboard</title>
<style>
:root {
  color-scheme: dark;
  --bg: #0d1117;
  --panel: #161b22;
  --panel2: #1f2630;
  --line: #30363d;
  --text: #e6edf3;
  --muted: #8b949e;
  --good: #3fb950;
  --bad: #f85149;
  --warn: #d29922;
  --blue: #58a6ff;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",Arial,sans-serif;
}
.wrap { max-width: 1650px; margin: 0 auto; padding: 20px; }
.header {
  display: flex;
  gap: 16px;
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
h1 { margin: 0; font-size: 26px; }
.sub { color: var(--muted); margin-top: 6px; font-size: 13px; }
.controls { display: flex; gap: 8px; flex-wrap: wrap; }
input, select {
  background: var(--panel);
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 9px 10px;
}
.status {
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: 10px;
  padding: 10px 12px;
  margin-bottom: 16px;
  font-size: 13px;
}
.status.bad { border-color: var(--bad); color: #ffb4ad; }
.summary {
  display: grid;
  grid-template-columns: repeat(6, minmax(120px,1fr));
  gap: 10px;
  margin-bottom: 16px;
}
.metric {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 12px;
}
.metric .n { font-size: 22px; font-weight: 700; }
.metric .l { color: var(--muted); font-size: 12px; margin-top: 3px; }
table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  overflow: hidden;
}
th, td {
  padding: 8px 9px;
  border-bottom: 1px solid var(--line);
  text-align: right;
  white-space: nowrap;
  font-size: 13px;
}
th {
  position: sticky;
  top: 0;
  background: var(--panel2);
  z-index: 2;
  color: #c9d1d9;
  cursor: pointer;
  user-select: none;
}
th:first-child, td:first-child,
th:nth-child(2), td:nth-child(2),
th:nth-child(10), td:nth-child(10) { text-align: left; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #1b222c; }
.ticker { font-weight: 750; font-size: 14px; }
.good { color: var(--good); }
.bad { color: var(--bad); }
.warn { color: var(--warn); }
.blue { color: var(--blue); }
.muted { color: var(--muted); }
.live { font-weight: 700; }
.badge {
  display: inline-block;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 2px 7px;
  font-size: 11px;
  margin-right: 4px;
}
.badge.good { border-color: rgba(63,185,80,.5); }
.badge.bad { border-color: rgba(248,81,73,.5); }
.badge.warn { border-color: rgba(210,153,34,.5); }
.detail {
  white-space: normal;
  max-width: 310px;
  line-height: 1.4;
  color: #c9d1d9;
}
.benchmarks {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.bench {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 9px;
  padding: 8px 11px;
  font-size: 13px;
}
@media (max-width: 900px) {
  .summary { grid-template-columns: repeat(2, minmax(120px,1fr)); }
  .table-wrap { overflow-x: auto; }
}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div>
      <h1>Stock Structure Live Dashboard</h1>
      <div class="sub" id="subtitle">讀取 structure_data.json...</div>
    </div>
    <div class="controls">
      <input id="search" placeholder="搜尋 ticker，例如 AMD">
      <select id="filter">
        <option value="all">全部</option>
        <option value="focus">Focus</option>
        <option value="breakdown">Breakdown</option>
        <option value="breakout">Breakout</option>
        <option value="near_support">靠近支撐</option>
        <option value="near_resistance">靠近壓力</option>
      </select>
    </div>
  </div>

  <div id="status" class="status">載入中...</div>
  <div class="benchmarks" id="benchmarks"></div>

  <div class="summary">
    <div class="metric"><div class="n" id="mTotal">0</div><div class="l">Symbols</div></div>
    <div class="metric"><div class="n good" id="mBreakout">0</div><div class="l">Breakout</div></div>
    <div class="metric"><div class="n bad" id="mBreakdown">0</div><div class="l">Breakdown</div></div>
    <div class="metric"><div class="n blue" id="mSupport">0</div><div class="l">Near support</div></div>
    <div class="metric"><div class="n warn" id="mResistance">0</div><div class="l">Near resistance</div></div>
    <div class="metric"><div class="n" id="mLive">0</div><div class="l">Live prices</div></div>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th data-key="ticker">Ticker</th>
          <th data-key="event">Event</th>
          <th data-key="live_price">Live</th>
          <th data-key="day_pct">Day %</th>
          <th data-key="mom_5m">5m %</th>
          <th data-key="mom_10m">10m %</th>
          <th data-key="support">Support</th>
          <th data-key="resistance">Resistance</th>
          <th data-key="dist_live_support">To Sup %</th>
          <th data-key="channel">Channel</th>
          <th data-key="ma">MA</th>
          <th>Suggestion</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
</div>

<script>
let structureRows = [];
let liveMap = {};
let wsInfo = {};
let sortKey = "ticker";
let sortAsc = true;

function fmt(v, digits=2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return Number(v).toFixed(digits);
}

function esc(s) {
  return String(s ?? "")
    .replaceAll("&","&amp;")
    .replaceAll("<","&lt;")
    .replaceAll(">","&gt;")
    .replaceAll('"',"&quot;");
}

function pctClass(v) {
  if (v === null || v === undefined) return "muted";
  if (Number(v) > 0) return "good";
  if (Number(v) < 0) return "bad";
  return "";
}

function eventClass(r) {
  if (r.breakdown) return "bad";
  if (r.breakout) return "good";
  if (r.near_resistance) return "warn";
  if (r.near_support) return "blue";
  return "";
}

function maHtml(r) {
  const price = Number(r.live_price ?? r.price);
  const ma20 = r.ma20 == null ? null : Number(r.ma20);
  const ma200 = r.ma200 == null ? null : Number(r.ma200);
  let bits = [];

  if (ma20 !== null) {
    bits.push(`<span class="${price >= ma20 ? "good" : "bad"}">20 ${fmt(ma20)}</span>`);
  } else {
    bits.push(`<span class="muted">20 —</span>`);
  }

  if (ma200 !== null) {
    bits.push(`<span class="${price >= ma200 ? "good" : "bad"}">200 ${fmt(ma200)}</span>`);
  } else {
    bits.push(`<span class="muted">200 —</span>`);
  }

  return bits.join("<br>");
}

function mergedRows() {
  return structureRows.map(r => {
    const l = liveMap[r.ticker] || {};
    const livePrice = l.price ?? null;

    let distLiveSupport = null;
    if (livePrice != null && r.support != null && Number(r.support) !== 0) {
      distLiveSupport = ((Number(livePrice) / Number(r.support)) - 1) * 100;
    }

    return {
      ...r,
      live_price: livePrice,
      day_pct: l.day_pct ?? null,
      mom_5m: l.mom_5m ?? null,
      mom_10m: l.mom_10m ?? null,
      dist_live_support: distLiveSupport
    };
  });
}

function filterRows(rows) {
  const q = document.getElementById("search").value.trim().toUpperCase();
  const f = document.getElementById("filter").value;

  return rows.filter(r => {
    if (q && !r.ticker.includes(q)) return false;
    if (f === "focus" && !r.focus) return false;
    if (f === "breakdown" && !r.breakdown) return false;
    if (f === "breakout" && !r.breakout) return false;
    if (f === "near_support" && !r.near_support) return false;
    if (f === "near_resistance" && !r.near_resistance) return false;
    return true;
  });
}

function sortRows(rows) {
  const out = [...rows];

  out.sort((a,b) => {
    let av = sortKey === "ma" ? a.ma20 : a[sortKey];
    let bv = sortKey === "ma" ? b.ma20 : b[sortKey];

    if (typeof av === "number" && typeof bv === "number") {
      return sortAsc ? av - bv : bv - av;
    }

    if (av == null && bv != null) return 1;
    if (av != null && bv == null) return -1;

    av = String(av ?? "");
    bv = String(bv ?? "");
    return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
  });

  return out;
}

function render() {
  const rows = sortRows(filterRows(mergedRows()));
  const body = document.getElementById("rows");

  body.innerHTML = rows.map(r => `
    <tr>
      <td class="ticker">${esc(r.ticker)}</td>
      <td class="${eventClass(r)}">
        ${r.breakout ? '<span class="badge good">突破</span>' : ""}
        ${r.breakdown ? '<span class="badge bad">跌破</span>' : ""}
        ${r.near_support ? '<span class="badge blue">近支撐</span>' : ""}
        ${r.near_resistance ? '<span class="badge warn">近壓力</span>' : ""}
        ${esc(r.event ?? "")}
      </td>
      <td class="live">${fmt(r.live_price)}</td>
      <td class="${pctClass(r.day_pct)}">${fmt(r.day_pct)}${r.day_pct == null ? "" : "%"}</td>
      <td class="${pctClass(r.mom_5m)}">${fmt(r.mom_5m)}${r.mom_5m == null ? "" : "%"}</td>
      <td class="${pctClass(r.mom_10m)}">${fmt(r.mom_10m)}${r.mom_10m == null ? "" : "%"}</td>
      <td>${fmt(r.support)}</td>
      <td>${fmt(r.resistance)}</td>
      <td class="${pctClass(r.dist_live_support)}">${fmt(r.dist_live_support)}${r.dist_live_support == null ? "" : "%"}</td>
      <td>${esc(r.channel ?? "—")}</td>
      <td>${maHtml(r)}</td>
      <td class="detail">${esc(r.suggestion ?? "")}</td>
    </tr>
  `).join("");

  document.getElementById("mLive").textContent =
    rows.filter(r => r.live_price != null).length;
}

function updateSummary(rows) {
  document.getElementById("mTotal").textContent = rows.length;
  document.getElementById("mBreakout").textContent = rows.filter(x => x.breakout).length;
  document.getElementById("mBreakdown").textContent = rows.filter(x => x.breakdown).length;
  document.getElementById("mSupport").textContent = rows.filter(x => x.near_support).length;
  document.getElementById("mResistance").textContent = rows.filter(x => x.near_resistance).length;
}

function renderBenchmarks() {
  const box = document.getElementById("benchmarks");

  box.innerHTML = ["QQQ","SPY","SMH"].map(t => {
    const d = liveMap[t] || {};
    return `
      <div class="bench">
        <b>${t}</b>&nbsp;
        ${fmt(d.price)}
        &nbsp;<span class="${pctClass(d.day_pct)}">${fmt(d.day_pct)}${d.day_pct == null ? "" : "%"}</span>
        &nbsp;<span class="${pctClass(d.mom_5m)}">5m ${fmt(d.mom_5m)}${d.mom_5m == null ? "" : "%"}</span>
      </div>
    `;
  }).join("");
}

async function refreshStructure() {
  try {
    const resp = await fetch("/api/structure?_=" + Date.now(), {cache:"no-store"});
    const data = await resp.json();

    if (!data.ok) {
      document.getElementById("status").className = "status bad";
      document.getElementById("status").textContent =
        "Structure JSON 讀取失敗：" + (data.error || "unknown error");
      return;
    }

    structureRows = Object.entries(data.symbols).map(([ticker, r]) => ({ticker, ...r}));

    document.getElementById("subtitle").textContent =
      `Structure generated: ${data.generated_at || "—"} (${data.timezone || "—"})`;

    updateSummary(structureRows);
    render();

  } catch (err) {
    document.getElementById("status").className = "status bad";
    document.getElementById("status").textContent =
      "Structure API 錯誤：" + err;
  }
}

async function refreshLive() {
  try {
    const resp = await fetch("/api/live?_=" + Date.now(), {cache:"no-store"});
    const data = await resp.json();

    liveMap = data.symbols || {};
    wsInfo = data.websocket || {};

    const status = document.getElementById("status");

    if (wsInfo.connected) {
      status.className = "status";
      status.textContent =
        `Finnhub LIVE connected｜${wsInfo.message || ""}｜頁面每 2 秒更新`;
    } else {
      status.className = "status bad";
      status.textContent =
        `Finnhub 尚未連線｜${wsInfo.message || "unknown"}`;
    }

    renderBenchmarks();
    render();

  } catch (err) {
    const status = document.getElementById("status");
    status.className = "status bad";
    status.textContent = "Live API 錯誤：" + err;
  }
}

document.getElementById("search").addEventListener("input", render);
document.getElementById("filter").addEventListener("change", render);

document.querySelectorAll("th[data-key]").forEach(th => {
  th.addEventListener("click", () => {
    const k = th.dataset.key;
    if (sortKey === k) {
      sortAsc = !sortAsc;
    } else {
      sortKey = k;
      sortAsc = true;
    }
    render();
  });
});

refreshStructure();
refreshLive();

setInterval(refreshLive, 2000);
setInterval(refreshStructure, 30000);
</script>
</body>
</html>
'''


class DashboardHandler(BaseHTTPRequestHandler):
    def _send_bytes(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            self._send_bytes(HTML.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/structure":
            payload = json.dumps(
                load_structure_data(),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            self._send_bytes(payload, "application/json; charset=utf-8")
            return

        if path == "/api/live":
            payload = json.dumps(
                live_snapshot(),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            self._send_bytes(payload, "application/json; charset=utf-8")
            return

        if path == "/favicon.ico":
            self._send_bytes(b"", "image/x-icon", status=204)
            return

        self._send_bytes(b"Not Found", "text/plain; charset=utf-8", status=404)

    def log_message(self, fmt, *args):
        return


def open_browser():
    webbrowser.open(f"http://{HOST}:{PORT}")


def main():
    print("=" * 68)
    print("Stock Structure Live Dashboard - Step 2")
    print("=" * 68)
    print(f"JSON : {STRUCTURE_JSON}")
    print(f"URL  : http://{HOST}:{PORT}")

    initial = load_structure_data()

    if initial["ok"]:
        print(
            f"OK   : {len(initial['symbols'])} structure symbols "
            f"(generated_at={initial['generated_at']})"
        )
    else:
        print(f"WARN : {initial['error']}")

    if websocket is None:
        print("WARN : websocket-client 未安裝")
        print("       pip install websocket-client")

    if not FINNHUB_API_KEY:
        print("WARN : FINNHUB_API_KEY 未設定")
        print('       PowerShell: $env:FINNHUB_API_KEY="YOUR_KEY"')

    symbols = all_symbols()

    t_boot = threading.Thread(
        target=bootstrap_quotes,
        args=(symbols,),
        daemon=True,
    )
    t_boot.start()

    t_ws = threading.Thread(
        target=websocket_loop,
        daemon=True,
    )
    t_ws.start()

    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)

    timer = threading.Timer(0.8, open_browser)
    timer.daemon = True
    timer.start()

    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
