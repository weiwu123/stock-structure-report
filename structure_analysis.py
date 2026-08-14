import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
import os

# ====================== 設定 ======================
CORE_LIST = [
    "AMD", "GOOG", "MRVL", "MXL", "HPE", "FIG",
    "AAPL", "AMZN", "ANET", "AVGO", "CSCO", "DELL",
    "IBM", "INTC", "MSFT", "MU", "NET", "NOW", "NVDA",
    "ORCL", "PLTR", "QCOM", "SNOW", "TSLA", "TSM", "QQQM"
]

SHORT_DAYS = 5
CTX_DAYS = 20
BB_PERIOD = 20
BB_STD = 2.0
SWING_LEFT = 2
SWING_RIGHT = 2
NEAR_PCT = 3.0
FORWARD_LIST = [1, 3, 5]
# ==================================================


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
    n = len(y)
    if n < 5:
        return 0.0
    x = np.arange(n)
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
    upper = mid + num_std * std
    lower = mid - num_std * std
    return float(upper.iloc[-1]), float(mid.iloc[-1]), float(lower.iloc[-1])


def bb_position(price, upper, mid, lower):
    if upper <= lower:
        return "中軌附近", 50.0
    pct = (price - lower) / (upper - lower) * 100
    if price >= upper * 0.998:
        label = "觸及上軌"
    elif price <= lower * 1.002:
        label = "觸及下軌"
    elif pct >= 70:
        label = "偏上軌"
    elif pct <= 30:
        label = "偏下軌"
    else:
        label = "中軌附近"
    return label, pct


def find_swings(high, low, left=SWING_LEFT, right=SWING_RIGHT):
    h = high.values.astype(float)
    l = low.values.astype(float)
    n = len(h)
    sh, sl = [], []
    for i in range(left, n - right):
        if h[i] >= np.max(h[i - left:i + right + 1]) - 1e-9:
            sh.append((i, h[i]))
        if l[i] <= np.min(l[i - left:i + right + 1]) + 1e-9:
            sl.append((i, l[i]))
    return sh, sl


def nearest_levels(price, swing_highs, swing_lows, fallback_high, fallback_low):
    resists = sorted([p for _, p in swing_highs if p > price * 1.001])
    supports = sorted([p for _, p in swing_lows if p < price * 0.999], reverse=True)
    resist = resists[0] if resists else fallback_high
    support = supports[0] if supports else fallback_low
    if fallback_high > price and fallback_high < resist:
        resist = fallback_high
    if fallback_low < price and fallback_low > support:
        support = fallback_low
    return float(support), float(resist)


def hist_near_level_stats(hist, side):
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
            lvl = np.min(lows[i - SHORT_DAYS:i])
            if lvl <= 0:
                continue
            dist = (px - lvl) / px * 100
            if not (0 <= dist <= NEAR_PCT):
                continue
        else:
            lvl = np.max(highs[i - SHORT_DAYS:i])
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
            wins = [1 if x > 0 else 0 for x in arr]
            out[d] = {
                "samples": len(arr),
                "winrate": float(np.mean(wins) * 100),
                "avg": float(np.mean(arr))
            }
    return out


def analyze(ticker, hist):
    close = hist["Close"]
    high = hist["High"]
    low = hist["Low"]
    vol = hist["Volume"]
    price = float(close.iloc[-1])

    s_high = float(high.iloc[-SHORT_DAYS:].max())
    s_low = float(low.iloc[-SHORT_DAYS:].min())
    c_high = float(high.iloc[-CTX_DAYS:].max())
    c_low = float(low.iloc[-CTX_DAYS:].min())

    sh, sl = find_swings(high.iloc[-60:], low.iloc[-60:])
    support, resist = nearest_levels(price, sh, sl, s_high, s_low)
    dist_sup = (price - support) / price * 100
    dist_res = (resist - price) / price * 100
    near_sup = dist_sup <= NEAR_PCT
    near_res = dist_res <= NEAR_PCT

    # 布林
    bb_u, bb_m, bb_l = bollinger(close)
    bb_label, bb_pct = bb_position(price, bb_u, bb_m, bb_l)

    slope = linear_slope(close.iloc[-12:])
    channel = channel_label(slope)

    if len(high) > SHORT_DAYS:
        prev_s_high = float(high.iloc[-SHORT_DAYS - 1:-1].max())
        prev_s_low = float(low.iloc[-SHORT_DAYS - 1:-1].min())
    else:
        prev_s_high, prev_s_low = s_high, s_low

    breakout = price > prev_s_high * 1.002
    breakdown = price < prev_s_low * 0.998

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

    vol_recent = float(vol.iloc[-3:].mean())
    vol_base = float(vol.iloc[-10:].mean())
    vol_ratio = vol_recent / vol_base if vol_base > 0 else 1.0
    if vol_ratio >= 1.4:
        vol_desc = f"放量{vol_ratio:.1f}x"
    elif vol_ratio <= 0.7:
        vol_desc = f"縮量{vol_ratio:.1f}x"
    else:
        vol_desc = f"普通{vol_ratio:.1f}x"

    if near_sup and not near_res:
        stats = hist_near_level_stats(hist, "support")
        stats_side = "近支撐"
    elif near_res and not near_sup:
        stats = hist_near_level_stats(hist, "resist")
        stats_side = "近壓力"
    else:
        stats, stats_side = None, None

    return {
        "ticker": ticker,
        "price": price,
        "support": support,
        "resist": resist,
        "dist_sup": dist_sup,
        "dist_res": dist_res,
        "s_low": s_low,
        "s_high": s_high,
        "bb_u": bb_u,
        "bb_m": bb_m,
        "bb_l": bb_l,
        "bb_label": bb_label,
        "bb_pct": bb_pct,
        "channel": channel,
        "slope": slope,
        "event": event,
        "vol_desc": vol_desc,
        "breakout": breakout,
        "breakdown": breakdown,
        "near_sup": near_sup,
        "near_res": near_res,
        "stats": stats,
        "stats_side": stats_side
    }


