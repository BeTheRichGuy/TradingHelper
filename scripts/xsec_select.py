"""크로스섹셔널 종목 선택 백테스트 (docs/14 §1 #7). 입력: dataset/vision/_daily_all.csv"""
import csv, collections, math, random, statistics

MMR  = 0.005
COST = 0.0011      # 왕복 0.11% (실측)
FUND = 0.000516    # 일 0.0516% (실측 SOXL 평균 0.0172%/8h)
EXCLUDE = {"BTCUSDT", "SPXUSDT", "NXPCUSDT"}   # 크립토 제외

def load(path):
    bars = collections.defaultdict(dict)
    for r in csv.DictReader(open(path)):
        s = r["symbol"]
        if s in EXCLUDE: continue
        bars[s][r["date"]] = (float(r["open"]), float(r["high"]),
                              float(r["low"]),  float(r["close"]))
    dates = sorted({d for s in bars for d in bars[s]})
    return bars, dates

def run(bars, dates, look, k, hold, lev, short=False, mode="mom",
        seed=0, start=0, min_univ=8):
    """wallet = 담보(실현). pos = sym -> (qty_signed, entry). equity = wallet + 미실현"""
    rng = random.Random(seed)
    wallet = 1.0
    pos = {}
    peak = 1.0; mdd = 0.0; liq = None
    eq = []

    def equity_at(day, px_of):
        e = wallet
        for s,(q,ent) in pos.items():
            p = px_of(s, day)
            if p is None: continue
            e += q*(p-ent)
        return e

    def notional_at(day, px_of):
        n = 0.0
        for s,(q,ent) in pos.items():
            p = px_of(s, day)
            if p is None: p = ent
            n += abs(q)*p
        return n

    for i in range(start, len(dates)):
        day = dates[i]
        # --- 리밸런스 ---
        if (i-start) % hold == 0:
            univ=[]
            for s,b in bars.items():
                if day not in b: continue
                j=i-look
                if j<0 or dates[j] not in b: continue
                univ.append((s, b[day][3]/b[dates[j]][3]-1.0))
            if len(univ) >= min_univ:
                # 기존 포지션 실현
                for s,(q,ent) in pos.items():
                    if day in bars[s]:
                        wallet += q*(bars[s][day][3]-ent)
                        wallet -= abs(q)*bars[s][day][3]*COST/2
                pos={}
                if wallet <= 0: liq=day; break
                if   mode=="mom": univ.sort(key=lambda x:-x[1])
                elif mode=="rev": univ.sort(key=lambda x: x[1])
                elif mode=="rand": rng.shuffle(univ)
                longs=[s for s,_ in univ[:k]]
                shorts=[s for s,_ in univ[-k:]] if short else []
                nsym=len(longs)+len(shorts)
                each = lev*wallet/nsym
                for s in longs:
                    p=bars[s][day][3]; pos[s]=( each/p, p); wallet -= each*COST/2
                for s in shorts:
                    p=bars[s][day][3]; pos[s]=(-each/p, p); wallet -= each*COST/2
        # --- 다음날 일중 청산 체크 ---
        if i+1 >= len(dates): break
        nd = dates[i+1]
        adverse_eq = wallet; notl = 0.0
        for s,(q,ent) in pos.items():
            if nd not in bars[s]: continue
            o,h,l,c = bars[s][nd]
            adv = l if q>0 else h
            adverse_eq += q*(adv-ent)
            notl += abs(q)*adv
        if notl>0 and adverse_eq <= notl*MMR:
            liq=nd; eq.append((nd,0.0)); break
        # 펀딩
        for s,(q,ent) in pos.items():
            if nd in bars[s]:
                wallet -= abs(q)*bars[s][nd][3]*FUND
        cur = equity_at(nd, lambda s,d: bars[s][d][3] if d in bars[s] else None)
        eq.append((nd,cur))
        peak=max(peak,cur); mdd=max(mdd, 1-cur/peak) if peak>0 else mdd
        if cur<=0: liq=nd; break

    if liq: final=0.0
    else:
        last=dates[-1]
        for s,(q,ent) in pos.items():
            if last in bars[s]: wallet += q*(bars[s][last][3]-ent)
        final=max(0.0,wallet)
    rets=[]; prev=1.0
    for _,v in eq:
        if prev>0 and v>0: rets.append(v/prev-1)
        prev=v
    sh = (statistics.mean(rets)/statistics.stdev(rets)*math.sqrt(252)
          if len(rets)>5 and statistics.stdev(rets)>0 else float("nan"))
    return dict(final=final, liq=liq, mdd=mdd, sharpe=sh, days=len(eq))

def summ(tag,res):
    fin=[r["final"] for r in res]
    liqn=sum(1 for r in res if r["liq"])
    sh=[r["sharpe"] for r in res if r["sharpe"]==r["sharpe"]]
    print(f"{tag:<38} 중앙 {statistics.median(fin):7.3f}x  최고 {max(fin):8.2f}x"
          f"  청산 {liqn:2d}/{len(res):2d}  MDD {statistics.median(r['mdd'] for r in res)*100:5.1f}%"
          f"  샤프 {statistics.median(sh) if sh else float('nan'):6.2f}")
