"""docs/16 §3 교차 검증 — 11종목에 A3/A7/A8/A9 를 돌려 뒤집기 대조군과 비교. 실행: python scripts/ma_youtube_multi.py"""
import sys; sys.path.insert(0,'scripts')
import ma_youtube as M
import statistics as st
D='dataset/vision/'
syms=['SOXLUSDT','SOXSUSDT','TQQQUSDT','SQQQUSDT','NVDAUSDT','AMDUSDT','TSLAUSDT','QQQUSDT','MSTRUSDT','COINUSDT','PLTRUSDT']
class A: cost=0.11; funding=0.0172; funding_crypto=0.01
variants={'A3 전시간':lambda B:M.strat_9_20_pullback(B,"ema99",3.0,None),
          'A8 본장밖':lambda B:M.strat_9_20_pullback(B,"ema99",3.0,None,off_only=True),
          'A9 본장밖 ATR':lambda B:M.strat_9_20_pullback(B,"atr25",3.0,None,off_only=True),
          'A7 본장만':lambda B:M.strat_9_20_pullback(B,"ema99",3.0,None,rth_only=True)}
agg={k:[] for k in variants}; wins={k:0 for k in variants}
print(f"{'종목':<10}"+"".join(f"{k:>34}" for k in variants))
for s in syms:
    ts,o,h,l,c,v=M.load_1m(D+s+'_1m.csv',None); T,O,H,L,C,V=M.to_5m(ts,o,h,l,c,v); B=M.build(T,O,H,L,C,V)
    row=f"{s[:-4]:<10}"
    for k,fn in variants.items():
        sig=fn(B); tr=M.simulate(B,sig,0.11,0.0172); fl=M.simulate(B,sig,0.11,0.0172,flip=True)
        r=[t[1] for t in tr]; rf=[t[1] for t in fl]
        m=st.mean(r) if r else float('nan'); mf=st.mean(rf) if rf else float('nan')
        agg[k]+=r; wins[k]+= (m>mf)
        row+=f"  n={len(r):4d} {m:+.3f}R vs뒤집기 {mf:+.3f}R {'✓' if m>mf else '✗'}"
    print(row)
print()
for k in variants:
    r=agg[k]; se=st.stdev(r)/len(r)**.5
    print(f"{k:<14} 합계 n={len(r)} 기대 {st.mean(r):+.3f}R ±{se:.3f} t={st.mean(r)/se:+.2f}  뒤집기 이긴 종목 {wins[k]}/{len(syms)}")
