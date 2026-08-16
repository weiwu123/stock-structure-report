import html
from datetime import datetime

import numpy as np
import pytz
import yfinance as yf

CORE_LIST = [
    "AAPL", "AMD", "AMZN", "ANET", "AVGO", "CSCO", "DELL", "GOOGL",
    "IBM", "INTC", "MRVL", "MSFT", "MU", "NET", "NOW", "NVDA",
    "ORCL", "PLTR", "QCOM", "SNOW", "TSLA", "TSM", "QQQM", "HPE",
]

BENCHMARKS = [
    ("QQQ", "納指ETF"),
    ("SPY", "標普ETF"),
    ("NQ=F", "納指期貨"),
    ("ES=F", "標普期貨"),
    ("SOXL", "半導體3x"),
]


def _safe_float(x):
    try:
        if x is None:
            return None
        v = float(x)
        if np.isnan(v) or np.isinf(v):
            return None
        return v
    except Exception:
        return None


def fetch_quote(symbol):
    """
    盡量取「當下可見價」與昨收，計算漲跌%。
    盤前/盤後有資料時會比較接近即時；失敗則退回日K。
    """
    last = None
    prev = None
    source = ""

    try:
        t = yf.Ticker(symbol)

        # 1) 分時（含 prepost）
        try:
            h1 = t.history(period="1d", interval="1m", prepost=True)
            if h1 is not None and len(h1) > 0:
                last = _safe_float(h1["Close"].iloc[-1])
                source = "1m+prepost"
        except Exception:
            pass

        # 2) fast_info
        try:
            fi = t.fast_info
            if last is None:
                last = _safe_float(getattr(fi, "last_price", None) or fi.get("last_price"))
                if last is not None:
                    source = source or "fast_info"
            if prev is None:
                prev = _safe_float(
                    getattr(fi, "previous_close", None) or fi.get("previous_close")
                )
        except Exception:
            pass

        # 3) 日K備援
        h = t.history(period="10d", interval="1d")
        if h is not None and len(h) >= 1:
            if last is None:
                last = _safe_float(h["Close"].iloc[-1])
                source = source or "daily"
            if prev is None:
                if len(h) >= 2:
                    # 若 last 接近最新收盤，昨收用前一日
                    prev = _safe_float(h["Close"].iloc[-2])
                else:
                    prev = _safe_float(h["Close"].iloc[-1])

        if last is None:
            return None

        if prev is None or prev == 0:
            chg = None
            chg_pct = None
        else:
            chg = last - prev
            chg_pct = (last - prev) / prev * 100

        return {
            "symbol": symbol,
            "last": last,
            "prev": prev,
            "chg": chg,
            "chg_pct": chg_pct,
            "source": source or "unknown",
        }
    except Exception as e:
        print(f"quote error {symbol}: {e}")
        return None


def session_label(now_tw):
    """粗分時段（台灣時間），僅供顯示。"""
    hhmm = now_tw.hour * 100 + now_tw.minute
    # 夏令大致：盤前 16:00-21:30；常規 21:30-04:00；盤後 04:00-08:00
    if 1600 <= hhmm < 2130:
        return "可能為美股盤前時段"
    if hhmm >= 2130 or hhmm < 400:
        return "可能為美股常規交易時段"
    if 400 <= hhmm < 900:
        return "可能為美股盤後／日K已出爐時段"
    return "非典型美股交易關注時段"


def market_bias(qqq_pct, nq_pct):
    vals = [v for v in [qqq_pct, nq_pct] if v is not None]
    if not vals:
        return "資料不足，無法判斷大盤方向", "neutral"
    avg = float(np.mean(vals))
    if avg >= 0.35:
        return f"偏多（基準平均約 {avg:+.2f}%）", "up"
    if avg <= -0.35:
        return f"偏空（基準平均約 {avg:+.2f}%）", "down"
    return f"中性／震盪（基準平均約 {avg:+.2f}%）", "neutral"


