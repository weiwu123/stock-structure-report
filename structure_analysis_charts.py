import io
import base64
import html
from datetime import datetime

import numpy as np
import pytz
import yfinance as yf

import matplotlib

matplotlib.use("Agg")
import mplfinance as mpf

CORE_LIST = [
    "AAOI", "AAPL", "AMD", "AMZN", "ANET", "AVGO", "CRWV", "CSCO", "DELL",
    "FIG", "GOOG", "HPE", "IBM", "INTC", "MRVL", "MSFT", "MU", "MXL",
    "NBIS", "NET", "NOK", "NOW", "NVDA", "NVTS", "ORCL", "ONDS",
    "PLTR", "QCOM", "QQQM", "SNOW", "SIMO", "SPCX", "TSLA", "TSM",
]

SHORT_DAYS = 5
CTX_DAYS = 20
FIB_DAYS = 60
FIB_MIN_SPAN_PCT = 5.0  # 波段太小不畫 Fib
BB_PERIOD = 20
BB_STD = 2.0
NEAR_PCT = 3.0
NARROW_PCT = 3.0
FORWARD_LIST = [1, 3, 5]
CHART_DAYS = 80


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
    mean = float(np.mean(y))
    if mean == 0:
        return 0.0
    return (slope / mean) * 100


def channel_label(slope):
    if slope >= 0.12:
        return "上升"
    if slope <= -0.12:
        return "下降"
    return "橫盤"


def ma_slope_label(slope):
    if slope >= 0.08:
        return "上揚"
    if slope <= -0.08:
        return "下彎"
    return "走平"


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


def calc_fib_60(hist, days=FIB_DAYS):
    """
    近 days 日高低當波段，多頭回撤：
    0.5 / 0.618 從高點往下算。
    回傳 dict 或 None（波段太小）。
    """
    if hist is None or len(hist) < days:
        window = hist
    else:
        window = hist.tail(days)

    lo = float(window["Low"].min())
    hi = float(window["High"].max())
    if hi <= lo or lo <= 0:
        return None

    span_pct = (hi - lo) / lo * 100
    if span_pct < FIB_MIN_SPAN_PCT:
        return None

    # 回撤線：高 - 幅度 * ratio（0.618 價位較低）
    fib50 = hi - (hi - lo) * 0.5
    fib618 = hi - (hi - lo) * 0.618
    # 保證 low_side < high_side
    zone_lo = min(fib50, fib618)
    zone_hi = max(fib50, fib618)

    return {
        "swing_low": lo,
        "swing_high": hi,
        "fib50": float(fib50),
        "fib618": float(fib618),
        "zone_lo": float(zone_lo),
        "zone_hi": float(zone_hi),
        "span_pct": float(span_pct),
    }


def fib_position_label(price, fib):
    if fib is None:
        return "無"
    zlo, zhi = fib["zone_lo"], fib["zone_hi"]
    if price < zlo * 0.998:
        return "帶下（跌破0.618）"
    if price > zhi * 1.002:
        return "帶上（未回撤到0.5）"
    return "帶內（0.5–0.618）"


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


def make_ma_note(r):
    parts = []
    if r.get("ma20") is not None:
        pos20 = "上方" if r["price"] >= r["ma20"] else "下方"
        parts.append(f"MA20 {r['ma20']:.2f}（價格在{pos20}，{r['ma20_slope_lbl']}）")
    if r.get("ma200") is not None:
        pos200 = "上方" if r["price"] >= r["ma200"] else "下方"
        parts.append(f"MA200 {r['ma200']:.2f}（價格在{pos200}，{r['ma200_slope_lbl']}）")
    if not parts:
        return "均線資料不足"
    if r.get("ma200") is not None and r.get("ma20") is not None:
        if r["price"] >= r["ma200"] and r["price"] >= r["ma20"]:
            parts.append("濾網：偏多（站上MA20與MA200）")
        elif r["price"] < r["ma200"] and r["price"] < r["ma20"]:
            parts.append("濾網：偏空／慎追多（低於MA20與MA200）")
        elif r["price"] >= r["ma200"] and r["price"] < r["ma20"]:
            parts.append("濾網：大方向仍偏多，短線回踩MA20中")
        else:
            parts.append("濾網：短線強於MA20，但尚未站回MA200")
    return "｜".join(parts)


