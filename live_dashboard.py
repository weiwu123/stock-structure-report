#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json, os, ssl, threading, time, urllib.parse, urllib.request, webbrowser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    import websocket
except ImportError:
    websocket = None

HOST = "0.0.0.0"; PORT = int(os.environ.get("PORT", "8765"))
BASE_DIR=Path(__file__).resolve().parent
STRUCTURE_JSON=BASE_DIR/'structure_data.json'
INTRADAY_STATE_JSON=BASE_DIR/'intraday_state.json'
ET=ZoneInfo('America/New_York')
PERSIST_EVERY_SEC=5
MAX_RESTORE_AGE_SEC=18*60*60
FINNHUB_API_KEY=os.environ.get('FINNHUB_API_KEY','').strip()
FINNHUB_WS_URL=(f"wss://ws.finnhub.io?token={urllib.parse.quote(FINNHUB_API_KEY)}" if FINNHUB_API_KEY else None)
BENCHMARKS=['QQQ','SPY','SMH']
STATE_LOCK=threading.Lock()
LIVE=defaultdict(lambda:{'price':None,'trade_ts':None,'prev_close':None,'day_pct':None,'mom_5m':None,'mom_10m':None})
HISTORY=defaultdict(lambda:deque(maxlen=10000))
WS_STATUS={'connected':False,'message':'not started','last_message_ts':None}
STRUCTURE_LEVELS={}
SUPPORT_STATE=defaultdict(lambda:{
    'session':None,'session_key':None,'session_low':None,'session_high':None,
    'touched_buy_zone':False,'touch_ts':None,'broke_support':False,
    'reclaimed_support':False,'bounced_from_zone':False,'min_after_touch':None,
    'last_price':None,'last_update_ts':None
})

def safe_float(v):
    try:
        x=float(v)
        if x!=x or x in (float('inf'),float('-inf')): return None
        return x
    except: return None

def clamp(v,lo=0,hi=100): return max(lo,min(hi,v))

def load_structure_data():
    if not STRUCTURE_JSON.exists():
        return {'ok':False,'error':f'找不到 {STRUCTURE_JSON.name}','path':str(STRUCTURE_JSON),'generated_at':None,'timezone':None,'symbols':{}}
    try:
        with STRUCTURE_JSON.open('r',encoding='utf-8') as f: data=json.load(f)
        symbols=data.get('symbols')
        if not isinstance(symbols,dict): raise ValueError('structure_data.json 缺少 symbols 物件')
        return {'ok':True,'error':None,'path':str(STRUCTURE_JSON),'generated_at':data.get('generated_at'),'timezone':data.get('timezone'),'symbols':symbols}
    except Exception as exc:
        return {'ok':False,'error':str(exc),'path':str(STRUCTURE_JSON),'generated_at':None,'timezone':None,'symbols':{}}

def all_symbols():
    d=load_structure_data(); syms=list(d.get('symbols',{}).keys())
    for s in BENCHMARKS:
        if s not in syms: syms.append(s)
    return syms


def refresh_structure_levels():
    data=load_structure_data()
    levels={}
    for ticker,s in data.get('symbols',{}).items():
        levels[ticker]={
            'support':safe_float(s.get('support')),
            'resistance':safe_float(s.get('resistance'))
        }
    with STATE_LOCK:
        STRUCTURE_LEVELS.clear()
        STRUCTURE_LEVELS.update(levels)

def market_session(ts=None):
    dt=datetime.fromtimestamp(ts or time.time(),ET)
    wd=dt.weekday()
    mins=dt.hour*60+dt.minute

    # Approximate U.S. equity sessions.  Overnight is treated as Sun-Thu 20:00-04:00 ET.
    if wd==5:
        return 'CLOSED', dt.strftime('%Y-%m-%d')+'-CLOSED'
    if wd==6 and mins<20*60:
        return 'CLOSED', dt.strftime('%Y-%m-%d')+'-CLOSED'

    if mins>=20*60:
        return 'OVERNIGHT', dt.strftime('%Y-%m-%d')+'-OVN'
    if mins<4*60:
        prev=(dt-timedelta(days=1)).strftime('%Y-%m-%d')
        return 'OVERNIGHT', prev+'-OVN'
    if mins<9*60+30:
        return 'PRE', dt.strftime('%Y-%m-%d')+'-PRE'
    if mins<16*60:
        return 'RTH', dt.strftime('%Y-%m-%d')+'-RTH'
    if mins<20*60:
        return 'AFTER', dt.strftime('%Y-%m-%d')+'-AFTER'
    return 'CLOSED', dt.strftime('%Y-%m-%d')+'-CLOSED'

