#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = "127.0.0.1"
PORT = 8765
BASE_DIR = Path(__file__).resolve().parent
STRUCTURE_JSON = BASE_DIR / "structure_data.json"


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


HTML = r'''<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Structure Dashboard</title>
<style>
:root{color-scheme:dark;--bg:#0d1117;--panel:#161b22;--panel2:#1f2630;--line:#30363d;--text:#e6edf3;--muted:#8b949e;--good:#3fb950;--bad:#f85149;--warn:#d29922;--blue:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",Arial,sans-serif}.wrap{max-width:1500px;margin:0 auto;padding:20px}.header{display:flex;gap:16px;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;margin-bottom:16px}h1{margin:0;font-size:26px}.sub{color:var(--muted);margin-top:6px;font-size:13px}.controls{display:flex;gap:8px;flex-wrap:wrap}input,select{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:9px 10px}.status{border:1px solid var(--line);background:var(--panel);border-radius:10px;padding:10px 12px;margin-bottom:16px;font-size:13px}.status.bad{border-color:var(--bad);color:#ffb4ad}.summary{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:10px;margin-bottom:16px}.metric{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}.metric .n{font-size:22px;font-weight:700}.metric .l{color:var(--muted);font-size:12px;margin-top:3px}.table-wrap{overflow:auto}table{width:100%;border-collapse:separate;border-spacing:0;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;font-size:13px}th{position:sticky;top:0;background:var(--panel2);z-index:2;color:#c9d1d9;cursor:pointer;user-select:none}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2),th:nth-child(7),td:nth-child(7),th:nth-child(8),td:nth-child(8){text-align:left}tr:last-child td{border-bottom:none}tr:hover td{background:#1b222c}.ticker{font-weight:750;font-size:14px}.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}.blue{color:var(--blue)}.muted{color:var(--muted)}.badge{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 7px;font-size:11px;margin-right:4px}.badge.good{border-color:rgba(63,185,80,.5)}.badge.bad{border-color:rgba(248,81,73,.5)}.badge.warn{border-color:rgba(210,153,34,.5)}.detail{white-space:normal;max-width:360px;line-height:1.45;color:#c9d1d9}@media(max-width:900px){.summary{grid-template-columns:repeat(2,minmax(120px,1fr))}}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div>
      <h1>Stock Structure Dashboard</h1>
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

  <div class="summary">
    <div class="metric"><div class="n" id="mTotal">0</div><div class="l">Symbols</div></div>
    <div class="metric"><div class="n good" id="mBreakout">0</div><div class="l">Breakout</div></div>
    <div class="metric"><div class="n bad" id="mBreakdown">0</div><div class="l">Breakdown</div></div>
    <div class="metric"><div class="n blue" id="mSupport">0</div><div class="l">Near support</div></div>
    <div class="metric"><div class="n warn" id="mResistance">0</div><div class="l">Near resistance</div></div>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th data-key="ticker">Ticker</th>
          <th data-key="event">Event</th>
          <th data-key="price">Price</th>
          <th data-key="support">Support</th>
          <th data-key="resistance">Resistance</th>
          <th data-key="dist_support_pct">To Sup %</th>
          <th data-key="channel">Channel</th>
          <th data-key="ma">MA</th>
          <th data-key="fib">Fib</th>
          <th>Suggestion</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
</div>

<script>
let rawRows=[];let sortKey="ticker";let sortAsc=true;
function fmt(v,digits=2){if(v===null||v===undefined||Number.isNaN(Number(v)))return"—";return Number(v).toFixed(digits)}
function esc(s){return String(s??"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;")}
function eventClass(r){if(r.breakdown)return"bad";if(r.breakout)return"good";if(r.near_resistance)return"warn";if(r.near_support)return"blue";return""}
function maHtml(r){const price=Number(r.price);const ma20=r.ma20==null?null:Number(r.ma20);const ma200=r.ma200==null?null:Number(r.ma200);let bits=[];bits.push(ma20!==null?`<span class="${price>=ma20?"good":"bad"}">20 ${fmt(ma20)}</span>`:`<span class="muted">20 —</span>`);bits.push(ma200!==null?`<span class="${price>=ma200?"good":"bad"}">200 ${fmt(ma200)}</span>`:`<span class="muted">200 —</span>`);return bits.join("<br>")}
function fibHtml(r){return`20D ${esc(r.fib20_position??"—")}<br>60D ${esc(r.fib60_position??"—")}`}
function filterRows(rows){const q=document.getElementById("search").value.trim().toUpperCase();const f=document.getElementById("filter").value;return rows.filter(r=>{if(q&&!r.ticker.includes(q))return false;if(f==="focus"&&!r.focus)return false;if(f==="breakdown"&&!r.breakdown)return false;if(f==="breakout"&&!r.breakout)return false;if(f==="near_support"&&!r.near_support)return false;if(f==="near_resistance"&&!r.near_resistance)return false;return true})}
function sortRows(rows){const out=[...rows];out.sort((a,b)=>{let av,bv;if(sortKey==="ma"){av=a.ma20??-Infinity;bv=b.ma20??-Infinity}else if(sortKey==="fib"){av=a.fib20_position??"";bv=b.fib20_position??""}else{av=a[sortKey];bv=b[sortKey]}if(typeof av==="number"&&typeof bv==="number")return sortAsc?av-bv:bv-av;av=String(av??"");bv=String(bv??"");return sortAsc?av.localeCompare(bv):bv.localeCompare(av)});return out}
function render(){const rows=sortRows(filterRows(rawRows));document.getElementById("rows").innerHTML=rows.map(r=>`<tr><td class="ticker">${esc(r.ticker)}</td><td class="${eventClass(r)}">${r.breakout?'<span class="badge good">突破</span>':""}${r.breakdown?'<span class="badge bad">跌破</span>':""}${r.near_support?'<span class="badge blue">近支撐</span>':""}${r.near_resistance?'<span class="badge warn">近壓力</span>':""}${esc(r.event??"")}</td><td>${fmt(r.price)}</td><td>${fmt(r.support)}</td><td>${fmt(r.resistance)}</td><td>${fmt(r.dist_support_pct)}</td><td>${esc(r.channel??"—")}</td><td>${maHtml(r)}</td><td>${fibHtml(r)}</td><td class="detail">${esc(r.suggestion??"")}</td></tr>`).join("")}
function updateSummary(rows){document.getElementById("mTotal").textContent=rows.length;document.getElementById("mBreakout").textContent=rows.filter(x=>x.breakout).length;document.getElementById("mBreakdown").textContent=rows.filter(x=>x.breakdown).length;document.getElementById("mSupport").textContent=rows.filter(x=>x.near_support).length;document.getElementById("mResistance").textContent=rows.filter(x=>x.near_resistance).length}
async function refresh(){const status=document.getElementById("status");try{const resp=await fetch("/api/structure?_="+Date.now(),{cache:"no-store"});const data=await resp.json();if(!data.ok){status.className="status bad";status.textContent="讀取失敗："+(data.error||"unknown error");return}rawRows=Object.entries(data.symbols).map(([ticker,r])=>({ticker,...r}));document.getElementById("subtitle").textContent=`Structure generated: ${data.generated_at||"—"} (${data.timezone||"—"})`;status.className="status";status.textContent=`已讀取 ${rawRows.length} 檔｜JSON: ${data.path}｜頁面每 30 秒重新讀取一次`;updateSummary(rawRows);render()}catch(err){status.className="status bad";status.textContent="Dashboard API 錯誤："+err}}
document.getElementById("search").addEventListener("input",render);document.getElementById("filter").addEventListener("change",render);document.querySelectorAll("th[data-key]").forEach(th=>{th.addEventListener("click",()=>{const k=th.dataset.key;if(sortKey===k)sortAsc=!sortAsc;else{sortKey=k;sortAsc=true}render()})});refresh();setInterval(refresh,30000);
</script>
</body>
</html>'''


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
            payload = json.dumps(load_structure_data(), ensure_ascii=False, allow_nan=False).encode("utf-8")
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
    print("=" * 64)
    print("Stock Structure Live Dashboard")
    print("=" * 64)
    print(f"JSON : {STRUCTURE_JSON}")
    print(f"URL  : http://{HOST}:{PORT}")

    initial = load_structure_data()
    if initial["ok"]:
        print(f"OK   : {len(initial['symbols'])} symbols (generated_at={initial['generated_at']})")
    else:
        print(f"WARN : {initial['error']}")
        print("       請確認 structure_data.json 與 live_dashboard.py 在同一資料夾。")

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