def make_fib_note(r):
    fib = r.get("fib")
    if not fib:
        return "Fib：近60日波段不足，不計"
    return (
        f"Fib0.5–0.618：{fib['zone_lo']:.2f}–{fib['zone_hi']:.2f}"
        f"（{r.get('fib_pos', '')}｜波段{fib['swing_low']:.2f}–{fib['swing_high']:.2f}）"
    )


def make_suggestion(r):
    sup = r["support"]
    res = r["resist"]
    event = r["event"]
    bb = r["bb_label"]
    px = r["price"]
    narrow = r.get("narrow", False)
    fib = r.get("fib")
    fib_pos = r.get("fib_pos", "")

    ma_tail = ""
    if r.get("ma200") is not None and px < r["ma200"]:
        ma_tail = "｜均線：在MA200下，追多需更嚴格"
    elif r.get("ma20") is not None and px < r["ma20"] and r.get("ma200") is not None and px >= r["ma200"]:
        ma_tail = "｜均線：回踩MA20，偏等多看支撐是否守住"
    elif r.get("ma20") is not None and px >= r["ma20"] and r.get("ma200") is not None and px >= r["ma200"]:
        ma_tail = "｜均線：站上MA20/200，結構偏多可參考"

    fib_tail = ""
    if fib:
        if "帶內" in fib_pos:
            fib_tail = f"｜Fib：位於回撤帶 {fib['zone_lo']:.2f}–{fib['zone_hi']:.2f}，可當承接參考"
        elif "帶下" in fib_pos:
            fib_tail = f"｜Fib：已低於0.618（{fib['fib618']:.2f}），回撤偏深，慎接"
        elif "帶上" in fib_pos:
            fib_tail = f"｜Fib：仍在0.5上方（{fib['fib50']:.2f}），回撤未到位"

    if narrow:
        return (
            f"建議：結構過窄（支撐壓力間距偏小）｜改看 20 日 "
            f"{sup:.2f}–{res:.2f}｜暫不硬做區間，等突破或跌破再定義"
            f"{ma_tail}{fib_tail}"
        )

    entry_lo = sup
    entry_hi = sup * 1.015
    stop = sup * 0.99
    mid = (sup + res) / 2

    # 若有 Fib 帶，進場區可與 Fib 交集提示
    if fib and "帶內" in fib_pos:
        entry_lo = max(entry_lo, fib["zone_lo"])
        entry_hi = min(max(entry_hi, fib["zone_lo"]), fib["zone_hi"])

    if event == "跌破5日低":
        return (
            f"建議：偏弱，先觀望｜等站回 {sup:.2f}–{entry_hi:.2f} 再考慮｜未站回不追"
            f"{ma_tail}{fib_tail}"
        )
    if event == "突破5日高":
        return (
            f"建議：偏強｜回測 {sup:.2f}–{px:.2f} 可考慮接｜"
            f"停損 <{stop:.2f}｜目標 {res:.2f}{ma_tail}{fib_tail}"
        )
    if event == "靠近支撐" or "下軌" in bb:
        return (
            f"建議：偏支撐區｜進 {entry_lo:.2f}–{entry_hi:.2f}｜"
            f"停損 <{stop:.2f}｜目標 {mid:.2f} / {res:.2f}{ma_tail}{fib_tail}"
        )
    if event == "靠近壓力" or "上軌" in bb:
        return (
            f"建議：接近壓力，慎追高｜減碼/出場參考 {res * 0.99:.2f}–{res:.2f}｜"
            f"未突破不追｜回落看 {mid:.2f}{ma_tail}{fib_tail}"
        )
    return (
        f"建議：區間中段，等邊緣｜偏多等 {entry_lo:.2f}–{entry_hi:.2f}｜"
        f"偏出看 {res * 0.99:.2f}–{res:.2f}{ma_tail}{fib_tail}"
    )