def session_zh(session):
    return {
        'OVERNIGHT':'夜盤','PRE':'盤前','RTH':'正常盤',
        'AFTER':'盤後','CLOSED':'休市'
    }.get(session,session or '未知')

def reset_support_state(st,session,key):
    st.update({
        'session':session,'session_key':key,'session_low':None,'session_high':None,
        'touched_buy_zone':False,'touch_ts':None,'broke_support':False,
        'reclaimed_support':False,'bounced_from_zone':False,'min_after_touch':None,
        'last_price':None,'last_update_ts':None
    })

def update_support_state(symbol,price,ts):
    level=STRUCTURE_LEVELS.get(symbol) or {}
    sup=safe_float(level.get('support'))
    if sup is None:
        return

    session,key=market_session(ts)
    st=SUPPORT_STATE[symbol]
    if st.get('session_key')!=key:
        reset_support_state(st,session,key)

    st['session']=session
    st['last_price']=price
    st['last_update_ts']=ts
    st['session_low']=price if st['session_low'] is None else min(st['session_low'],price)
    st['session_high']=price if st['session_high'] is None else max(st['session_high'],price)

    buy_high=sup*1.015
    invalid=sup*0.985

    if invalid<=price<=buy_high:
        if not st['touched_buy_zone']:
            st['touch_ts']=ts
        st['touched_buy_zone']=True
        st['min_after_touch']=price if st['min_after_touch'] is None else min(st['min_after_touch'],price)

    if price<sup:
        st['broke_support']=True

    if st['broke_support'] and price>=sup:
        st['reclaimed_support']=True

    if st['touched_buy_zone'] and st['min_after_touch'] is not None:
        # "Bounced" means the market actually moved away from the tested area,
        # not merely one green tick.
        rebound_from_low=(price/st['min_after_touch']-1)*100 if st['min_after_touch'] else 0
        if price>buy_high or rebound_from_low>=0.8:
            st['bounced_from_zone']=True

