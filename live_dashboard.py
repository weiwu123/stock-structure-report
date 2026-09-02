#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json, os, ssl, threading, time, urllib.parse, urllib.request, webbrowser
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    import websocket
except ImportError:
    websocket = None

HOST='127.0.0.1'; PORT=8765
BASE_DIR=Path(__file__).resolve().parent
STRUCTURE_JSON=BASE_DIR/'structure_data.json'
FINNHUB_API_KEY=os.environ.get('FINNHUB_API_KEY','').strip()
FINNHUB_WS_URL=(f"wss://ws.finnhub.io?token={urllib.parse.quote(FINNHUB_API_KEY)}" if FINNHUB_API_KEY else None)
BENCHMARKS=['QQQ','SPY','SMH']
STATE_LOCK=threading.Lock()
LIVE=defaultdict(lambda:{'price':None,'trade_ts':None,'prev_close':None,'day_pct':None,'mom_5m':None,'mom_10m':None})
HISTORY=defaultdict(lambda:deque(maxlen=10000))
WS_STATUS={'connected':False,'message':'not started','last_message_ts':None}

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

def score_symbol(ticker,s,live):
    price=safe_float(live.get('price')) or safe_float(s.get('price'))
    day=safe_float(live.get('day_pct')); m5=safe_float(live.get('mom_5m')); m10=safe_float(live.get('mom_10m'))
    rsq=None if day is None or bench('QQQ','day_pct') is None else day-bench('QQQ','day_pct')
    rss=None if day is None or bench('SMH','day_pct') is None else day-bench('SMH','day_pct')
    rs5q=None if m5 is None or bench('QQQ','mom_5m') is None else m5-bench('QQQ','mom_5m')
    rs5s=None if m5 is None or bench('SMH','mom_5m') is None else m5-bench('SMH','mom_5m')
    add=50.; risk=50.; ra=[]; rr=[]
    sup=safe_float(s.get('support')); res=safe_float(s.get('resistance')); ma20=safe_float(s.get('ma20')); ma200=safe_float(s.get('ma200'))
    if s.get('near_support'): add+=10; ra.append('近支撐')
    if s.get('near_resistance'): add-=5; risk+=6; rr.append('近壓力')
    if s.get('breakout'): add+=12; ra.append('日K突破')
    if s.get('breakdown'): add-=18; risk+=18; rr.append('日K跌破')
    ch=str(s.get('channel') or '')
    if '上' in ch: add+=6; risk-=4; ra.append('上升結構')
    elif '下' in ch: add-=7; risk+=8; rr.append('下降結構')
    if price is not None and ma20 is not None:
        if price>=ma20: add+=6
        else: add-=5; risk+=5; rr.append('低於MA20')
    if price is not None and ma200 is not None:
        if price>=ma200: add+=7; risk-=4; ra.append('高於MA200')
        else: add-=9; risk+=10; rr.append('低於MA200')
    if price is not None and sup:
        ds=((price/sup)-1)*100
        if -1<=ds<=2: add+=7; ra.append('貼近支撐')
        if ds<-1: add-=10; risk+=12; rr.append('跌破支撐')
    if m5 is not None:
        if m5>=0.6: add+=9; ra.append('5m強')
        elif m5>=0.2: add+=4
        elif m5<=-0.6: add-=9; risk+=10; rr.append('5m弱')
        elif m5<=-0.2: add-=4; risk+=4
    if m10 is not None:
        if m10>=1.0: add+=9; ra.append('10m強')
        elif m10>=0.4: add+=4
        elif m10<=-1.0: add-=9; risk+=10; rr.append('10m弱')
        elif m10<=-0.4: add-=4; risk+=4
    for val,label in [(rsq,'強於QQQ'),(rss,'強於SMH')]:
        if val is not None:
            if val>=1: add+=8; ra.append(label)
            elif val>=0.4: add+=4
            elif val<=-1: add-=8; risk+=8; rr.append(label.replace('強於','弱於'))
            elif val<=-0.4: add-=4; risk+=4
    for val,label in [(rs5q,'5m強於QQQ'),(rs5s,'5m強於SMH')]:
        if val is not None:
            if val>=0.5: add+=5; ra.append(label)
            elif val<=-0.5: add-=5; risk+=5; rr.append(label.replace('強於','弱於'))
    add=clamp(round(add)); risk=clamp(round(risk))
    if risk>=78 and add<=42: signal='SHORT'; score=risk
    elif risk>=65 and add<=52: signal='REDUCE'; score=risk
    elif add>=72 and risk<65: signal='ADD'; score=add
    else: signal='HOLD'; score=max(add,100-risk)
    return {'signal':signal,'score':int(clamp(score)),'add_score':int(add),'risk_score':int(risk),'rs_qqq':rsq,'rs_smh':rss,'rs5_qqq':rs5q,'rs5_smh':rs5s,'reasons_add':ra[:4],'reasons_risk':rr[:4]}