def make_chart_base64(hist, support, resist, ticker, fib=None):
    """K線 + 支撐壓力 + 布林 + MA200 + Fib 0.5–0.618 半透明帶"""
    try:
        close_full = hist["Close"]
        mid_full = close_full.rolling(BB_PERIOD).mean()
        std_full = close_full.rolling(BB_PERIOD).std()
        upper_full = mid_full + BB_STD * std_full
        lower_full = mid_full - BB_STD * std_full
        ma200_full = close_full.rolling(200).mean()

        df = hist.tail(CHART_DAYS).copy()
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        if len(df) < 10:
            return None

        df["bb_u"] = upper_full.reindex(df.index)
        df["bb_m"] = mid_full.reindex(df.index)
        df["bb_l"] = lower_full.reindex(df.index)
        df["ma200"] = ma200_full.reindex(df.index)

        buf = io.BytesIO()
        mc = mpf.make_marketcolors(
            up="#3dd68c", down="#ff6b6b", edge="inherit", wick="inherit", volume="in"
        )
        style = mpf.make_mpf_style(
            base_mpf_style="nightclouds",
            marketcolors=mc,
            facecolor="#0f1115",
            edgecolor="#0f1115",
            figcolor="#0f1115",
            gridcolor="#2a2f3a",
            gridstyle="--",
        )

        apds = []
        if df["bb_u"].notna().sum() > 5:
            apds.append(mpf.make_addplot(df["bb_u"], color="#c9d1d9", width=1.0, linestyle="-"))
            apds.append(mpf.make_addplot(df["bb_m"], color="#8b949e", width=1.0, linestyle="-"))
            apds.append(mpf.make_addplot(df["bb_l"], color="#c9d1d9", width=1.0, linestyle="-"))
        if df["ma200"].notna().sum() > 5:
            apds.append(mpf.make_addplot(df["ma200"], color="#ff7b72", width=1.2))

        lo = float(df["Low"].min())
        hi = float(df["High"].max())
        extra = []
        for col in ("ma200", "bb_u", "bb_l"):
            if col in df and df[col].notna().any():
                extra.append(float(df[col].min()))
                extra.append(float(df[col].max()))
        if fib:
            extra.extend([fib["zone_lo"], fib["zone_hi"], fib["swing_low"], fib["swing_high"]])
        if extra:
            lo = min(lo, min(extra))
            hi = max(hi, max(extra))
        pad = (hi - lo) * 0.03 if hi > lo else 1.0
        y_lo, y_hi = lo - pad, hi + pad

        levels, colors = [], []
        if y_lo <= support <= y_hi:
            levels.append(support)
            colors.append("#58a6ff")
        if y_lo <= resist <= y_hi:
            levels.append(resist)
            colors.append("#f0c14b")

        hlines = None
        if levels:
            hlines = dict(
                hlines=levels,
                colors=colors,
                linestyle="-.",
                linewidths=tuple([1.2] * len(levels)),
                alpha=0.9,
            )

        plot_kwargs = dict(
            type="candle",
            style=style,
            volume=True,
            ylabel="Price",
            ylabel_lower="Vol",
            figratio=(12, 7),
            figscale=1.05,
            tight_layout=True,
            update_width_config=dict(candle_linewidth=0.8),
            ylim=(y_lo, y_hi),
            scale_padding=dict(top=0.15, bottom=0.25, left=0.12, right=0.12),
            returnfig=True,
        )
        if apds:
            plot_kwargs["addplot"] = apds
        if hlines:
            plot_kwargs["hlines"] = hlines

        fig, axes = mpf.plot(df, **plot_kwargs)
        ax = axes[0] if isinstance(axes, (list, np.ndarray)) else axes

        # Fib 0.5–0.618 半透明帶 + 邊界線
        if fib is not None:
            zlo, zhi = fib["zone_lo"], fib["zone_hi"]
            ax.axhspan(zlo, zhi, facecolor="#a371f7", alpha=0.18, zorder=0)
            ax.axhline(fib["fib50"], color="#a371f7", linestyle="--", linewidth=1.0, alpha=0.85)
            ax.axhline(fib["fib618"], color="#a371f7", linestyle="--", linewidth=1.0, alpha=0.85)

        fig.savefig(
            buf,
            dpi=110,
            bbox_inches="tight",
            facecolor="#0f1115",
            pad_inches=0.2,
        )
        import matplotlib.pyplot as plt

        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        print(f"chart error {ticker}: {e}")
        return None


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

    span_pct = (resist - support) / price * 100 if price > 0 else 0.0
    narrow = span_pct < NARROW_PCT
    if narrow:
        support, resist = c_low, c_high
        if support >= resist:
            support, resist = s_low, s_high

    dist_sup = (price - support) / price * 100
    dist_res = (resist - price) / price * 100
    near_sup = dist_sup <= NEAR_PCT
    near_res = dist_res <= NEAR_PCT

    bb_u, bb_m, bb_l = bollinger(close)
    bb_label, bb_pct = bb_position(price, bb_u, bb_l)

    ma20_s = close.rolling(20).mean()
    ma200_s = close.rolling(200).mean()
    ma20 = float(ma20_s.iloc[-1]) if ma20_s.notna().iloc[-1] else None
    ma200 = float(ma200_s.iloc[-1]) if ma200_s.notna().iloc[-1] else None
    ma20_slope = linear_slope(ma20_s.dropna().iloc[-10:]) if ma20_s.dropna().shape[0] >= 10 else 0.0
    ma200_slope = (
        linear_slope(ma200_s.dropna().iloc[-20:]) if ma200_s.dropna().shape[0] >= 20 else 0.0
    )

    slope = linear_slope(close.iloc[-12:])
    channel = channel_label(slope)

    fib = calc_fib_60(hist, FIB_DAYS)
    fib_pos = fib_position_label(price, fib)

    if len(high) > SHORT_DAYS:
        prev_hi = float(high.iloc[-SHORT_DAYS - 1 : -1].max())
        prev_lo = float(low.iloc[-SHORT_DAYS - 1 : -1].min())
    else:
        prev_hi, prev_lo = s_high, s_low

    breakout = price > prev_hi * 1.002
    breakdown = price < prev_lo * 0.998

    if narrow and not breakout and not breakdown:
        event = "結構過窄"
    elif breakout:
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

    if not narrow and near_sup and not near_res:
        stats, side = hist_stats(hist, "support"), "近支撐"
    elif not narrow and near_res and not near_sup:
        stats, side = hist_stats(hist, "resist"), "近壓力"
    else:
        stats, side = None, None

    in_fib = fib is not None and "帶內" in fib_pos

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
        "bb_label": bb_label,
        "breakout": breakout,
        "breakdown": breakdown,
        "near_sup": near_sup,
        "near_res": near_res,
        "narrow": narrow,
        "ma20": ma20,
        "ma200": ma200,
        "ma20_slope_lbl": ma_slope_label(ma20_slope),
        "ma200_slope_lbl": ma_slope_label(ma200_slope),
        "fib": fib,
        "fib_pos": fib_pos,
        "in_fib": in_fib,
        "stats": stats,
        "side": side,
        "hist": hist,
    }
    r["ma_note"] = make_ma_note(r)
    r["fib_note"] = make_fib_note(r)
    r["suggestion"] = make_suggestion(r)
    r["focus"] = near_sup or near_res or breakout or breakdown or narrow or in_fib
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
    if "過窄" in event:
        return "narrow"
    return ""


