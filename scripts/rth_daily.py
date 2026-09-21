"""일간 10종목 분산·정규장 전용 신호 검증 (docs/14 §1 #8). 입력: dataset/vision/_rth/_all_rth.csv"""
import csv,collections,statistics as st,math,random
R=collections.defaultdict(dict)
for r in csv.DictReader(open('dataset/vision/_rth/_all_rth.csv')):
    if r['symbol'] in ('BTCUSDT','SPXUSDT','NXPCUSDT'): continue
    R[r['symbol']][r['date']]=dict(o=float(r['o']),h=float(r['h']),l=float(r['l']),
                                   c=float(r['c']),v=float(r['vol']),
                                   ec=float(r['ext_close']) if r['ext_close'] else None)
DATES=sorted({d for s in R for d in R[s]})
IDX={s:sorted(R[s]) for s in R}
POS={s:{d:i for i,d in enumerate(IDX[s])} for s in R}
COST=0.0011

def prev(s,d,n=1):
    i=POS[s].get(d)
    if i is None or i-n<0: return None
    return R[s][IDX[s][i-n]]

def sig(name,s,d):
    b=R[s][d]; p=prev(s,d)
    if p is None: return None
    if name=='overnight':   return b['o']/p['c']-1            # 밤사이 움직임
    if name=='prevday':     return p['c']/p['o']-1            # 전일 정규장 수익
    if name=='prev5':
        q=prev(s,d,5)
        return None if q is None else p['c']/q['c']-1
    if name=='closepos':                                       # 전일 종가의 봉 내 위치
        rng=p['h']-p['l']
        return None if rng<=0 else (p['c']-p['l'])/rng-0.5
    if name=='gap_norm':
        rng=(p['h']-p['l'])/p['c']
        return None if rng<=0 else (b['o']/p['c']-1)/rng
    if name=='volshock':
        q=[prev(s,d,k) for k in range(1,6)]
        if any(x is None for x in q): return None
        base=st.mean(x['v'] for x in q[1:])
        return None if base<=0 else (p['v']/base-1)*(1 if p['c']>p['o'] else -1)
    return None

def backtest(name, flip=False, k=10, lo=0, hi=10**9, mode='sig', seed=0, rand_dir=False):
    rng=random.Random(seed)
    wins=0; tot=0; rets=[]; daily=[]
    for i,d in enumerate(DATES):
        if not (lo<=i<hi): continue
        cands=[]
        for s in R:
            if d not in R[s]: continue
            v=sig(name,s,d)
            if v is None: continue
            cands.append((s,v))
        if len(cands)<k: continue
        if mode=='rand': rng.shuffle(cands)
        else: cands.sort(key=lambda x:-abs(x[1]))
        day=[]
        for s,v in cands[:k]:
            dirn = rng.choice((1,-1)) if rand_dir else (1 if v>0 else -1)
            if flip: dirn=-dirn
            b=R[s][d]
            gross=(b['c']/b['o']-1)*dirn
            net=gross-COST
            rets.append(net); day.append(net)
            wins += 1 if gross>0 else 0; tot+=1
        daily.append(st.mean(day))
    mu=st.mean(rets); sd=st.stdev(rets)
    dmu=st.mean(daily); dsd=st.stdev(daily)
    return dict(n=tot, hit=wins/tot, mu=mu, se=sd/math.sqrt(len(daily)),
                days=len(daily), dmu=dmu,
                sharpe=dmu/dsd*math.sqrt(252) if dsd>0 else float('nan'),
                eq10=math.prod(1+10*x for x in daily))