def live_snapshot():
    structure=load_structure_data()
    with STATE_LOCK:
        live={s:dict(d) for s,d in LIVE.items()}; ws=dict(WS_STATUS)
    scores={t:score_symbol(t,s,live.get(t,{})) for t,s in structure.get('symbols',{}).items()}
    return {'ok':True,'websocket':ws,'symbols':live,'scores':scores,'server_time':time.time()}

HTML='''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Live Dashboard</title>
<style>body{font-family:Segoe UI,Arial;background:#0d1117;color:#e6edf3;margin:0}.wrap{max-width:1700px;margin:auto;padding:20px}.status,.card,table{background:#161b22;border:1px solid #30363d}.status{padding:10px;border-radius:8px;margin:12px 0}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}.card{padding:12px;border-radius:8px}.n{font-size:24px;font-weight:800}.good{color:#3fb950}.bad{color:#f85149}.warn{color:#d29922}.muted{color:#8b949e}.bench{display:inline-block;background:#161b22;border:1px solid #30363d;padding:7px 10px;border-radius:8px;margin:0 6px 10px 0}table{width:100%;border-collapse:collapse}th,td{padding:8px;border-bottom:1px solid #30363d;text-align:right;font-size:13px}th{background:#1f2630;position:sticky;top:0}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2),td:last-child{text-align:left}.signal{font-weight:800}.reason{white-space:normal;max-width:320px}input,select{background:#161b22;color:#e6edf3;border:1px solid #30363d;padding:8px;border-radius:7px;margin-right:8px}</style></head><body><div class="wrap">
<h1>Stock Structure Live Dashboard</h1><div id="sub" class="muted"></div><div><input id="search" placeholder="Ticker"><select id="filter"><option value="all">全部</option><option>ADD</option><option>HOLD</option><option>REDUCE</option><option>SHORT</option></select></div><div id="status" class="status">載入中...</div><div id="bench"></div>
<div class="cards"><div class="card"><div id="a" class="n good">0</div>ADD</div><div class="card"><div id="h" class="n">0</div>HOLD</div><div class="card"><div id="r" class="n warn">0</div>REDUCE</div><div class="card"><div id="s" class="n bad">0</div>SHORT</div></div>
<table><thead><tr><th>Ticker</th><th>Signal</th><th>Score</th><th>Live</th><th>Day %</th><th>5m %</th><th>10m %</th><th>vs QQQ</th><th>vs SMH</th><th>Support</th><th>Resistance</th><th>Why</th></tr></thead><tbody id="rows"></tbody></table></div>
<script>
let st=[],lv={},sc={};function f(v){return v==null?'—':Number(v).toFixed(2)}function pc(v){return v>0?'good':v<0?'bad':''}function sigc(v){return v==='ADD'?'good':v==='SHORT'?'bad':v==='REDUCE'?'warn':''}
function merged(){return st.map(x=>{let l=lv[x.ticker]||{},z=sc[x.ticker]||{};return {...x,live:l.price,day:l.day_pct,m5:l.mom_5m,m10:l.mom_10m,signal:z.signal||'HOLD',score:z.score||0,rsq:z.rs_qqq,rss:z.rs_smh,why:[...(z.reasons_add||[]),...(z.reasons_risk||[])].slice(0,5).join(' / ')}})}
function render(){let q=document.getElementById('search').value.toUpperCase(),fl=document.getElementById('filter').value;let x=merged().filter(o=>(!q||o.ticker.includes(q))&&(fl==='all'||o.signal===fl)).sort((a,b)=>b.score-a.score);document.getElementById('rows').innerHTML=x.map(o=>`<tr><td><b>${o.ticker}</b></td><td class="signal ${sigc(o.signal)}">${o.signal}</td><td class="${sigc(o.signal)}"><b>${o.score}</b></td><td>${f(o.live)}</td><td class="${pc(o.day)}">${f(o.day)}${o.day==null?'':'%'}</td><td class="${pc(o.m5)}">${f(o.m5)}${o.m5==null?'':'%'}</td><td class="${pc(o.m10)}">${f(o.m10)}${o.m10==null?'':'%'}</td><td class="${pc(o.rsq)}">${f(o.rsq)}${o.rsq==null?'':'%'}</td><td class="${pc(o.rss)}">${f(o.rss)}${o.rss==null?'':'%'}</td><td>${f(o.support)}</td><td>${f(o.resistance)}</td><td class="reason">${o.why}</td></tr>`).join('');let all=merged();a.textContent=all.filter(o=>o.signal==='ADD').length;h.textContent=all.filter(o=>o.signal==='HOLD').length;r.textContent=all.filter(o=>o.signal==='REDUCE').length;s.textContent=all.filter(o=>o.signal==='SHORT').length}
async function rs(){let d=await (await fetch('/api/structure?'+Date.now())).json();if(d.ok){st=Object.entries(d.symbols).map(([ticker,x])=>({ticker,...x}));sub.textContent=`Structure: ${d.generated_at||'—'} (${d.timezone||'—'})`;render()}}
async function rl(){let d=await (await fetch('/api/live?'+Date.now())).json();lv=d.symbols||{};sc=d.scores||{};status.textContent=d.websocket?.connected?`Finnhub LIVE connected | ${d.websocket.message||''}`:`Finnhub 尚未連線 | ${d.websocket?.message||''}`;bench.innerHTML=['QQQ','SPY','SMH'].map(t=>{let x=lv[t]||{};return `<span class="bench"><b>${t}</b> ${f(x.price)} <span class="${pc(x.day_pct)}">${f(x.day_pct)}${x.day_pct==null?'':'%'}</span> 5m <span class="${pc(x.mom_5m)}">${f(x.mom_5m)}${x.mom_5m==null?'':'%'}</span></span>`}).join('');render()}
search.oninput=render;filter.onchange=render;rs();rl();setInterval(rl,2000);setInterval(rs,30000);
</script></body></html>'''

