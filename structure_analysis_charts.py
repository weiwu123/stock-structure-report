import yfinance as yf
import numpy as np
from datetime import datetime
import pytz
import html

CORE_LIST = [
    "AAPL", "AMD", "AMZN", "ANET", "AVGO", "CSCO", "DELL", "GOOGL",
    "IBM", "INTC", "MRVL", "MSFT", "MU", "NET", "NOW", "NVDA",
    "ORCL", "PLTR", "QCOM", "SNOW", "TSLA", "TSM", "QQQM", "HPE",
]

SHORT_DAYS = 5
CTX_DAYS = 20
BB_PERIOD = 20
BB_STD = 2.0
NEAR_PCT = 3.0
FORWARD_LIST = [1, 3, 5]


def get_history(ticker, days=320):
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=f"{days}d")
        if hist is None or len(hist) < CTX_DAYS + 15:
            return None
        return hist
    except Exception as e:
        print(f"Error {ticker}: {e}")
        return None


def linear_slope(series):
    y = series.values.astype(float)
    if len(y) < 5:
        return 0.0
    x = np.arange(len(y))
    slope = np.polyfit(x, y, 1)[0]
    return (slope / np.mean(y)) * 100


def channel_label(slope):
    if slope >= 0.12:
        return "上升"
    if slope <= -0.12:
        return "下降"
    return "橫盤"


def bollinger(close, period=BB_PERIOD, num_std=BB_STD):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    return (
        float((mid + num_std * std).iloc[-1]),
        float(mid.iloc[-1]),
        float((mid - num_std * std).iloc[-1]),
    )


def bb_position(price, upper, lower):
    if upper <= lower:
        return "中軌附近", 50.0
    pct = (price - lower) / (upper - lower) * 100
    if price >= upper * 0.998:
        return "觸及上軌", pct
    if price <= lower * 1.002:
        return "觸及下軌", pct
    if pct >= 70:
        return "偏上軌", pct
    if pct <= 30:
        return "偏下軌", pct
    return "中軌附近", pct


def find_swings(high, low, left=2, right=2):
    h = high.values.astype(float)
    l = low.values.astype(float)
    n = len(h)
    sh, sl = [], []
    for i in range(left, n - right):
        if h[i] >= np.max(h[i - left : i + right + 1]) - 1e-9:
            sh.append((i, h[i]))
        if l[i] <= np.min(l[i - left : i + right + 1]) + 1e-9:
            sl.append((i, l[i]))
    return sh, sl


def nearest_levels(price, sh, sl, fb_high, fb_low):
    resists = sorted([p for _, p in sh if p > price * 1.001])
    supports = sorted([p for _, p in sl if p < price * 0.999], reverse=True)
    resist = resists[0] if resists else fb_high
    support = supports[0] if supports else fb_low
    if fb_high > price and fb_high < resist:
        resist = fb_high
    if fb_low < price and fb_low > support:
        support = fb_low
    return float(support), float(resist)


def hist_stats(hist, side):
    closes = hist["Close"].values.astype(float)
    highs = hist["High"].values.astype(float)
    lows = hist["Low"].values.astype(float)
    n = len(closes)
    max_fwd = max(FORWARD_LIST)
    if n < SHORT_DAYS + max_fwd + 50:
        return None

    rets_map = {d: [] for d in FORWARD_LIST}
    for i in range(SHORT_DAYS + 2, n - max_fwd):
        px = closes[i]
        if side == "support":
            lvl = np.min(lows[i - SHORT_DAYS : i])
            if lvl <= 0:
                continue
            dist = (px - lvl) / px * 100
            if not (0 <= dist <= NEAR_PCT):
                continue
        else:
            lvl = np.max(highs[i - SHORT_DAYS : i])
            if lvl <= 0:
                continue
            dist = (lvl - px) / px * 100
            if not (0 <= dist <= NEAR_PCT):
                continue
        for d in FORWARD_LIST:
            rets_map[d].append((closes[i + d] - px) / px * 100)

    out = {}
    for d in FORWARD_LIST:
        arr = rets_map[d]
        if len(arr) < 10:
            out[d] = None
        else:
            out[d] = {
                "samples": len(arr),
                "winrate": float(np.mean([1 if x > 0 else 0 for x in arr]) * 100),
                "avg": float(np.mean(arr)),
            }
    return out


