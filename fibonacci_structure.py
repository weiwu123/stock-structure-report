"""Completed-daily-bar Fibonacci context; no network, trading, or file writes."""

VERSION = 'fib-shadow-1'

RATIOS = (0.382, 0.5, 0.618, 0.786)

RULES = dict(lookback=60, pivot_left=3, pivot_right=3, min_bars=80,
             min_swing_atr=2, tolerance_atr=0.25, max_daily_calendar_age=6,
             live_max_age_seconds=90, bar_final_after_et='16:15')

def classify(context, price):
    levels = context['levels']
    low, high = context['low']['price'], context['high']['price']
    tol, ma = context['tolerance'], context['ma20']
    rising = ma >= context['ma20_lag5']
    if price < low - tol:
        return 'BREAKDOWN'
    if context['direction'] == 'UP':
        if price < levels['0.786'] - tol and price < ma:
            return 'BREAKDOWN'
        if price >= levels['0.382'] - tol and price >= ma and rising:
            return 'UPTREND'
        if levels['0.786'] - tol <= price < levels['0.382'] - tol:
            return 'PULLBACK'
    else:
        if price <= low + tol and price < ma:
            return 'BREAKDOWN'
        if price > high + tol and price >= ma and rising:
            return 'UPTREND'
    return 'RANGE'

def analyze(rows):
    """Input MUST be a prefix of completed daily bars, sorted by Date."""
    base = dict(version=VERSION, mode='SHADOW', quality='INSUFFICIENT_HISTORY',
                daily_state='NO_DATA', bars=len(rows), levels={})
    if len(rows) < RULES['min_bars']:
        return base
    end = len(rows) - 1
    tr = [max(rows[i]['High'] - rows[i]['Low'],
              abs(rows[i]['High'] - rows[i-1]['Close']),
              abs(rows[i]['Low'] - rows[i-1]['Close'])) for i in range(end-13, end+1)]
    atr = sum(tr) / 14
    base.update(asof_date=rows[-1]['Date'], close=rows[-1]['Close'], atr14=atr,
                ma20=sum(r['Close'] for r in rows[-20:])/20,
                ma20_lag5=sum(r['Close'] for r in rows[-25:-5])/20,
                ma200=sum(r['Close'] for r in rows[-200:])/200 if len(rows) >= 200 else None,
                tolerance=atr * RULES['tolerance_atr'])
    if any(abs(rows[i]['Open']/rows[i-1]['Close']-1) > .35
           for i in range(max(1,len(rows)-200),len(rows))):
        base['ma200'] = None
    window_start = max(3, len(rows)-RULES['lookback'])
    # Unadjusted cached history: quarantine large discontinuities, not auto-correct.
    jumps = [rows[i]['Date'] for i in range(max(1, window_start-3), len(rows))
             if abs(rows[i]['Open']/rows[i-1]['Close']-1) > .35]
    if jumps:
        base.update(quality='PRICE_DISCONTINUITY', discontinuity_dates=jumps)
        return base
    pivots = []
    for i in range(window_start, end-2):
        w = rows[i-3:i+4]
        # Tied extrema choose the rightmost occurrence. Outside bars are ambiguous.
        hi = rows[i]['High'] == max(r['High'] for r in w) and all(rows[i]['High'] > r['High'] for r in w[4:])
        lo = rows[i]['Low'] == min(r['Low'] for r in w) and all(rows[i]['Low'] < r['Low'] for r in w[4:])
        if hi == lo:
            continue
        kind, value = ('H', rows[i]['High']) if hi else ('L', rows[i]['Low'])
        pivot = dict(kind=kind, price=value, date=rows[i]['Date'], confirmed_on=rows[i+3]['Date'])
        if pivots and pivots[-1]['kind'] == kind:
            old = pivots[-1]['price']
            if (kind == 'H' and value >= old) or (kind == 'L' and value <= old):
                pivots[-1] = pivot
        else:
            pivots.append(pivot)
    for a, b in reversed(list(zip(pivots, pivots[1:]))):
        low, high = (a, b) if a['kind'] == 'L' else (b, a)
        span = high['price'] - low['price']
        if span <= 0 or span < RULES['min_swing_atr'] * atr:
            continue
        direction = 'UP' if a['kind'] == 'L' else 'DOWN'
        levels = {str(r): high['price'] - r*span if direction == 'UP'
                  else low['price'] + r*span for r in RATIOS}
        base.update(quality='OK', direction=direction, low=low, high=high,
                    confirmed_on=b['confirmed_on'], levels=levels, span_atr=span/atr)
        base['daily_state'] = classify(base, rows[-1]['Close'])
        return base
    base['quality'] = 'NO_CONFIRMED_SWING'
    return base