class DashboardHandler(BaseHTTPRequestHandler):
    def sendb(self,b,ct,status=200):
        self.send_response(status); self.send_header('Content-Type',ct); self.send_header('Content-Length',str(len(b))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p=urlparse(self.path).path
        if p=='/': return self.sendb(HTML.encode(),'text/html; charset=utf-8')
        if p=='/api/structure': return self.sendb(json.dumps(load_structure_data(),ensure_ascii=False,allow_nan=False).encode(),'application/json; charset=utf-8')
        if p=='/api/live': return self.sendb(json.dumps(live_snapshot(),ensure_ascii=False,allow_nan=False).encode(),'application/json; charset=utf-8')
        return self.sendb(b'Not Found','text/plain',404)
    def log_message(self,*args): pass

def main():
    print('Stock Structure Live Dashboard - Scoring')
    print('JSON:',STRUCTURE_JSON); print(f'URL : http://{HOST}:{PORT}')
    d=load_structure_data(); print('OK  :',len(d.get('symbols',{})),'structure symbols' if d.get('ok') else d.get('error'))
    if websocket is None: print('WARN: pip install websocket-client')
    if not FINNHUB_API_KEY: print('WARN: FINNHUB_API_KEY 未設定')
    syms=all_symbols(); threading.Thread(target=bootstrap_quotes,args=(syms,),daemon=True).start(); threading.Thread(target=websocket_loop,daemon=True).start()
    srv=ThreadingHTTPServer((HOST,PORT),DashboardHandler); threading.Timer(0.8,lambda:webbrowser.open(f'http://{HOST}:{PORT}')).start()
    print('Press Ctrl+C to stop.')
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally: srv.server_close()

if __name__=='__main__': main()