def make_suggestion(r):
    sup = r["support"]
    res = r["resist"]
    event = r["event"]
    bb = r["bb_label"]
    px = r["price"]

    entry_lo = sup
    entry_hi = sup * 1.015
    stop = sup * 0.99
    mid = (sup + res) / 2

    if event == "跌破5日低":
        return (
            f"建議：偏弱，先觀望｜等站回 {sup:.2f}–{entry_hi:.2f} 再考慮｜未站回不追"
        )

    if event == "突破5日高":
        return (
            f"建議：偏強｜回測 {sup:.2f}–{px:.2f} 可考慮接｜"
            f"停損 <{stop:.2f}｜目標 {res:.2f}"
        )

    if event == "靠近支撐" or "下軌" in bb:
        return (
            f"建議：偏支撐區｜進 {entry_lo:.2f}–{entry_hi:.2f}｜"
            f"停損 <{stop:.2f}｜目標 {mid:.2f} / {res:.2f}"
        )

    if event == "靠近壓力" or "上軌" in bb:
        return (
            f"建議：接近壓力，慎追高｜減碼/出場參考 {res * 0.99:.2f}–{res:.2f}｜"
            f"未突破不追｜回落看 {mid:.2f}"
        )

    return (
        f"建議：區間中段，等邊緣｜偏多等 {entry_lo:.2f}–{entry_hi:.2f}｜"
        f"偏出看 {res * 0.99:.2f}–{res:.2f}"
    )


def analyze(ticker, hist):
    close = hist["Close"]
    high = hist["High"]
    low = hist["Low"]
    vol = hist["Volume"]
    price = float(close.iloc[-1])

    s_high = float(high.iloc[-SHORT_DAYS:].max())
    s_low = float(low.iloc[-SHORT_DAYS:].min())

    sh, sl = find_swings(high.iloc[-60:], low.iloc[-60:])
    support, resist = nearest_levels(price, sh, sl, s_high, s_low)
    dist_sup = (price - support) / price * 100
    dist_res = (resist - price) / price * 100
    near_sup = dist_sup <= NEAR_PCT
    near_res = dist_res <= NEAR_PCT

    bb_u, bb_m, bb_l = bollinger(close)
    bb_label, bb_pct = bb_position(price, bb_u, bb_l)

    slope = linear_slope(close.iloc[-12:])
    channel = channel_label(slope)

    if len(high) > SHORT_DAYS:
        prev_hi = float(high.iloc[-SHORT_DAYS - 1 : -1].max())
        prev_lo = float(low.iloc[-SHORT_DAYS - 1 : -1].min())
    else:
        prev_hi, prev_lo = s_high, s_low

    breakout = price > prev_hi * 1.002
    breakdown = price < prev_lo * 0.998

    if breakout:
        event = "突破5日高"
    elif breakdown:
        event = "跌破5日低"
    elif near_res:
        event = "靠近壓力"
    elif near_sup:
        event = "靠近支撐"
    else:
        event = "中段"

    vol_base = float(vol.iloc[-10:].mean())
    vol_ratio = float(vol.iloc[-3:].mean()) / vol_base if vol_base > 0 else 1.0
    if vol_ratio >= 1.4:
        vol_desc = f"放量{vol_ratio:.1f}x"
    elif vol_ratio <= 0.7:
        vol_desc = f"縮量{vol_ratio:.1f}x"
    else:
        vol_desc = f"普通{vol_ratio:.1f}x"

    if near_sup and not near_res:
        stats, side = hist_stats(hist, "support"), "近支撐"
    elif near_res and not near_sup:
        stats, side = hist_stats(hist, "resist"), "近壓力"
    else:
        stats, side = None, None

    r = {
        "ticker": ticker,
        "price": price,
        "support": support,
        "resist": resist,
        "dist_sup": dist_sup,
        "dist_res": dist_res,
        "channel": channel,
        "event": event,
        "vol_desc": vol_desc,
        "bb_u": bb_u,
        "bb_m": bb_m,
        "bb_l": bb_l,
        "bb_label": bb_label,
        "bb_pct": bb_pct,
        "breakout": breakout,
        "breakdown": breakdown,
        "near_sup": near_sup,
        "near_res": near_res,
        "stats": stats,
        "side": side,
    }
    r["suggestion"] = make_suggestion(r)
    return r