def main():
    lines = []
    def log(s=""):
        print(s)
        lines.append(str(s))

    now = datetime.now(pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
    log("=" * 100)
    log(f"【關鍵位 + 布林】 {now}")
    log(f"BB({BB_PERIOD},{BB_STD}) | 近{SHORT_DAYS}日短線 | 靠近≤{NEAR_PCT}% | 勝率1/3/5日")
    log("=" * 100)

    results = []
    for ticker in CORE_LIST:
        hist = get_history(ticker)
        if hist is None:
            continue
        results.append(analyze(ticker, hist))

    # ----- 主表 -----
    log("")
    hdr = f"{'代號':6} {'現價':>8} {'支撐':>8} {'距%':>6} {'壓力':>8} {'距%':>6} {'通道':4} {'狀態':8} {'布林':8} {'量能':10}"
    log(hdr)
    log("-" * 100)

    for r in results:
        log(
            f"{r['ticker']:6} "
            f"{r['price']:8.2f} "
            f"{r['support']:8.2f} "
            f"{r['dist_sup']:+5.1f}% "
            f"{r['resist']:8.2f} "
            f"{r['dist_res']:+5.1f}% "
            f"{r['channel']:4} "
            f"{r['event']:8} "
            f"{r['bb_label']:8} "
            f"{r['vol_desc']:10}"
        )

    # ----- 布林明細表 -----
    log("\n" + "=" * 100)
    log("【布林通道明細】")
    log("-" * 100)
    log(f"{'代號':6} {'現價':>8} {'下軌':>8} {'中軌':>8} {'上軌':>8} {'位置%':>6} {'說明':10}")
    log("-" * 100)
    for r in results:
        log(
            f"{r['ticker']:6} "
            f"{r['price']:8.2f} "
            f"{r['bb_l']:8.2f} "
            f"{r['bb_m']:8.2f} "
            f"{r['bb_u']:8.2f} "
            f"{r['bb_pct']:5.0f}% "
            f"{r['bb_label']:10}"
        )

    # ----- 靠近關鍵位 + 勝率 -----
    focus = [r for r in results if r["near_sup"] or r["near_res"] or r["breakout"] or r["breakdown"]]
    log("\n" + "=" * 100)
    log("【需關注（靠近支撐/壓力 或 突破/跌破）】")
    log("-" * 100)
    if not focus:
        log("無")
    else:
        log(f"{'代號':6} {'現價':>8} {'事件':10} {'支撐':>8} {'壓力':>8} {'布林':8} {'歷史勝率(近關鍵位)'}")
        log("-" * 100)
        for r in focus:
            if r["stats"] and r["stats_side"]:
                parts = []
                for d in FORWARD_LIST:
                    s = r["stats"].get(d)
                    if s:
                        parts.append(f"{d}d:{s['winrate']:.0f}%/{s['avg']:+.1f}%")
                stat_txt = f"{r['stats_side']}→ " + " ".join(parts) if parts else "樣本不足"
            else:
                stat_txt = "-"
            log(
                f"{r['ticker']:6} "
                f"{r['price']:8.2f} "
                f"{r['event']:10} "
                f"{r['support']:8.2f} "
                f"{r['resist']:8.2f} "
                f"{r['bb_label']:8} "
                f"{stat_txt}"
            )

    # ----- 摘要 -----
    breakouts = [r for r in results if r["breakout"]]
    breakdowns = [r for r in results if r["breakdown"]]
    bb_upper = [r for r in results if "上軌" in r["bb_label"]]
    bb_lower = [r for r in results if "下軌" in r["bb_label"]]
    up_ch = [r["ticker"] for r in results if r["channel"] == "上升"]
    down_ch = [r["ticker"] for r in results if r["channel"] == "下降"]

    log("\n" + "=" * 100)
    log("【摘要】")
    log("-" * 100)
    log("突破5日高: " + (", ".join(f"{r['ticker']}({r['price']:.2f})" for r in breakouts) if breakouts else "無"))
    log("跌破5日低: " + (", ".join(f"{r['ticker']}({r['price']:.2f})" for r in breakdowns) if breakdowns else "無"))
    log("觸及/偏布林上軌: " + (", ".join(r["ticker"] for r in bb_upper) if bb_upper else "無"))
    log("觸及/偏布林下軌: " + (", ".join(r["ticker"] for r in bb_lower) if bb_lower else "無"))
    log("上升通道: " + (", ".join(up_ch) if up_ch else "無"))
    log("下降通道: " + (", ".join(down_ch) if down_ch else "無"))
    log("=" * 100)

    fname = f"關鍵位布林_{datetime.now(pytz.timezone('Asia/Taipei')).strftime('%Y%m%d_%H%M')}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n已存檔: {fname}")
    print(f"路徑: {os.path.abspath(fname)}")

	out_name = "latest_report.txt"
	with open(out_name, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

if __name__ == "__main__":
    main()