def build_html(bench_rows, stock_rows, now_str, sess, bias_text, bias_cls, qqq_pct):
    def fmt_pct(p):
        if p is None:
            return "—"
        return f"{p:+.2f}%"

    def fmt_px(p):
        if p is None:
            return "—"
        return f"{p:.2f}"

    def row_cls(p):
        if p is None:
            return ""
        if p >= 0.5:
            return "up"
        if p <= -0.5:
            return "down"
        return ""

    bench_html = []
    for r in bench_rows:
        if not r:
            continue
        bench_html.append(
            f"""<tr class="{row_cls(r['chg_pct'])}">
            <td><b>{html.escape(r['symbol'])}</b></td>
            <td>{html.escape(r.get('name', ''))}</td>
            <td class="num">{fmt_px(r['last'])}</td>
            <td class="num">{fmt_px(r['prev'])}</td>
            <td class="num">{fmt_pct(r['chg_pct'])}</td>
            </tr>"""
        )

    # 依漲跌排序
    ranked = [r for r in stock_rows if r and r.get("chg_pct") is not None]
    ranked.sort(key=lambda x: x["chg_pct"], reverse=True)

    stock_html = []
    for r in ranked:
        rs = None
        if r["chg_pct"] is not None and qqq_pct is not None:
            rs = r["chg_pct"] - qqq_pct
        rs_txt = fmt_pct(rs) if rs is not None else "—"
        note = ""
        if rs is not None:
            if rs >= 0.8:
                note = "相對QQQ偏強"
            elif rs <= -0.8:
                note = "相對QQQ偏弱"
        stock_html.append(
            f"""<tr class="{row_cls(r['chg_pct'])}">
            <td><b>{html.escape(r['symbol'])}</b></td>
            <td class="num">{fmt_px(r['last'])}</td>
            <td class="num">{fmt_pct(r['chg_pct'])}</td>
            <td class="num">{rs_txt}</td>
            <td>{html.escape(note)}</td>
            </tr>"""
        )

    strong = [r["symbol"] for r in ranked[:5] if r["chg_pct"] is not None and r["chg_pct"] > 0]
    weak = [r["symbol"] for r in ranked[-5:] if r["chg_pct"] is not None and r["chg_pct"] < 0]
    weak = list(reversed(weak))

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1"/>
<title>即時／盤前快照</title>
<style>
html, body {{ -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  margin: 12px; background: #0f1115; color: #e8eaed;
}}
h1 {{ font-size: 1.25rem; margin: 0 0 4px; }}
.badge {{
  display: inline-block; background: #293040; color: #9ecbff;
  font-size: 0.75rem; padding: 2px 8px; border-radius: 6px; margin-left: 6px;
}}
.meta {{ color: #9aa0a6; font-size: 0.85rem; margin-bottom: 12px; }}
h2 {{
  font-size: 1.05rem; margin: 20px 0 8px;
  border-bottom: 1px solid #333; padding-bottom: 4px;
}}
.bias {{
  padding: 10px 12px; border-radius: 8px; margin: 8px 0 12px; font-size: 0.95rem;
}}
.bias.up {{ background: #0d3d1a; border-left: 4px solid #3dd68c; }}
.bias.down {{ background: #4a1515; border-left: 4px solid #ff6b6b; }}
.bias.neutral {{ background: #1e222a; border-left: 4px solid #8b949e; }}
.summary span {{
  display: inline-block; background: #1e222a; padding: 4px 8px;
  border-radius: 6px; margin: 2px 4px 2px 0; font-size: 0.85rem;
}}
.wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
table {{ border-collapse: collapse; width: 100%; min-width: 520px; font-size: 13px; }}
th, td {{ border-bottom: 1px solid #2a2f3a; padding: 8px 6px; text-align: left; }}
th {{
  color: #9aa0a6; font-weight: 600; position: sticky; top: 0;
  background: #0f1115; white-space: nowrap;
}}
td.num {{ font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }}
tr.up td {{ background: #0d3d1a !important; box-shadow: inset 4px 0 0 #3dd68c; }}
tr.down td {{ background: #4a1515 !important; box-shadow: inset 4px 0 0 #ff6b6b; }}
.note {{
  background: #1a2332; border-left: 3px solid #58a6ff;
  padding: 8px 10px; margin: 12px 0; font-size: 0.8rem; color: #b0c4de;
}}
.footer {{ margin-top: 20px; color: #6b7280; font-size: 12px; }}
</style>
</head>
<body>
<h1>即時／盤前快照 <span class="badge">手動 Run</span></h1>
<div class="meta">產生時間：{html.escape(now_str)}（台灣時間） · {html.escape(sess)}</div>

<div class="note">
此頁是你按下 GitHub Actions 當下的行情快照（yfinance）。
非券商級即時，盤前欄位有時會缺；適合看大盤氣氛與核心股相對強弱。
</div>

<h2>大盤方向（簡判）</h2>
<div class="bias {bias_cls}">{html.escape(bias_text)}</div>

<h2>強弱摘要</h2>
<div class="summary">
  <span>偏強前段：{", ".join(strong) if strong else "無"}</span>
  <span>偏弱前段：{", ".join(weak) if weak else "無"}</span>
</div>

<h2>基準（大盤／期貨）</h2>
<div class="wrap">
<table>
<thead><tr>
<th>代號</th><th>名稱</th><th>現價</th><th>昨收</th><th>漲跌%</th>
</tr></thead>
<tbody>
{''.join(bench_html) if bench_html else '<tr><td colspan="5">無資料</td></tr>'}
</tbody>
</table>
</div>

<h2>核心股（相對 QQQ）</h2>
<div class="wrap">
<table>
<thead><tr>
<th>代號</th><th>現價</th><th>漲跌%</th><th>vs QQQ</th><th>備注</th>
</tr></thead>
<tbody>
{''.join(stock_html) if stock_html else '<tr><td colspan="5">無資料</td></tr>'}
</tbody>
</table>
</div>

<div class="footer">
資料來源 yfinance · 僅供參考，非投資建議 · report_live.html
</div>
</body>
</html>
"""


def main():
    now = datetime.now(pytz.timezone("Asia/Taipei"))
    now_str = now.strftime("%Y-%m-%d %H:%M")
    sess = session_label(now)

    bench_rows = []
    qqq_pct = None
    nq_pct = None

    for sym, name in BENCHMARKS:
        print(f"bench {sym} ...")
        q = fetch_quote(sym)
        if q:
            q["name"] = name
            bench_rows.append(q)
            if sym == "QQQ":
                qqq_pct = q.get("chg_pct")
            if sym == "NQ=F":
                nq_pct = q.get("chg_pct")
        else:
            print(f"  skip {sym}")

    stock_rows = []
    for sym in CORE_LIST:
        print(f"stock {sym} ...")
        q = fetch_quote(sym)
        if q:
            stock_rows.append(q)
        else:
            print(f"  skip {sym}")

    bias_text, bias_cls = market_bias(qqq_pct, nq_pct)
    doc = build_html(bench_rows, stock_rows, now_str, sess, bias_text, bias_cls, qqq_pct)

    out_name = "report_live.html"
    with open(out_name, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"written {out_name}")


if __name__ == "__main__":
    main()