def build_html(results, now_str):
    br = [r["ticker"] for r in results if r["breakout"]]
    bd = [r["ticker"] for r in results if r["breakdown"]]
    up = [r["ticker"] for r in results if "上軌" in r["bb_label"]]
    dn = [r["ticker"] for r in results if "下軌" in r["bb_label"]]
    narrow_list = [r["ticker"] for r in results if r.get("narrow")]
    fib_in = [r["ticker"] for r in results if r.get("in_fib")]
    focus = [r for r in results if r.get("focus")]
    above_both = [
        r["ticker"]
        for r in results
        if r.get("ma20") is not None
        and r.get("ma200") is not None
        and r["price"] >= r["ma20"]
        and r["price"] >= r["ma200"]
    ]
    below_both = [
        r["ticker"]
        for r in results
        if r.get("ma20") is not None
        and r.get("ma200") is not None
        and r["price"] < r["ma20"]
        and r["price"] < r["ma200"]
    ]

    for r in results:
        print(f"chart {r['ticker']} ...")
        r["chart"] = make_chart_base64(
            r["hist"], r["support"], r["resist"], r["ticker"], fib=r.get("fib")
        )

    def card_html(r, badge=None):
        stat = ""
        if r["stats"] and r["side"]:
            parts = []
            for d in FORWARD_LIST:
                s = r["stats"].get(d)
                if s:
                    parts.append(f"{d}d:{s['winrate']:.0f}%/{s['avg']:+.1f}%")
            if parts:
                stat = f"{r['side']} → " + " · ".join(parts)

        if r.get("chart"):
            chart_html = (
                f'<div class="chart">'
                f'<img src="data:image/png;base64,{r["chart"]}" '
                f'alt="{html.escape(r["ticker"])} chart"/></div>'
                f'<div class="chart-legend">'
                f'<span class="lg-sup">┅ 支撐</span>'
                f'<span class="lg-res">┅ 壓力</span>'
                f'<span class="lg-bb">━ 布林</span>'
                f'<span class="lg-ma200">━ MA200</span>'
                f'<span class="lg-fib">▓▓ Fib0.5–0.618</span>'
                f"</div>"
            )
        else:
            chart_html = '<div class="chart miss">（圖表產生失敗）</div>'

        badge_html = f'<span class="badge-mini">{html.escape(badge)}</span>' if badge else ""

        return f"""
<div class="card">
  <div class="card-h">
    <b>{html.escape(r['ticker'])}</b>
    <span class="tag {event_class(r['event'])}">{html.escape(r['event'])}</span>
    {badge_html}
    <span class="px">{r['price']:.2f}</span>
  </div>
  <div class="card-m">
    支撐 {r['support']:.2f}（{r['dist_sup']:+.1f}%） ·
    壓力 {r['resist']:.2f}（{r['dist_res']:+.1f}%） ·
    {html.escape(r['bb_label'])} · {html.escape(r['channel'])} · {html.escape(r['vol_desc'])}
  </div>
  {chart_html}
  <div class="sug">↳ {html.escape(r.get('suggestion', ''))}</div>
  <div class="ma">↳ 均線：{html.escape(r.get('ma_note', ''))}</div>
  <div class="fib">↳ {html.escape(r.get('fib_note', ''))}</div>
  <div class="stat">{html.escape(stat) if stat else ''}</div>
</div>
"""

    focus_blocks = [card_html(r, "需關注") for r in focus] if focus else ["<p>無</p>"]
    other = [r for r in results if not r.get("focus")]
    other_blocks = [card_html(r, "一般") for r in other] if other else ["<p>無</p>"]

    rows = []
    for r in results:
        cls = event_class(r["event"])
        ma20_txt = f"{r['ma20']:.2f}" if r.get("ma20") is not None else "—"
        ma200_txt = f"{r['ma200']:.2f}" if r.get("ma200") is not None else "—"
        if r.get("fib"):
            fib_txt = f"{r['fib']['zone_lo']:.2f}–{r['fib']['zone_hi']:.2f}"
        else:
            fib_txt = "—"
        rows.append(
            f"""<tr class="{cls}">
            <td><b>{html.escape(r['ticker'])}</b></td>
            <td class="num">{r['price']:.2f}</td>
            <td class="num">{r['support']:.2f}</td>
            <td class="num">{r['resist']:.2f}</td>
            <td class="num">{fib_txt}</td>
            <td>{html.escape(r.get('fib_pos') or '—')}</td>
            <td>{html.escape(r['event'])}</td>
            <td>{html.escape(r['bb_label'])}</td>
            <td class="num">{ma20_txt}</td>
            <td class="num">{ma200_txt}</td>
            <td>{html.escape(r['vol_desc'])}</td>
            </tr>
            <tr class="suggest"><td colspan="11">↳ {html.escape(r.get('suggestion', ''))}<br/>
            ↳ 均線：{html.escape(r.get('ma_note', ''))}<br/>
            ↳ {html.escape(r.get('fib_note', ''))}</td></tr>"""
        )

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1"/>
<title>結構分析報告（圖表版）</title>
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
.badge-mini {{
  font-size: 0.7rem; padding: 1px 6px; border-radius: 4px;
  background: #30363d; color: #c9d1d9;
}}
.meta {{ color: #9aa0a6; font-size: 0.85rem; margin-bottom: 12px; }}
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
.card {{
  background: #161b22; border: 1px solid #2a2f3a; border-radius: 10px;
  padding: 10px; margin: 0 0 14px;
}}
.card-h {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 4px; }}
.card-h .px {{ margin-left: auto; font-variant-numeric: tabular-nums; }}
.tag {{
  font-size: 0.75rem; padding: 2px 8px; border-radius: 6px; background: #2a2f3a;
}}
.tag.up {{ background: #0d3d1a; color: #3dd68c; }}
.tag.down {{ background: #4a1515; color: #ff6b6b; }}
.tag.resist {{ background: #3d3010; color: #f0c14b; }}
.tag.support {{ background: #0d2a4a; color: #58a6ff; }}
.tag.narrow {{ background: #2a2a2a; color: #c9d1d9; }}
.card-m {{ color: #9aa0a6; font-size: 0.8rem; margin-bottom: 8px; }}
.chart img {{
  width: 100%; max-width: 900px; height: auto;
  border-radius: 8px; display: block; background: #0f1115;
}}
.chart.miss {{ color: #6b7280; font-size: 0.8rem; }}
.chart-legend {{
  display: flex; flex-wrap: wrap; gap: 8px 12px; margin: 6px 0 4px;
  font-size: 11px; color: #9aa0a6;
}}
.lg-sup {{ color: #58a6ff; font-weight: 600; }}
.lg-res {{ color: #f0c14b; font-weight: 600; }}
.lg-bb {{ color: #c9d1d9; }}
.lg-ma200 {{ color: #ff7b72; }}
.lg-fib {{ color: #a371f7; font-weight: 600; }}
.sug {{ color: #9ecbff; font-size: 11px; line-height: 1.35; margin-top: 6px; }}
.ma {{ color: #7ee787; font-size: 11px; line-height: 1.35; margin-top: 4px; }}
.fib {{ color: #d2a8ff; font-size: 11px; line-height: 1.35; margin-top: 4px; }}
.stat {{ color: #8b949e; font-size: 11px; margin-top: 4px; }}
.wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
table {{ border-collapse: collapse; width: 100%; min-width: 900px; font-size: 13px; }}
th, td {{ border-bottom: 1px solid #2a2f3a; padding: 8px 6px; text-align: left; }}
th {{
  color: #9aa0a6; font-weight: 600; position: sticky; top: 0;
  background: #0f1115; white-space: nowrap;
}}
td.num {{ font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }}
tr.up td {{ background: #0d3d1a !important; box-shadow: inset 4px 0 0 #3dd68c; }}
tr.down td {{ background: #4a1515 !important; box-shadow: inset 4px 0 0 #ff6b6b; }}
tr.resist td {{ background: #3d3010 !important; box-shadow: inset 4px 0 0 #f0c14b; }}
tr.support td {{ background: #0d2a4a !important; box-shadow: inset 4px 0 0 #58a6ff; }}
tr.narrow td {{ background: #222 !important; box-shadow: inset 4px 0 0 #8b949e; }}
tr.suggest td {{
  background: #161b22 !important; color: #9ecbff;
  font-size: 11px !important; padding-top: 2px; padding-bottom: 6px;
  border-bottom: 1px solid #3a3f4a; white-space: normal; line-height: 1.35;
}}
.footer {{ margin-top: 20px; color: #6b7280; font-size: 12px; }}
</style>
</head>
<body>
<h1>關鍵位 + 布林 + 均線 + Fib <span class="badge">全檔K線</span></h1>
<div class="meta">產生時間：{html.escape(now_str)}（台灣時間） · 共 {len(results)} 檔 · Fib＝近{FIB_DAYS}日波段 0.5–0.618 半透明帶</div>

<h2>摘要</h2>
<div class="summary">
  <span>突破5日高：{", ".join(br) if br else "無"}</span>
  <span>跌破5日低：{", ".join(bd) if bd else "無"}</span>
  <span>布林上軌：{", ".join(up) if up else "無"}</span>
  <span>布林下軌：{", ".join(dn) if dn else "無"}</span>
  <span>Fib帶內：{", ".join(fib_in) if fib_in else "無"}</span>
  <span>結構過窄：{", ".join(narrow_list) if narrow_list else "無"}</span>
  <span>站上MA20+200：{", ".join(above_both) if above_both else "無"}</span>
  <span>低於MA20+200：{", ".join(below_both) if below_both else "無"}</span>
</div>
<div class="legend">
  <i style="background:#3dd68c;margin-left:0"></i>突破
  <i style="background:#ff6b6b"></i>跌破
  <i style="background:#58a6ff"></i>支撐
  <i style="background:#f0c14b"></i>壓力
  <i style="background:#c9d1d9"></i>布林
  <i style="background:#ff7b72"></i>MA200
  <i style="background:#a371f7"></i>Fib 0.5–0.618
</div>

<h2>需關注</h2>
{''.join(focus_blocks)}

<h2>其餘（非需關注 · 也有K線）</h2>
{''.join(other_blocks)}

<h2>全部清單</h2>
<div class="wrap">
<table>
<thead><tr>
<th>代號</th><th>現價</th><th>支撐</th><th>壓力</th><th>Fib帶</th><th>Fib位置</th>
<th>狀態</th><th>布林</th><th>MA20</th><th>MA200</th><th>量能</th>
</tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
</div>

<div class="footer">
Fib：近60日高低回撤 0.5–0.618（半透明紫帶）· 波段&lt;{FIB_MIN_SPAN_PCT}%不計 · 非投資建議 · report_charts.html
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