def event_class(event):
    if "突破" in event:
        return "up"
    if "跌破" in event:
        return "down"
    if "壓力" in event:
        return "resist"
    if "支撐" in event:
        return "support"
    return ""


def build_html(results, now_str):
    br = [r["ticker"] for r in results if r["breakout"]]
    bd = [r["ticker"] for r in results if r["breakdown"]]
    up = [r["ticker"] for r in results if "上軌" in r["bb_label"]]
    dn = [r["ticker"] for r in results if "下軌" in r["bb_label"]]
    focus = [
        r
        for r in results
        if r["near_sup"] or r["near_res"] or r["breakout"] or r["breakdown"]
    ]

    rows = []
    for r in results:
        cls = event_class(r["event"])
        rows.append(
            f"""<tr class="{cls}">
            <td><b>{html.escape(r['ticker'])}</b></td>
            <td class="num">{r['price']:.2f}</td>
            <td class="num">{r['support']:.2f}</td>
            <td class="num">{r['dist_sup']:+.1f}%</td>
            <td class="num">{r['resist']:.2f}</td>
            <td class="num">{r['dist_res']:+.1f}%</td>
            <td>{html.escape(r['channel'])}</td>
            <td>{html.escape(r['event'])}</td>
            <td>{html.escape(r['bb_label'])}</td>
            <td>{html.escape(r['vol_desc'])}</td>
            </tr>
            <tr class="suggest"><td colspan="10">↳ {html.escape(r.get('suggestion', ''))}</td></tr>"""
        )

    focus_rows = []
    for r in focus:
        stat = ""
        if r["stats"] and r["side"]:
            parts = []
            for d in FORWARD_LIST:
                s = r["stats"].get(d)
                if s:
                    parts.append(f"{d}d:{s['winrate']:.0f}%/{s['avg']:+.1f}%")
            if parts:
                stat = f"{r['side']} → " + " · ".join(parts)
        focus_rows.append(
            f"""<tr class="{event_class(r['event'])}">
            <td><b>{html.escape(r['ticker'])}</b></td>
            <td class="num">{r['price']:.2f}</td>
            <td>{html.escape(r['event'])}</td>
            <td class="num">{r['support']:.2f}</td>
            <td class="num">{r['resist']:.2f}</td>
            <td>{html.escape(r['bb_label'])}</td>
            <td class="stat">{html.escape(stat)}</td>
            </tr>
            <tr class="suggest"><td colspan="7">↳ {html.escape(r.get('suggestion', ''))}</td></tr>"""
        )

    focus_body = (
        "".join(focus_rows) if focus_rows else '<tr><td colspan="7">無</td></tr>'
    )

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1"/>
<title>結構分析報告（圖表版）</title>
<style>
html {{
  -webkit-text-size-adjust: 100%;
  text-size-adjust: 100%;
}}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  margin: 12px; background: #0f1115; color: #e8eaed;
  -webkit-text-size-adjust: 100%;
  text-size-adjust: 100%;
}}
h1 {{ font-size: 1.25rem; margin: 0 0 4px; }}
.meta {{ color: #9aa0a6; font-size: 0.85rem; margin-bottom: 12px; }}
.badge {{
  display: inline-block; background: #293040; color: #9ecbff;
  font-size: 0.75rem; padding: 2px 8px; border-radius: 6px; margin-left: 6px;
}}
h2 {{
  font-size: 1.05rem; margin: 20px 0 8px;
  border-bottom: 1px solid #333; padding-bottom: 4px;
}}
.summary span {{
  display: inline-block; background: #1e222a; padding: 4px 8px;
  border-radius: 6px; margin: 2px 4px 2px 0; font-size: 0.85rem;
}}
.legend {{ font-size: 0.8rem; color: #9aa0a6; margin: 8px 0 12px; }}
.legend i {{
  display: inline-block; width: 10px; height: 10px;
  border-radius: 2px; margin: 0 4px 0 10px; vertical-align: middle;
}}
.wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
table {{
  border-collapse: collapse; width: 100%;
  min-width: 720px; font-size: 13px;
  -webkit-text-size-adjust: 100%;
}}
th, td {{
  border-bottom: 1px solid #2a2f3a; padding: 8px 6px; text-align: left;
}}
th {{
  color: #9aa0a6; font-weight: 600; position: sticky; top: 0;
  background: #0f1115; white-space: nowrap; font-size: 13px;
}}
td.num {{
  font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap;
}}
td.stat {{ white-space: normal; min-width: 140px; color: #bdc1c6; }}
tr.up td {{
  background: #0d3d1a !important;
  box-shadow: inset 4px 0 0 #3dd68c;
}}
tr.down td {{
  background: #4a1515 !important;
  box-shadow: inset 4px 0 0 #ff6b6b;
}}
tr.resist td {{
  background: #3d3010 !important;
  box-shadow: inset 4px 0 0 #f0c14b;
}}
tr.support td {{
  background: #0d2a4a !important;
  box-shadow: inset 4px 0 0 #58a6ff;
}}
tr.suggest td {{
  background: #161b22 !important;
  color: #9ecbff;
  font-size: 11px !important;
  -webkit-text-size-adjust: 100%;
  text-size-adjust: 100%;
  padding-top: 2px;
  padding-bottom: 6px;
  border-bottom: 1px solid #3a3f4a;
  white-space: normal;
  line-height: 1.3;
  font-weight: 400;
}}
.footer {{ margin-top: 20px; color: #6b7280; font-size: 12px; }}
.note {{
  background: #1a2332; border-left: 3px solid #58a6ff;
  padding: 8px 10px; margin: 12px 0; font-size: 0.8rem; color: #b0c4de;
}}
</style>
</head>
<body>
<h1>關鍵位 + 布林結構分析 <span class="badge">圖表版</span></h1>
<div class="meta">產生時間：{html.escape(now_str)}（台灣時間） · 共 {len(results)} 檔</div>
<div class="note">此為第二版報告（report_charts.html）。目前邏輯與原版相同；之後可在此版加入 K 線圖，不影響 report.html。</div>

<h2>摘要</h2>
<div class="summary">
  <span>突破5日高：{", ".join(br) if br else "無"}</span>
  <span>跌破5日低：{", ".join(bd) if bd else "無"}</span>
  <span>布林上軌：{", ".join(up) if up else "無"}</span>
  <span>布林下軌：{", ".join(dn) if dn else "無"}</span>
</div>
<div class="legend">
  <i style="background:#3dd68c;margin-left:0"></i>突破
  <i style="background:#ff6b6b"></i>跌破
  <i style="background:#f0c14b"></i>靠近壓力
  <i style="background:#58a6ff"></i>靠近支撐
</div>

<h2>需關注</h2>
<div class="wrap">
<table>
<thead><tr>
<th>代號</th><th>現價</th><th>狀態</th><th>支撐</th><th>壓力</th><th>布林</th><th>歷史勝率</th>
</tr></thead>
<tbody>
{focus_body}
</tbody>
</table>
</div>

<h2>全部清單</h2>
<div class="wrap">
<table>
<thead><tr>
<th>代號</th><th>現價</th><th>支撐</th><th>距%</th><th>壓力</th><th>距%</th>
<th>通道</th><th>狀態</th><th>布林</th><th>量能</th>
</tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
</div>

<div class="footer">
資料來源 yfinance · 建議依支撐/壓力/布林自動產生，僅供參考，非投資建議 · 圖表版
</div>
</body>
</html>
"""


def main():
    now = datetime.now(pytz.timezone("Asia/Taipei"))
    now_str = now.strftime("%Y-%m-%d %H:%M")

    results = []
    for t in CORE_LIST:
        hist = get_history(t)
        if hist is None:
            print(f"{t}: skip")
            continue
        results.append(analyze(t, hist))
        print(f"ok {t}")

    doc = build_html(results, now_str)
    out_name = "report_charts.html"
    with open(out_name, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"written {out_name}")


if __name__ == "__main__":
    main()