def sample_history(hist,bucket_sec=5):
    out=[]
    last_bucket=None
    for ts,price in hist:
        bucket=int(ts//bucket_sec)
        if bucket==last_bucket:
            out[-1]=[ts,price]
        else:
            out.append([ts,price])
            last_bucket=bucket
    return out

def persist_intraday_state():
    tmp=INTRADAY_STATE_JSON.with_suffix('.json.tmp')
    with STATE_LOCK:
        payload={
            'saved_at':time.time(),
            'symbols':{}
        }
        symbols=set(LIVE.keys())|set(HISTORY.keys())|set(SUPPORT_STATE.keys())
        for s in symbols:
            payload['symbols'][s]={
                'live':dict(LIVE.get(s,{})),
                'history':sample_history(HISTORY.get(s,deque())),
                'support_state':dict(SUPPORT_STATE.get(s,{}))
            }
    try:
        tmp.write_text(json.dumps(payload,ensure_ascii=False,allow_nan=False),encoding='utf-8')
        tmp.replace(INTRADAY_STATE_JSON)
    except Exception as exc:
        print('state save error:',exc)

def persistence_loop():
    while True:
        time.sleep(PERSIST_EVERY_SEC)
        persist_intraday_state()

def restore_intraday_state():
    if not INTRADAY_STATE_JSON.exists():
        print('State: no saved intraday_state.json')
        return
    try:
        obj=json.loads(INTRADAY_STATE_JSON.read_text(encoding='utf-8'))
        saved=safe_float(obj.get('saved_at'))
        if not saved or time.time()-saved>MAX_RESTORE_AGE_SEC:
            print('State: saved state too old, ignored')
            return

        cutoff=time.time()-20*60
        restored=0
        with STATE_LOCK:
            for s,d in obj.get('symbols',{}).items():
                lv=d.get('live') or {}
                for k in LIVE[s]:
                    if k in lv:
                        LIVE[s][k]=lv[k]

                hist=d.get('history') or []
                HISTORY[s].clear()
                for item in hist:
                    if isinstance(item,list) and len(item)==2:
                        ts=safe_float(item[0]); p=safe_float(item[1])
                        if ts and p and ts>=cutoff:
                            HISTORY[s].append((ts,p))

                ss=d.get('support_state') or {}
                if ss:
                    SUPPORT_STATE[s].update(ss)
                restored+=1

                if HISTORY[s]:
                    m5,m10=calculate_momentum(s,HISTORY[s][-1][0])
                    LIVE[s]['mom_5m']=m5
                    LIVE[s]['mom_10m']=m10

        print(f'State: restored {restored} symbols from intraday_state.json')
    except Exception as exc:
        print('State restore error:',exc)

def finnhub_quote(symbol):
    if not FINNHUB_API_KEY: return None
    url='https://finnhub.io/api/v1/quote?'+urllib.parse.urlencode({'symbol':symbol,'token':FINNHUB_API_KEY})
    req=urllib.request.Request(url,headers={'User-Agent':'stock-structure-live-dashboard/1.0'})
    with urllib.request.urlopen(req,timeout=8) as resp: obj=json.loads(resp.read().decode('utf-8'))
    return {'current':safe_float(obj.get('c')),'prev_close':safe_float(obj.get('pc'))}

def bootstrap_quotes(symbols):
    if not FINNHUB_API_KEY:
        print('WARN : FINNHUB_API_KEY 未設定，live feed 不會啟動。'); return
    print(f'Quote bootstrap: {len(symbols)} symbols')
    for i,s in enumerate(symbols,1):
        try:
            q=finnhub_quote(s); now=time.time()
            if q:
                with STATE_LOCK:
                    if q['prev_close'] is not None: LIVE[s]['prev_close']=q['prev_close']
                    if q['current'] is not None and q['current']>0:
                        LIVE[s]['price']=q['current']; LIVE[s]['trade_ts']=now; HISTORY[s].append((now,q['current']))
                        pc=LIVE[s]['prev_close']
                        if pc: LIVE[s]['day_pct']=((q['current']/pc)-1)*100
            print(f'  [{i:02d}/{len(symbols)}] {s}: ok')
        except Exception as exc: print(f'  [{i:02d}/{len(symbols)}] {s}: quote error: {exc}')
        time.sleep(0.25)

def price_at_or_before(hist,target):
    for ts,p in reversed(hist):
        if ts<=target: return p
    return None

def calculate_momentum(symbol,now_ts):
    hist=HISTORY[symbol]
    if not hist: return None,None
    cur=hist[-1][1]; p5=price_at_or_before(hist,now_ts-300); p10=price_at_or_before(hist,now_ts-600)
    return (((cur/p5)-1)*100 if p5 else None, ((cur/p10)-1)*100 if p10 else None)

def cleanup_history(now_ts):
    cutoff=now_ts-1200
    for hist in HISTORY.values():
        while hist and hist[0][0]<cutoff: hist.popleft()

def process_trade(symbol,price,trade_ts):
    if not symbol or price is None or price<=0: return
    now_ts=trade_ts or time.time()
    with STATE_LOCK:
        HISTORY[symbol].append((now_ts,price)); cleanup_history(now_ts)
        LIVE[symbol]['price']=price; LIVE[symbol]['trade_ts']=now_ts
        pc=LIVE[symbol]['prev_close']
        if pc: LIVE[symbol]['day_pct']=((price/pc)-1)*100
        m5,m10=calculate_momentum(symbol,now_ts); LIVE[symbol]['mom_5m']=m5; LIVE[symbol]['mom_10m']=m10
        update_support_state(symbol,price,now_ts)

def ws_on_open(ws):
    syms=all_symbols()
    with STATE_LOCK:
        WS_STATUS['connected']=True; WS_STATUS['message']=f'connected / subscribed {len(syms)} symbols'
    print(f'Finnhub WebSocket connected. Subscribing {len(syms)} symbols...')
    for s in syms:
        try: ws.send(json.dumps({'type':'subscribe','symbol':s}))
        except Exception as exc: print(f'{s}: subscribe error: {exc}')

def ws_on_message(ws,message):
    try: obj=json.loads(message)
    except: return
    with STATE_LOCK: WS_STATUS['last_message_ts']=time.time()
    if obj.get('type')!='trade': return
    for t in obj.get('data',[]):
        ts=safe_float(t.get('t')); process_trade(t.get('s'),safe_float(t.get('p')),ts/1000 if ts else time.time())

def ws_on_error(ws,error):
    with STATE_LOCK: WS_STATUS['connected']=False; WS_STATUS['message']=f'error: {error}'

def ws_on_close(ws,status_code,msg):
    with STATE_LOCK: WS_STATUS['connected']=False; WS_STATUS['message']=f'closed: {status_code} {msg or ""}'.strip()

def websocket_loop():
    if websocket is None:
        with STATE_LOCK: WS_STATUS['message']='websocket-client 未安裝。執行: pip install websocket-client'
        return
    if not FINNHUB_API_KEY:
        with STATE_LOCK: WS_STATUS['message']='FINNHUB_API_KEY 未設定'
        return
    while True:
        try:
            app=websocket.WebSocketApp(FINNHUB_WS_URL,on_open=ws_on_open,on_message=ws_on_message,on_error=ws_on_error,on_close=ws_on_close)
            app.run_forever(ping_interval=20,ping_timeout=10,sslopt={'cert_reqs':ssl.CERT_REQUIRED})
        except Exception as exc:
            with STATE_LOCK: WS_STATUS['connected']=False; WS_STATUS['message']=f'reconnect error: {exc}'
        time.sleep(5)

def bench(symbol,field): return safe_float(LIVE.get(symbol,{}).get(field))

def position_guidance(ticker,s,live):
    price=safe_float(live.get('price')) or safe_float(s.get('price'))
    day=safe_float(live.get('day_pct'))
    m5=safe_float(live.get('mom_5m')); m10=safe_float(live.get('mom_10m'))
    sup=safe_float(s.get('support')); res=safe_float(s.get('resistance'))
    ma20=safe_float(s.get('ma20')); ma200=safe_float(s.get('ma200'))

    q5=bench('QQQ','mom_5m'); h5=bench('SMH','mom_5m')
    rq=None if m5 is None or q5 is None else m5-q5
    rs=None if m5 is None or h5 is None else m5-h5

    buy_low=sup
    buy_high=sup*1.015 if sup else None
    invalid=sup*0.985 if sup else None
    ds=None if price is None or sup is None else (price/sup-1)*100
    dr=None if price is None or res is None else (res/price-1)*100

    st=dict(SUPPORT_STATE.get(ticker,{}))
    session=st.get('session')
    if not session:
        session,_=market_session(live.get('trade_ts') or time.time())

    touched=bool(st.get('touched_buy_zone'))
    broke=bool(st.get('broke_support'))
    reclaimed=bool(st.get('reclaimed_support'))
    bounced=bool(st.get('bounced_from_zone'))
    session_low=safe_float(st.get('session_low'))

    turn=m5 is not None and m5>=0.15 and (m10 is None or m5>=m10/2)
    rv=[x for x in (rq,rs) if x is not None]
    rel=bool(rv) and max(rv)>=0.15

    now='觀察'; detail='等待價格接近明確結構位置。'; priority=40

    if price is None or sup is None:
        now='資料不足'; detail='缺少即時價格或支撐資料。'; priority=0

    elif session!='RTH':
        tag=session_zh(session)
        if price<invalid:
            now=f'{tag}結構失效・不接'
            detail=f'{tag}已明顯跌破支撐 {sup:.2f}；正式盤先看能否重新站回。'
            priority=100
        elif price<sup:
            now=f'{tag}跌破支撐・觀察收復'
            detail=f'{tag}價格在支撐 {sup:.2f} 下方；先列警戒，不直接低接。'
            priority=95
        elif touched and bounced and price>buy_high:
            now=f'{tag}接區守住・列入觀察'
            detail=f'{tag}曾測試 {buy_low:.2f}–{buy_high:.2f} 且未失效後彈離；正式盤等回踩/5m確認，不追。'
            priority=88
        elif price<=buy_high:
            now=f'{tag}測試接區・列入觀察'
            detail=f'已進入 {buy_low:.2f}–{buy_high:.2f}；{tag}流動性較低，只記錄位置，不直接確認買點。'
            priority=84
        elif ds is not None and ds<=4:
            now=f'{tag}接近接區'
            detail=f'距支撐約 {ds:.1f}%；把 {buy_low:.2f}–{buy_high:.2f} 列為正式盤重點區。'
            priority=70
        elif res is not None and dr is not None and 0<=dr<=1.5:
            now=f'{tag}近壓力・勿追'
            detail=f'距壓力 {res:.2f} 很近；非正常盤不追價。'
            priority=72
        else:
            now=f'{tag}離接區尚遠'
            detail=f'目前距支撐約 {ds:.1f}%；先記錄，不因{tag}漲跌直接動作。' if ds is not None else f'{tag}觀察。'
            priority=35

    else:
        if price<invalid:
            now='結構失效・不接'
            detail=f'明顯跌破支撐 {sup:.2f}；先等重新站回支撐。'
            priority=100
        elif price<sup:
            if reclaimed:
                now='再度失守・先不接'
                detail=f'本盤曾收復 {sup:.2f} 但又跌回下方；等待再次收復。'
            else:
                now='跌破支撐・等收復'
                detail=f'在支撐 {sup:.2f} 下方；不要因跌深直接接，先等收復。'
            priority=95
        elif broke and reclaimed and (turn or bounced):
            now='收復支撐・可試接'
            detail=f'本盤曾跌破 {sup:.2f}，現在已收復且出現止跌/反彈；可考慮小量試單。'
            priority=92
        elif touched and bounced and price>buy_high:
            now='接區守住・等回踩'
            detail=f'本盤已測過接區且沒有失效，現已彈離；不追，等回踩 {buy_high:.2f} 附近或短線再確認。'
            priority=87
        elif price<=buy_high:
            if turn and rel:
                now='接區確認・可分批'
                detail='價格在接區，5m止跌轉強且相對QQQ/SMH不弱；可考慮小量分批。'
                priority=90
            elif turn:
                now='可小量試接'
                detail='已到接區且5m開始止跌轉強；相對強弱尚未完全確認。'
                priority=85
            else:
                now='已進接區・等止跌'
                detail='位置到了，但動能尚未確認；等5m轉正/止跌，不盲接。'
                priority=80
        elif touched and not broke and price>buy_high:
            now='接區守住・離開接區'
            detail='本盤曾進入接區且未跌破支撐，現在已離開；不追高，等下一次回踩。'
            priority=82
        elif ds is not None and ds<=4:
            now='接近接區・準備'
            detail=f'距支撐約 {ds:.1f}%；等進入 {buy_low:.2f}–{buy_high:.2f}。'
            priority=70
        elif res is not None and dr is not None and 0<=dr<=1.5:
            now='近壓力・不要追'
            detail=f'距壓力 {res:.2f} 很近；新倉不追，已有持股觀察突破或減碼。'
            priority=75
        elif s.get('breakout') and res is not None and price>=res:
            now='突破・等回踩'
            detail=f'已突破原壓力 {res:.2f}；不追高，優先等回踩站穩。'
            priority=72
        else:
            now='離接區尚遠'
            detail=f'目前離支撐約 {ds:.1f}%；先等，不因盤中下跌就提前接。' if ds is not None else '等待結構位置。'
            priority=35

    overhead=[]
    if price is not None:
        for label,val in (('壓力',res),('MA20',ma20),('MA200',ma200)):
            if val is not None and val>price: overhead.append((val,label))
    overhead.sort()
    nr=overhead[0][0] if overhead else res
    nl=overhead[0][1] if overhead else ('壓力' if res else None)

    return {
        'now':now,'detail':detail,'priority':priority,'session':session,'session_zh':session_zh(session),
        'buy_low':buy_low,'buy_high':buy_high,'invalid':invalid,
        'dist_support_pct':ds,'dist_resistance_pct':dr,
        'next_resistance':nr,'next_resistance_label':nl,
        'rs5_qqq':rq,'rs5_smh':rs,'momentum_turn':turn,'relative_ok':rel,
        'day_pct':day,'session_low':session_low,'touched_buy_zone':touched,
        'broke_support':broke,'reclaimed_support':reclaimed,'bounced_from_zone':bounced
    }


def live_snapshot():
    structure=load_structure_data()
    with STATE_LOCK:
        live={s:dict(d) for s,d in LIVE.items()}; ws=dict(WS_STATUS)
    scores={t:position_guidance(t,s,live.get(t,{})) for t,s in structure.get('symbols',{}).items()}
    return {'ok':True,'websocket':ws,'symbols':live,'scores':scores,'server_time':time.time()}

HTML=r"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Structure Entry Dashboard</title>
<style>:root{color-scheme:dark;--bg:#0d1117;--p:#161b22;--p2:#1f2630;--l:#30363d;--t:#e6edf3;--m:#8b949e;--g:#3fb950;--r:#f85149;--y:#d29922;--b:#58a6ff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t);font-family:Segoe UI,Noto Sans TC,Arial,sans-serif}.wrap{max-width:1800px;margin:auto;padding:20px}.head{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:end}h1{margin:0}.sub,.muted{color:var(--m)}input,select{background:var(--p);color:var(--t);border:1px solid var(--l);padding:9px;border-radius:8px}.status{margin:14px 0;padding:10px;background:var(--p);border:1px solid var(--l);border-radius:9px}.bench{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}.bench span{background:var(--p);border:1px solid var(--l);padding:7px 10px;border-radius:8px}table{width:100%;border-collapse:separate;border-spacing:0;background:var(--p);border:1px solid var(--l);border-radius:10px;overflow:hidden}th,td{padding:8px;border-bottom:1px solid var(--l);font-size:13px;text-align:right;white-space:nowrap}th{background:var(--p2);position:sticky;top:0;cursor:pointer}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2),th:last-child,td:last-child{text-align:left}.good{color:var(--g)}.bad{color:var(--r)}.warn{color:var(--y)}.blue{color:var(--b)}.ticker{font-weight:800}.now{font-weight:800}.detail{white-space:normal;min-width:320px;max-width:440px;line-height:1.35}.controls{display:flex;gap:8px}.wraptable{overflow:auto}</style></head>
<body><div class="wrap"><div class="head"><div><h1>Structure Entry Dashboard</h1><div id="sub" class="sub">loading...</div></div><div class="controls"><input id="q" placeholder="搜尋 ticker"><select id="f"><option value="all">全部</option><option value="接">接區相關</option><option value="跌破">跌破/失效</option><option value="壓力">近壓力</option><option value="等待">等待</option></select></div></div><div id="status" class="status">loading...</div><div id="bench" class="bench"></div>
<div class="wraptable"><table><thead><tr><th data-k="ticker">Ticker</th><th data-k="now">NOW</th><th data-k="session">Session</th><th data-k="priority">Priority</th><th data-k="live">Live</th><th data-k="day">Day%</th><th data-k="m5">5m%</th><th data-k="ds">距支撐%</th><th>Session Low</th><th>接區</th><th>失效</th><th>下一壓力</th><th data-k="rq">5m vs QQQ</th><th data-k="rs">5m vs SMH</th><th>當下訊息</th></tr></thead><tbody id="rows"></tbody></table></div></div>
<script>let S=[],L={},G={},key="priority",asc=false;const fmt=(v,d=2)=>v==null?"—":Number(v).toFixed(d);const esc=s=>String(s??"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");const pc=v=>v==null?"muted":v>0?"good":v<0?"bad":"";const nc=s=>s.includes("失效")||s.includes("跌破")?"bad":s.includes("可")||s.includes("確認")?"good":s.includes("接區")||s.includes("準備")?"blue":s.includes("壓力")?"warn":"";
function merge(){return S.map(x=>{let l=L[x.ticker]||{},g=G[x.ticker]||{};return {...x,...g,live:l.price??null,day:l.day_pct??null,m5:l.mom_5m??null,rq:g.rs5_qqq??null,rs:g.rs5_smh??null,ds:g.dist_support_pct??null}})}
function filt(a){let q=document.getElementById("q").value.trim().toUpperCase(),f=document.getElementById("f").value;return a.filter(x=>{if(q&&!x.ticker.includes(q))return false;if(f==="接"&&!(/接|準備/.test(x.now)))return false;if(f==="跌破"&&!(/跌破|失效/.test(x.now)))return false;if(f==="壓力"&&!x.now.includes("壓力"))return false;if(f==="等待"&&!(/等|遠/.test(x.now)))return false;return true})}
function render(){let a=filt(merge());a.sort((x,y)=>{let A=x[key],B=y[key];if(typeof A==="number"&&typeof B==="number")return asc?A-B:B-A;return asc?String(A??"").localeCompare(String(B??"")):String(B??"").localeCompare(String(A??""))});document.getElementById("rows").innerHTML=a.map(x=>`<tr><td class="ticker">${esc(x.ticker)}</td><td class="now ${nc(x.now||"")}">${esc(x.now||"—")}</td><td>${esc(x.session_zh||x.session||"—")}</td><td>${x.priority??0}</td><td>${fmt(x.live)}</td><td class="${pc(x.day)}">${fmt(x.day)}${x.day==null?"":"%"}</td><td class="${pc(x.m5)}">${fmt(x.m5)}${x.m5==null?"":"%"}</td><td class="${pc(x.ds)}">${fmt(x.ds)}${x.ds==null?"":"%"}</td><td>${fmt(x.session_low)}</td><td>${x.buy_low==null?"—":fmt(x.buy_low)+" – "+fmt(x.buy_high)}</td><td class="bad">${fmt(x.invalid)}</td><td>${x.next_resistance==null?"—":esc(x.next_resistance_label||"")+" "+fmt(x.next_resistance)}</td><td class="${pc(x.rq)}">${fmt(x.rq)}${x.rq==null?"":"%"}</td><td class="${pc(x.rs)}">${fmt(x.rs)}${x.rs==null?"":"%"}</td><td class="detail">${esc(x.detail||"")}</td></tr>`).join("")}
async function structure(){let d=await(await fetch("/api/structure?_="+Date.now())).json();if(d.ok){S=Object.entries(d.symbols).map(([ticker,x])=>({ticker,...x}));document.getElementById("sub").textContent=`Structure: ${d.generated_at||"—"} (${d.timezone||"—"})`;render()}}
async function live(){try{let d=await(await fetch("/api/live?_="+Date.now())).json();L=d.symbols||{};G=d.scores||{};let w=d.websocket||{};document.getElementById("status").textContent=w.connected?`Finnhub LIVE connected｜${w.message||""}`:`Finnhub 尚未連線｜${w.message||""}`;document.getElementById("bench").innerHTML=["QQQ","SPY","SMH"].map(t=>{let z=L[t]||{};return `<span><b>${t}</b> ${fmt(z.price)} <i class="${pc(z.day_pct)}">${fmt(z.day_pct)}%</i> 5m <i class="${pc(z.mom_5m)}">${fmt(z.mom_5m)}%</i></span>`}).join("");render()}catch(e){document.getElementById("status").textContent="Live API error: "+e}}
document.getElementById("q").addEventListener("input",render);document.getElementById("f").addEventListener("change",render);document.querySelectorAll("th[data-k]").forEach(h=>h.onclick=()=>{let k=h.dataset.k;if(key===k)asc=!asc;else{key=k;asc=true}render()});structure();live();setInterval(live,2000);setInterval(structure,30000);</script></body></html>"""
class DashboardHandler(BaseHTTPRequestHandler):
    def sendb(self,b,ct,status=200):
        self.send_response(status); self.send_header('Content-Type',ct); self.send_header('Content-Length',str(len(b))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p=urlparse(self.path).path
        if p=='/': return self.sendb(HTML.encode(),'text/html; charset=utf-8')
        if p=='/api/structure':
            refresh_structure_levels(); return self.sendb(json.dumps(load_structure_data(),ensure_ascii=False,allow_nan=False).encode(),'application/json; charset=utf-8')
        if p=='/api/live': return self.sendb(json.dumps(live_snapshot(),ensure_ascii=False,allow_nan=False).encode(),'application/json; charset=utf-8')
        return self.sendb(b'Not Found','text/plain',404)
    def log_message(self,*args): pass

def main():
    print('Stock Structure Live Dashboard - Scoring')
    print('JSON:',STRUCTURE_JSON); print(f'URL : http://{HOST}:{PORT}')
    d=load_structure_data(); print('OK  :',len(d.get('symbols',{})),'structure symbols' if d.get('ok') else d.get('error'))
    if websocket is None: print('WARN: pip install websocket-client')
    if not FINNHUB_API_KEY: print('WARN: FINNHUB_API_KEY 未設定')
    refresh_structure_levels()
    restore_intraday_state()
    syms=all_symbols()
    threading.Thread(target=bootstrap_quotes,args=(syms,),daemon=True).start()
    threading.Thread(target=websocket_loop,daemon=True).start()
    threading.Thread(target=persistence_loop,daemon=True).start()
    srv=ThreadingHTTPServer((HOST,PORT),DashboardHandler); threading.Timer(0.8,lambda:webbrowser.open(f'http://{HOST}:{PORT}')).start()
    print('Press Ctrl+C to stop.')
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        persist_intraday_state(); srv.server_close()

if __name__=='__main__': main()
