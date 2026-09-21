"""화면 코드를 **실제로 실행해서** 결과를 확인한다.

지금까지는 HTML 을 문자열로만 검사했다. 그래서 "함수 이름이 있는가"는 알아도
"그 함수가 제대로 그리는가"는 몰랐다. 여기서는 최소 DOM 을 흉내내고
node 로 스크립트를 돌려 실제 출력물을 본다.

node 가 없으면 건너뛴다 (파이썬만으로는 못 하는 검사라 어쩔 수 없다).
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

HTML = (pathlib.Path(__file__).resolve().parent.parent
        / "src" / "th" / "api" / "web" / "index.html")

HARNESS = r"""
const els = {};
const mk = (id) => ({
  id, textContent:"", innerHTML:"", className:"", title:"", style:{}, hidden:false, value:"",
  classList:{ _s:new Set(), add(...a){a.forEach(x=>this._s.add(x))},
              remove(...a){a.forEach(x=>this._s.delete(x))} },
  offsetWidth:1, clientWidth:800, clientHeight:300,
  getContext(){ return new Proxy({}, {get:()=>()=>{}}); },
  getBoundingClientRect(){ return {left:0, top:0, width:800, height:300}; },
  setAttribute(){}, removeAttribute(){}, dataset:{},
  _h:{}, addEventListener(t, f){ (this._h[t] ||= []).push(f); },
  setPointerCapture(){}, releasePointerCapture(){},
});
global.document = { getElementById:(id)=>(els[id] ||= mk(id)),
                    querySelectorAll:()=>[], documentElement:{}, addEventListener(){} };
global.window = { devicePixelRatio:1, addEventListener(){} };
global.getComputedStyle = () => ({ getPropertyValue: () => "#fff" });
global.matchMedia = () => ({ addEventListener(){} });
global.location = { protocol:"http:", host:"x" };
global.WebSocket = function(){ global.__ws = this; };   // 마지막으로 만든 소켓을 잡아 둔다
global.__fetches = [];
global.fetch = async (url, opt) => { global.__fetches.push([url, opt]);
  return { ok:true, status:200, json: async () => ({bars:[],items:[],events:[]}) }; };
global.setInterval = () => 0; global.setTimeout = () => 0; global.clearInterval = () => {};

const fs = require("fs");
const M = {};
new Function("M", fs.readFileSync(process.argv[2], "utf8") +
  ";M.S=S;M.everySecond=everySecond;M.paintRisk=paintRisk;" +
  "M.paintReasons=paintReasons;M.paintAlerts=paintAlerts;M.paintDaily=paintDaily;" +
  "M.paintDiscipline=paintDiscipline;M.paintBrackets=paintBrackets;M.paintBracketR=paintBracketR;" +
  "M.chartWheel=chartWheel;M.chartDown=chartDown;M.chartMove=chartMove;M.chartUp=chartUp;" +
  "M.goLatest=goLatest;M.draw=draw;M.allBars=allBars;M.viewStart=viewStart;M.chartLeave=chartLeave;" +
  "M.loadChart=loadChart;M.loadFoot=loadFoot;M.serverLooksOld=serverLooksOld;M.paintSym=paintSym;" +
  "M.paintPositions=paintPositions;M.paintPositionsLive=paintPositionsLive;M.setBrkTab=setBrkTab;" +
  "M.setOrderTab=setOrderTab;M.orderPreview=orderPreview;M.submitOrder=submitOrder;M.setDir=setDir;" +
  "M.quickStop=quickStop;M.quickTp=quickTp;M.setWatchGroup=setWatchGroup;M.O=O;")(M);
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r)); };

(async () => {
const { S } = M;
const out = {};

// ① 데이터가 하나도 없을 때 죽지 않는가
try { M.everySecond(); out.empty_ok = true; } catch (e) { out.empty_ok = String(e); }

// ② 정상 상태
S.connected = true; S.fresh = true;
S.price = 126.32; S.prevPrice = 126.10; S.quoteAt = Date.now();
S.snap = {
  symbol:"SOXLUSDT",
  session:{regime:"RTH", regime_label_ko:"본장", blocked:false, regime_ends_in_sec:3600, warnings:[]},
  risk:{has_position:true, side:"LONG", qty:41.6667, entry_price:126.00, stop_price:124.80,
        has_stop:true, liq_distance_pct:8.4, underlying_move_to_liq_pct:2.8,
        account_leverage:0.53, daily_r:-0.4, daily_locked:false, errors:[], stale:false},
  no_trade:{reasons:[{source:"session",code:"x",text_ko:"본주 휴장 구간"}], note_ko:""},
  alerts:[{level:"critical", message_ko:"손절 주문이 없습니다", detail_ko:"지금 거세요"}],
};
S.snapAt = Date.now();
M.everySecond(); M.paintAlerts(); M.paintReasons();
out.price   = els.px.textContent;
// positions 메시지가 오기 전에는 스냅샷 risk 가 포지션 표의 한 줄로 그려진다 (paintRisk 대체 경로)
const pb0 = els.posbody.innerHTML;
out.pnl     = (pb0.match(/id="pc_pnl_SOXLUSDT">([^<]*)</) || [])[1];
out.liq     = (pb0.match(/id="pc_liqd_SOXLUSDT">([^<]*)</) || [])[1];
out.fallback_rows = (pb0.match(/class="prow/g) || []).length;
out.alert   = els.alert.innerHTML.includes("손절 주문이 없습니다");
out.reason  = els.rs.innerHTML.includes("본주 휴장");
out.daily   = els.daily.innerHTML.includes("0.40R") || els.daily.innerHTML.includes("-0.40R");

// ③ 계좌 조회 실패 — '포지션 없음' 으로 그리면 안 된다
S.snap.risk.errors = ["계정 조회 실패: ReadTimeout"];
M.paintRisk();
out.fail_keeps_last = els.posnote.innerHTML.includes("마지막으로 확인된 값") && els.posbody.className.includes("stale");
out.fail_says_none  = els.posbody.innerHTML.includes("열린 포지션이 없습니다");
out.fail_row_kept   = (els.posbody.innerHTML.match(/class="prow/g) || []).length === 1;

// ④ 빈 사유 목록이 초록불로 보이면 안 된다
S.snap.no_trade = {reasons:[], note_ko:"지금 막을 이유는 찾지 못했습니다"};
M.paintReasons();
out.empty_reason_html = els.rs.innerHTML;

// ⑤ 손절이 없으면 빨갛게
S.snap.risk.errors = []; S.snap.risk.has_stop = false;
M.paintRisk();
out.no_stop_marked = /class="down" id="pc_stop_SOXLUSDT">없음</.test(els.posbody.innerHTML);

// ⑥ 끝나는 시각이 없는 레짐으로 바뀌면 카운트다운을 지운다
const ws = global.__ws;
const snap = (session) => ({ data: JSON.stringify({
  type:"snapshot", symbol:"SOXLUSDT", session,
  no_trade:{reasons:[], note_ko:""}, alerts:[],
}) });
ws.onmessage(snap({regime:"RTH", regime_label_ko:"본장", blocked:false, regime_ends_in_sec:3600, warnings:[]}));
out.ends_set = S.sessionEndsAt != null;
ws.onmessage(snap({regime:"CLOSED", regime_label_ko:"휴장", blocked:true, regime_ends_in_sec:null, warnings:[]}));
out.ends_cleared = S.sessionEndsAt === null;

// ⑦ 살아있는 봉 — 호가가 올 때마다 마지막 캔들의 고가·저가·종가가 움직여야 한다
S.tf = 5; S.live = null;
const t0 = Math.floor(Date.now()/300000)*300000;          // 현재 5분 묶음 시작
S.bars = [{t:new Date(t0-300000).toISOString(), o:100, h:101, l:99, c:100.5,
           ema9:100.4, ema21:100.2, ema99:100.0, vwap:100.3}];
const tick = (price, ms) => ws.onmessage({data: JSON.stringify(
  {type:"tick", fresh:true, price, bid:price-0.01, ask:price+0.01, spread_bps:1, ts:new Date(ms).toISOString()})});
tick(100.7, t0+1000);
out.live_created = !!S.live && S.live.o === 100.5 && S.live.c === 100.7;
tick(101.2, t0+2000); tick(100.1, t0+3000);
out.live_hl = S.live && S.live.h === 101.2 && S.live.l === 100.1 && S.live.c === 100.1;
out.live_count_before = S.bars.length;
tick(100.3, t0+300000+1000);                              // 다음 5분 묶음 — 새 봉 시작
out.live_rolled = S.bars.length === 2 && S.bars[1].provisional === true && S.live.o === 100.1 && S.live.c === 100.3;
tick(100.0, t0-1000);                                     // 닫힌 구간의 늦은 호가 — 무시
out.stale_ignored = S.live.c === 100.3;

// ⑧ 워치리스트 — 36 종목이 줄로 그려지고 포커스 종목이 강조된다
const SYMS = ["SOXL","SOXS","TQQQ","SQQQ","NVDA","AMD","TSLA","MSTR","COIN","PLTR","SMCI","INTC",
  "SMH","QQQ","SPY","AAPL","MSFT","GOOGL","AMZN","META","NFLX","GME","UVXY","TZA","SNDK","UBER",
  "WMT","LLY","IBM","HPE","COST","EBAY","DKNG","PANW","GDX","SHOP"];
const LEV = {SOXL:"3x", SOXS:"-3x", TQQQ:"3x", SQQQ:"-3x", TZA:"-3x", UVXY:"1.5x"};
ws.onmessage({data: JSON.stringify({type:"watchlist", ts:"x", focus:"SOXLUSDT",
  items: SYMS.map((s,i) => ({symbol:s+"USDT", price:100+i, change_pct:(i%5)-2+0.1, quote_volume:1e6,
                             lev_tag:LEV[s]||null, collected:i<3})),
  basket_exposure_x:1.9})});
const wl = els.wl.innerHTML;
out.watch_rows = (wl.match(/class="wr/g) || []).length;
out.watch_sel = /class="wr sel" data-sym="SOXLUSDT"/.test(wl);
out.watch_lev = wl.includes('>3x<') && wl.includes('>-3x<');
out.watch_strip = wl.includes('>SOXL<') && !wl.includes('>SOXLUSDT<');
out.watch_nc = (wl.match(/class="nc"/g) || []).length;   // 수집 안 하는 종목 표시 = 36 - 3
out.watch_basket = els.wbasket.textContent;
out.watch_title = els.wtitle.textContent;
// 검색은 클라이언트에서
els.wq.value = "SOX"; els.wq._h.input[0]();
out.watch_search = (els.wl.innerHTML.match(/class="wr/g) || []).length;
els.wq.value = ""; els.wq._h.input[0]();

// ⑨ 규율 지표 — 게이지 색·값, ok:false 면 마지막 값 유지 + 흐리게
const snapD = (discipline) => ({ data: JSON.stringify({ type:"snapshot", symbol:"SOXLUSDT",
  session:{regime:"RTH", regime_label_ko:"본장", blocked:false, regime_ends_in_sec:3600, warnings:[]},
  no_trade:{reasons:[], note_ko:""}, alerts:[], discipline }) });
ws.onmessage(snapD({total_exposure_x:2.1, total_limit_x:3.0, max_symbol:"SOXLUSDT",
  max_symbol_exposure_x:0.9, symbol_limit_x:1.0, daily_pnl_pct:-1.8, drawdown_limit_pct:15.0,
  remaining_r:2.6, r_limit:3.0, event_blocked:true, event_name:"FOMC 금리 결정", event_in_sec:2480,
  closes_in_sec:15160, ok:true}));
M.paintDiscipline();
out.disc_exp = els.g_exp.innerHTML; out.disc_exp_w = els.gb_exp.style.width; out.disc_exp_c = els.gb_exp.style.background;
out.disc_sym_c = els.gb_sym.style.background; out.disc_sym_cls = els.g_sym.className;
out.disc_dd = els.g_dd.innerHTML; out.disc_r = els.g_r.innerHTML;
out.disc_ev = els.c_evt.textContent; out.disc_ev_on = els.c_ev.className;
out.disc_close = els.c_closet.textContent;
out.disc_ok_cls = els.disc.className;
ws.onmessage(snapD({total_exposure_x:null, total_limit_x:3.0, max_symbol:null, max_symbol_exposure_x:null,
  symbol_limit_x:1.0, daily_pnl_pct:null, drawdown_limit_pct:15.0, remaining_r:null, r_limit:3.0,
  event_blocked:false, event_name:null, event_in_sec:null, closes_in_sec:null, ok:false}));
M.paintDiscipline();
out.disc_fail_keeps = els.g_exp.innerHTML === out.disc_exp && els.g_r.innerHTML === out.disc_r;
out.disc_fail_cls = els.disc.className;
out.disc_fail_note = els.discnote.textContent;

// ⑩ 브래킷 — 줄이 그려지고 R 은 브라우저가 매초 가격으로 센다
S.price = 131.80;                                          // 포커스 SOXLUSDT 의 현재가
ws.onmessage({data: JSON.stringify({type:"brackets", ts:"x", mode:"paper", available:true,
  open:[{id:"a1", symbol:"SOXLUSDT", direction:"LONG", entry:132.50, stop:131.25, tp1:134.09, tp2:135.68,
         qty:41.67, position_qty:20.8, has_stop:true, status_ko:"손절 대기 · 잔여 50%", realized_r:0,
         risk_per_unit:1.25, close_requested:false},
        {id:"b2", symbol:"NVDAUSDT", direction:"SHORT", entry:189.40, stop:190.60, tp1:187.10, tp2:185.20,
         qty:10, position_qty:10, has_stop:true, status_ko:"손절 대기", realized_r:0,
         risk_per_unit:1.20, close_requested:true},
        {id:"c3", symbol:"TQQQUSDT", direction:"LONG", entry:91.20, stop:90.40, tp1:92.80, tp2:null,
         qty:10, position_qty:0, has_stop:false, status_ko:"진입 대기 · 지정가 · 4:12 후 만료", realized_r:0,
         risk_per_unit:0.8, close_requested:false}],
  today_realized_r:1.2, today_count:3, expectancy_r:0.18})});
const bh = els.brkbody.innerHTML;
out.brk_rows = (bh.match(/class="brow"/g) || []).length;
out.brk_title = els.brktitle.textContent;
out.brk_stat = els.brkstat.innerHTML;
out.brk_mode = els.brkmode.textContent;
// SOXL LONG: (131.80 − 132.50) / 1.25 = −0.56R
out.brk_r_long = (bh.match(/id="br_a1">([^<]*)</) || [])[1];
// NVDA SHORT 는 워치리스트 가격(NVDA 는 5번째 → 104): (189.40 − 104) / 1.20 = +71.17R
out.brk_r_short = (bh.match(/id="br_b2">([^<]*)</) || [])[1];
out.brk_r_pending = (bh.match(/id="br_c3">([^<]*)</) || [])[1];   // 진입 전 → —
out.brk_disabled = /data-id="b2" disabled/.test(bh);
out.brk_enabled = /data-id="a1"(?! disabled)/.test(bh);
// 매초 갱신: 가격이 바뀌면 R 셀만 다시 쓴다
S.price = 133.75; M.paintBracketR();
out.brk_r_tick = els.br_a1.textContent;                    // (133.75 − 132.50)/1.25 = +1.00R
// 청산 버튼 → POST, 403 이면 안내
global.fetch = async (url, opt) => { global.__fetches.push([url, opt]);
  return { ok:false, status:403, json: async () => ({}) }; };
els.brkbody._h.click[0]({target:{closest:(sel)=>({dataset:{id:"a1"}, disabled:false})}});
await new Promise(r => setImmediate(r));
out.brk_close_url = (global.__fetches.find(f => /brackets\/a1\/close/.test(f[0])) || [])[0];
out.brk_close_403 = els.brkmsg.textContent;

// ⑪ 팬/줌 — 휠은 봉 수를 줄이고, 드래그는 따라가기를 끄고, 최신은 되돌린다
S.bars = Array.from({length:300}, (_, i) => ({t:new Date(t0 - (300-i)*300000).toISOString(),
  o:100, h:101, l:99, c:100.5, ema9:100, ema21:100, ema99:100, vwap:100}));
S.view = {start:0, count:120, follow:true};
M.draw();
out.view_follow_start = S.view.start;                      // 301 봉(live 포함) − 120 + 8 = 189
out.latest_hidden = els.btnlatest.hidden;
M.chartWheel({deltaY:-100, clientX:400, clientY:100, preventDefault(){}});
out.view_zoom_in = S.view.count;
M.chartWheel({deltaY:100, clientX:400, clientY:100, preventDefault(){}});
out.view_zoom_out = S.view.count;
for (let i=0;i<60;i++) M.chartWheel({deltaY:-100, clientX:400, clientY:100, preventDefault(){}});
out.view_min = S.view.count;
for (let i=0;i<80;i++) M.chartWheel({deltaY:100, clientX:400, clientY:100, preventDefault(){}});
out.view_max = S.view.count;
S.view = {start:0, count:120, follow:true}; M.draw();
M.chartDown({clientX:400, clientY:100, pointerId:1});
out.drag_follow = S.view.follow;
M.chartMove({clientX:600, clientY:100});                   // 오른쪽으로 200px = 과거로 팬
out.drag_start = S.view.start;
M.chartUp({});
out.latest_shown = els.btnlatest.hidden === false;
M.goLatest();
out.latest_follow = S.view.follow;
out.latest_start = M.viewStart(M.allBars().length);
// 왼쪽 끝에 닿으면 더 받아온다
global.fetch = async (url, opt) => { global.__fetches.push([url, opt]);
  return { ok:true, status:200, json: async () => ({bars:[]}) }; };
M.chartDown({clientX:400, clientY:100, pointerId:1});
M.chartMove({clientX:2400, clientY:100});
out.more_limit = S.limit;
out.more_url = (global.__fetches.filter(f => /api\/candles/.test(f[0])).pop() || [])[0];
M.chartUp({});
// 가격축 — 멀리 있는 TP2/SL 은 축 범위를 늘리지 않고 꼬리표로만 알린다
S.price = 100.5; S.live = null; S.view = {start:0, count:120, follow:true};
S.brk = {open:[]}; S.snap = Object.assign({}, S.snap, {risk:{has_position:false}}); M.draw();
const fit0 = {...S.fit};                                    // 봉만: 99~101 근처
S.brk = {open:[{id:"z", symbol:"SOXLUSDT", direction:"LONG", entry:100.2, stop:99.6, tp1:101.5, tp2:140.0,
                qty:1, position_qty:1, risk_per_unit:0.6, status_ko:"x"}]};
M.draw();
out.fit_near = {lo:fit0.lo, hi:fit0.hi};
out.fit_far = {lo:S.fit.lo, hi:S.fit.hi, tags:S.fit.tags};
out.fit_unchanged = Math.abs(S.fit.hi - fit0.hi) < 1.5 && Math.abs(S.fit.lo - fit0.lo) < 1.5;
S.price = 200; M.draw();                                    // 현재가는 항상 축 안에
out.fit_price_in = S.fit.hi >= 200 && S.fit.lo <= 99;
S.price = 100.5;
// 크로스헤어 — 포인터가 차트 위에 있으면 축 꼬리표 값(가격·시각)이 잡히고, 나가면 지워진다
M.chartMove({clientX:400, clientY:100});
out.cross_set = !!(S.cross && typeof S.cross.price === "number" && typeof S.cross.t === "string");
out.cross_price_in_fit = !!S.cross && S.cross.price <= S.fit.hi && S.cross.price >= S.fit.lo;
M.chartLeave();
out.cross_cleared = S.cross === null;
// 봉이 하나도 없어도 죽지 않는다
S.bars = []; S.live = null; S.view = {start:0, count:120, follow:false};
try { M.draw(); M.chartWheel({deltaY:-100, clientX:400, clientY:100, preventDefault(){}});
      M.chartDown({clientX:1, clientY:1}); M.chartMove({clientX:2, clientY:1}); M.chartUp({}); out.empty_chart_ok = true; }
catch (e) { out.empty_chart_ok = String(e); }

// ⑫ 서버(DB)가 뒤처져도 살아있는 봉이 하나로 뭉개지면 안 된다 (실제로 겪은 버그: 90분 멈춤)
S.tf = 1; S.live = null; S.symbol = "SOXLUSDT"; S.view = {start:0, count:120, follow:true};
const m0 = Math.floor(Date.now()/60000)*60000 - 90*60000;         // 서버 마지막 봉 = 90분 전
const stale = [1,2,3].map(i => ({t:new Date(m0-(3-i)*60000).toISOString(), o:100, h:101, l:99, c:100.5,
                                 ema9:100, ema21:100, ema99:100, vwap:100}));
S.bars = stale.slice();
global.fetch = async (url, opt) => { global.__fetches.push([url, opt]);
  return { ok:true, status:200, json: async () => ({bars:stale, tf:1, symbol:"SOXLUSDT"}) }; };
tick(100.7, m0+60000+1000);                                        // T+1 — 살아있는 봉 시작
tick(100.9, m0+2*60000+1000);                                      // T+2 — T+1 임시로 닫힘
tick(100.2, m0+3*60000+1000);                                      // T+3
tick(100.4, m0+4*60000+1000);                                      // T+4
out.merge_prov_before = S.bars.filter(b => b.provisional).length;  // 3
const liveBefore = JSON.stringify(S.live);
await M.loadChart();                                               // 같은(뒤처진) 서버 봉이 또 온다
out.merge_bars_after = S.bars.length;                              // 3 서버 + 3 임시 = 6
out.merge_prov_after = S.bars.filter(b => b.provisional).length;
out.merge_live_kept = JSON.stringify(S.live) === liveBefore;
out.merge_order_ok = S.bars.every((b, i) => i === 0 || Date.parse(b.t) > Date.parse(S.bars[i-1].t));
// 서버가 따라잡으면 임시 봉은 서버 값으로 바뀐다
const caught = stale.concat([1,2,3].map(i => ({t:new Date(m0+i*60000).toISOString(), o:100, h:102, l:98, c:101,
                                              ema9:100, ema21:100, ema99:100, vwap:100})));
global.fetch = async () => ({ ok:true, status:200, json: async () => ({bars:caught, tf:1, symbol:"SOXLUSDT"}) });
await M.loadChart();
out.merge_replaced = S.bars.length === 6 && S.bars.filter(b => b.provisional).length === 0 && S.bars[5].h === 102;
out.merge_live_still = !!S.live && S.live.c === 100.4;

// ⑬ 옛 서버 감지 — health 에 ui:3 이 없거나, 15초가 지나도 watchlist 가 없으면 알린다
global.fetch = async (url) => ({ ok:true, status:200, json: async () =>
  /health/.test(url) ? {ok:true, symbol:"SOXLUSDT", mode:"paper"} : {items:[], events:[]} });
await M.loadFoot();
out.old_notice = els.wold.hidden === false ? els.wold.textContent : null;
M.everySecond();
out.old_foot = els.fconn.innerHTML.includes("옛 버전");
global.fetch = async (url) => ({ ok:true, status:200, json: async () =>
  /health/.test(url) ? {ok:true, symbol:"SOXLUSDT", mode:"paper", ui:3} : {items:[], events:[]} });
await M.loadFoot();
out.new_notice_hidden = els.wold.hidden === true;
const savedWatch = S.watch; S.watch = null; S.serverOld = null; S.connected = true;
S.connectedAt = Date.now() - 20000;
out.old_by_timeout = M.serverLooksOld();
S.connectedAt = Date.now() - 5000;
out.new_by_timeout = M.serverLooksOld();
S.watch = savedWatch;

// ═══ v2.1 (docs/15 §6-9) ═══════════════════════════════════
// ⑭ 워치리스트 그룹 칩 — item.group 으로 거르고, 머리글 수는 필터를 반영한다
S.symbol = "SOXLUSDT";
const GROUP_OF = (i) => i < 12 ? "반도체" : i < 20 ? "지수ETF" : i < 30 ? "대형기술주" : "기타";
ws.onmessage({data: JSON.stringify({type:"watchlist", ts:"x", focus:"SOXLUSDT",
  items: SYMS.map((s,i) => ({symbol:s+"USDT", price:100+i, change_pct:(i%5)-2+0.1, quote_volume:1e6,
                             lev_tag:LEV[s]||null, collected:i<3, group:GROUP_OF(i)})),
  basket_exposure_x:1.9})});
out.grp_shown = els.wgroups.hidden === false;
out.grp_chips = (els.wgroups.innerHTML.match(/class="gc/g) || []).length;
out.grp_all_title = els.wtitle.textContent;
els.wgroups._h.click[0]({target:{closest:()=>({dataset:{g:"반도체"}})}});
out.grp_semi_rows = (els.wl.innerHTML.match(/class="wr/g) || []).length;
out.grp_semi_title = els.wtitle.textContent;
out.grp_semi_on = /class="gc on" data-g="반도체"/.test(els.wgroups.innerHTML);
els.wq.value = "SOX"; els.wq._h.input[0]();                 // 검색은 그룹 안에서
out.grp_semi_search = (els.wl.innerHTML.match(/class="wr/g) || []).length;
els.wq.value = ""; els.wq._h.input[0]();
M.setWatchGroup("기타");
out.grp_etc_rows = (els.wl.innerHTML.match(/class="wr/g) || []).length;
M.setWatchGroup("전체");
out.grp_back_rows = (els.wl.innerHTML.match(/class="wr/g) || []).length;
// 그룹이 없는 옛 서버 메시지면 칩을 숨긴다
ws.onmessage({data: JSON.stringify({type:"watchlist", ts:"x", focus:"SOXLUSDT",
  items: SYMS.slice(0,3).map((s,i) => ({symbol:s+"USDT", price:100+i, change_pct:0, quote_volume:1, lev_tag:null, collected:true})),
  basket_exposure_x:null})});
out.grp_hidden_old = els.wgroups.hidden === true;
ws.onmessage({data: JSON.stringify({type:"watchlist", ts:"x", focus:"SOXLUSDT",
  items: SYMS.map((s,i) => ({symbol:s+"USDT", price:100+i, change_pct:0, quote_volume:1, lev_tag:LEV[s]||null,
                             collected:i<3, group:GROUP_OF(i)})), basket_exposure_x:1.9})});

// ⑮ 실매매 포지션 탭 — 종목별 한 줄, 평가손익은 브라우저가 매초 (포커스 = S.price, 나머지 = 워치리스트 가격)
S.price = 117.21;
const posMsg = (ok, items, wallet) => ({data: JSON.stringify({type:"positions", ts:"x", ok, wallet_usdt:wallet, items})});
ws.onmessage(posMsg(true, [
  {symbol:"SOXLUSDT", side:"LONG", qty:12.61, entry_price:125.62, mark_price:117.0, notional_usdt:1478.0,
   unrealized_pnl:-106.0, unrealized_pct:-6.7, liquidation_price:109.88, liq_distance_pct:6.22,
   underlying_move_to_liq_pct:-2.1, leverage_eff:0.51, has_stop:false, stop_price:null, stop_distance_pct:null},
  {symbol:"NVDAUSDT", side:"SHORT", qty:10, entry_price:110, mark_price:100, notional_usdt:1000,
   unrealized_pnl:100, unrealized_pct:9.09, liquidation_price:130, liq_distance_pct:25,
   underlying_move_to_liq_pct:null, leverage_eff:0.35, has_stop:true, stop_price:112, stop_distance_pct:7.7},
], 2890.5));
const pb = els.posbody.innerHTML;
out.pos_rows = (pb.match(/class="prow/g) || []).length;
out.pos_title = els.postitle.textContent;
out.pos_wallet = els.pwallet.textContent;
out.pos_notional = els.pnot.textContent;                    // 117.21×12.61 + 104×10 = 2,518
out.pos_long_pnl = (pb.match(/id="pc_pnl_SOXLUSDT">([^<]*)</) || [])[1];   // (117.21−125.62)×12.61
out.pos_short_pnl = (pb.match(/id="pc_pnl_NVDAUSDT">([^<]*)</) || [])[1];  // NVDA 워치리스트 104: (110−104)×10
out.pos_long_liq = (pb.match(/id="pc_liqd_SOXLUSDT">([^<]*)</) || [])[1];  // (117.21−109.88)/117.21
out.pos_short_liq = (pb.match(/id="pc_liqd_NVDAUSDT">([^<]*)</) || [])[1]; // (130−104)/104
out.pos_no_stop = /class="down" id="pc_stop_SOXLUSDT">없음</.test(pb);
out.pos_stop = (pb.match(/id="pc_stop_NVDAUSDT">([^<]*)</) || [])[1];      // 112.00 · |104−112|/104
out.pos_sel = /class="prow sel" data-sym="SOXLUSDT"/.test(pb);
out.pos_lev = pb.includes("0.51배") && pb.includes("0.35배");
S.price = 120; M.paintPositionsLive();                      // 매초: 셀만 다시 쓴다
out.pos_tick_pnl = els.pc_pnl_SOXLUSDT.textContent;         // (120−125.62)×12.61 = −70.87
out.pos_tick_cls = els.pc_pnl_SOXLUSDT.className;
out.pos_tick_px = els.pc_px_SOXLUSDT.textContent;
// 조회 실패 → 마지막 값 유지 + 흐리게
ws.onmessage(posMsg(false, [], null));
out.pos_fail_rows = (els.posbody.innerHTML.match(/class="prow/g) || []).length;
out.pos_fail_stale = els.posbody.className.includes("stale");
out.pos_fail_note = els.posnote.innerHTML.includes("마지막으로 확인된 값");
out.pos_fail_title = els.postitle.textContent;
// 정상 복귀 · 빈 목록
ws.onmessage(posMsg(true, [], 2890.5));
out.pos_empty = els.posbody.innerHTML.includes("열린 포지션이 없습니다") && !els.posbody.className.includes("stale");
// 탭 전환
M.setBrkTab("pos");
out.tab_pos = els.posview.hidden === false && els.brkview.hidden === true && els.tab_pos.className === "tab on"
  && els.posstat.hidden === false && els.brkstat.hidden === true;
M.setBrkTab("brk");
out.tab_brk = els.brkview.hidden === false && els.posview.hidden === true && els.tab_brk.className === "tab on";

// ⑯ 주문 패널 — 미리보기 POST, 주문 넣기, 403/503, 실매매 잠김
S.symbol = "SOXLUSDT"; S.price = 100; M.paintSym();
const savedTO = global.setTimeout; global.setTimeout = (f) => { f(); return 1; };   // 디바운스를 건너뛴다
const PREVIEW_OK = {ok:true, symbol:"SOXLUSDT", direction:"LONG", ref_price:100, qty:41.67, notional_usdt:4167,
  risk_usdt:14.45, r_unit_usdt:14.45, stop_pct:0.99, cost_r:0.12, equity_usdt:2890, warnings:["비용/R 주의"], reasons:[]};
let previewResp = PREVIEW_OK, orderResp = {status:200, body:{ok:true, bracket_id:"a1b2c3d4"}}, gateResp = null;
global.fetch = async (url, opt) => { global.__fetches.push([url, opt]);
  if (url === "/api/orders/preview") return {ok:true, status:200, json: async () => previewResp};
  if (url === "/api/orders") return {ok: orderResp.status < 400, status: orderResp.status, json: async () => orderResp.body};
  if (url === "/api/orders/gate") return {ok:true, status:200, json: async () => gateResp};
  return {ok:true, status:200, json: async () => ({bars:[], items:[], events:[]})}; };
out.order_sym = els.o_sym.textContent;
out.order_lev = els.o_lev.textContent;
out.order_risk_default = els.o_risk.value;
out.submit_off_empty = els.o_submit.disabled === true;       // 손절이 비면 잠김
global.__fetches = [];
els.o_stop.value = "99"; els.o_stop._h.input[0](); await flush();
const pv = global.__fetches.find(f => f[0] === "/api/orders/preview");
out.prev_method = pv && pv[1].method;
out.prev_body = pv && JSON.parse(pv[1].body);
out.prev_html = els.o_prev.innerHTML;
out.prev_warn = els.o_warn.innerHTML;
out.prev_reasons = els.o_reasons.innerHTML;
out.submit_on = els.o_submit.disabled === false;
// 빠른 채우기 — 기준가 100 · 손절 99 → 1R = 1 → TP1 101.5 · TP2 103
M.quickTp("o_tp1", 1.5); M.quickTp("o_tp2", 3); await flush();
out.quick_tp1 = els.o_tp1.value; out.quick_tp2 = els.o_tp2.value;
M.setDir("SHORT"); await flush();
out.short_sl_m_off = els.o_sl_m.disabled; out.short_sl_p_on = els.o_sl_p.disabled === false;
M.quickStop(+1); await flush();
out.quick_stop_short = els.o_stop.value;                    // 101
M.setDir("LONG"); M.quickStop(-1); await flush();
out.quick_stop_long = els.o_stop.value;                     // 99
out.long_sl_m_on = els.o_sl_m.disabled === false;
// 가드 사유가 있으면 주문 넣기가 잠긴다
previewResp = Object.assign({}, PREVIEW_OK, {reasons:["오늘 손실 한도 도달"]});
els.o_stop.value = "98"; els.o_stop._h.input[0](); await flush();
out.reason_html = els.o_reasons.innerHTML;
out.submit_blocked = els.o_submit.disabled === true;
// 주문 넣기 → POST /api/orders (expire_s 포함) → 성공 문구, 진입·손절·TP 비움, 방향·위험% 유지
previewResp = PREVIEW_OK;
els.o_entry.value = "100.5"; els.o_stop.value = "99"; els.o_tp1.value = "101.5"; els.o_tp2.value = "103";
els.o_stop._h.input[0](); await flush();
out.submit_ready = els.o_submit.disabled === false;
global.__fetches = [];
await M.submitOrder(); await flush();
const od = global.__fetches.find(f => f[0] === "/api/orders");
out.sub_body = od && JSON.parse(od[1].body);
out.sub_msg = els.o_msg.textContent; out.sub_cls = els.o_msg.className;
out.sub_cleared = [els.o_entry.value, els.o_stop.value, els.o_tp1.value, els.o_tp2.value].every(v => v === "");
out.sub_kept = M.O.dir === "LONG" && els.o_risk.value === "0.5" && els.o_exp.value === "300";
out.sub_reload = global.__fetches.some(f => f[0] === "/api/brackets");
out.sub_locked_after = els.o_submit.disabled === true;
// 403 — 실거래 경로 없음
orderResp = {status:403, body:{ok:false, reasons:["실거래 주문 경로는 아직 없습니다 (docs/07 게이트)"]}};
els.o_stop.value = "99"; els.o_stop._h.input[0](); await flush();
await M.submitOrder(); await flush();
out.sub_403 = els.o_msg.textContent; out.sub_403_cls = els.o_msg.className;
// 503 — 페이퍼 엔진 꺼짐
orderResp = {status:503, body:{ok:false, reasons:["페이퍼 엔진이 떠 있지 않습니다"]}};
await M.submitOrder(); await flush();
out.sub_503 = els.o_msg.textContent;
// 200 ok:false — 가드 거절: 사유를 빨갛게, 아무것도 열지 않는다
orderResp = {status:200, body:{ok:false, reasons:["세션 차단: 이벤트 30분 전", "열린 브래킷 3개"]}};
await M.submitOrder(); await flush();
out.sub_reject_reasons = els.o_reasons.innerHTML;
out.sub_reject_msg = els.o_msg.textContent;
out.sub_reject_locked = els.o_submit.disabled === true;
out.sub_reject_kept = els.o_stop.value === "99";
// 실매매 탭 — 잠김 화면 + 게이트 체크리스트
gateResp = {mode:"paper", live_allowed:false, conditions:[{text_ko:"페이퍼 30건 이상", met:true}, {text_ko:"기대값 양수", met:false}]};
global.__fetches = [];
M.setOrderTab("live"); await flush();
out.live_gate_url = (global.__fetches.find(f => f[0] === "/api/orders/gate") || [])[0];
out.live_locked = els.o_stop.disabled === true && els.o_long.disabled === true && els.o_form.className.includes("locked")
  && els.o_paperbits.hidden === true && els.o_lock.hidden === false && els.o_tab_live.className === "tab on";
out.live_gate = els.o_gate.innerHTML;
out.live_sum = els.o_gatesum.textContent;
out.live_submit_off = els.o_submit.disabled === true;
M.setOrderTab("paper"); await flush();
out.paper_back = els.o_stop.disabled === false && els.o_lock.hidden === true && els.o_paperbits.hidden === false;
// 종목이 바뀌면 다른 종목 가격으로 넣은 손절·목표는 버린다
els.o_stop.value = "99"; els.o_stop._h.input[0](); await flush();
global.__fetches = [];
els.posbody._h.click[0]({target:{closest:()=>({dataset:{sym:"NVDAUSDT"}})}}); await flush();
out.pos_click_focus = (global.__fetches.find(f => f[0] === "/api/focus") || [])[1];
out.focus_clears_form = S.symbol === "NVDAUSDT" && els.o_stop.value === "" && els.o_sym.textContent === "NVDAUSDT";
global.setTimeout = savedTO;

console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def _run_render() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 가 없어 화면 실행 검사를 건너뜁니다")
    js = re.search(r"<script>\n(.*?)\n</script>", HTML.read_text(encoding="utf-8"), re.S)
    assert js, "index.html 에서 스크립트를 찾지 못했다"
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "dash.js").write_text(js.group(1), encoding="utf-8")
    (d / "harness.js").write_text(HARNESS, encoding="utf-8")
    # Windows 는 기본 인코딩이 cp949 라 node 의 UTF-8 출력(한글)을 못 읽는다 → 명시
    p = subprocess.run([node, str(d / "harness.js"), str(d / "dash.js")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    assert p.returncode == 0, f"화면 코드 실행 실패:\n{p.stderr}"
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_화면_코드가_실제로_돈다():
    r = _run_render()
    assert r["empty_ok"] is True, f"데이터가 없을 때 죽는다: {r['empty_ok']}"


def test_가격과_평가손익이_제대로_계산된다():
    r = _run_render()
    assert r["price"] == "126.32", f"가격 표시가 이상하다: {r['price']}"
    # positions 메시지 전에는 스냅샷 risk 가 포지션 표 한 줄로: (126.32 − 126.00) × 41.6667 = +13.33 USDT
    assert r["fallback_rows"] == 1, "positions 메시지가 없으면 스냅샷 risk 로 한 줄을 그린다"
    assert r["pnl"] == "+13.33 USDT · +0.25%", f"평가손익이 틀렸다: {r['pnl']}"
    assert r["liq"] == "8.40%", f"청산 거리 표시가 이상하다: {r['liq']}"


def test_경고와_사유가_화면에_나온다():
    r = _run_render()
    assert r["alert"], "치명적 경고가 화면에 안 나온다"
    assert r["reason"], "매매 금지 사유가 화면에 안 나온다"


def test_계좌_조회_실패를_포지션_없음으로_그리지_않는다():
    """실제로 겪은 버그. 화면이 '없음'과 '못 가져옴'을 섞으면 안 된다."""
    r = _run_render()
    assert r["fail_keeps_last"], "실패 시 마지막 값을 유지한다고 알려야 한다 (흐리게 + 문구)"
    assert not r["fail_says_none"], "조회 실패인데 '포지션 없음' 이라고 그렸다"
    assert r["fail_row_kept"], "조회 실패여도 마지막 포지션 줄은 남는다"


def test_빈_사유_목록이_초록불로_보이지_않는다():
    r = _run_render()
    html = r["empty_reason_html"]
    assert "지금 막을 이유는" in html
    for bad in ("--up", "class=\"up", "green", "허가"):
        assert bad not in html, f"빈 사유가 초록불처럼 보인다: {bad}"


def test_손절이_없으면_화면에_표시된다():
    r = _run_render()
    assert r["no_stop_marked"], "손절 없는 상태가 화면에 안 보인다"


def test_끝나는_시각이_없는_레짐이_오면_카운트다운을_지운다():
    """본장(1시간 남음) 뒤에 주말 스냅샷이 오면, 이전 카운트다운이 '휴장' 옆에서
    계속 줄어들면 안 된다."""
    r = _run_render()
    assert r["ends_set"], "regime_ends_in_sec 가 있으면 카운트다운을 잡아야 한다"
    assert r["ends_cleared"], "regime_ends_in_sec 가 null 인데 이전 카운트다운이 남아 있다"


def test_호가가_올_때마다_살아있는_봉이_갱신된다():
    """가격은 매초 바뀌는데 캔들은 15초마다 서버 값만 받아 멈춰 있던 문제."""
    r = _run_render()
    assert r["live_created"] is True, "첫 호가에 살아있는 봉이 만들어져야 한다 (시가 = 직전 종가)"
    assert r["live_hl"] is True, "호가마다 고가·저가·종가가 갱신돼야 한다"
    assert r["live_rolled"] is True, "묶음이 바뀌면 이전 봉을 임시로 닫고 새 봉을 연다"
    assert r["stale_ignored"] is True, "이미 닫힌 구간의 늦은 호가는 무시한다"


# ── 대시보드 v2 (docs/15 계약) ─────────────────────────────
def test_워치리스트가_36줄로_그려지고_포커스가_강조된다():
    r = _run_render()
    assert r["watch_rows"] == 36, f"워치리스트 줄 수: {r['watch_rows']}"
    assert r["watch_sel"], "포커스 종목 줄이 강조되지 않았다"
    assert r["watch_strip"], "종목명에서 USDT 를 떼야 한다"
    assert r["watch_lev"], "레버리지 태그(3x·-3x)가 안 보인다"
    assert r["watch_nc"] == 33, f"수집 안 하는 종목 표시가 틀렸다: {r['watch_nc']}"
    assert r["watch_basket"] == "1.9x"
    assert r["watch_title"] == "워치리스트 · 36"
    assert r["watch_search"] == 2, f"검색 'SOX' 는 SOXL·SOXS 두 줄이어야 한다: {r['watch_search']}"


def test_규율_지표_게이지가_그려진다():
    r = _run_render()
    assert r["disc_exp"].startswith("2.1x") and "한도 3x" in r["disc_exp"]
    assert r["disc_exp_w"] == "70.0%"
    assert r["disc_exp_c"] == "var(--warn)", "70% 이상은 노랑"
    assert r["disc_sym_c"] == "var(--down)" and r["disc_sym_cls"] == "gv down", "90% 이상은 빨강"
    assert r["disc_dd"].startswith("-1.8%") and "한도 15%" in r["disc_dd"]
    assert r["disc_r"].startswith("2.6R") and "한도 3R" in r["disc_r"]
    assert r["disc_ev"] == "이벤트 차단 · 41분" and r["disc_ev_on"] == "sc on"
    assert r["disc_close"] == "휴장까지 4:12:40"
    assert r["disc_ok_cls"] == "panel"


def test_규율_지표_조회_실패면_마지막_값을_유지하고_흐리게():
    r = _run_render()
    assert r["disc_fail_keeps"], "ok:false 인데 게이지 값이 바뀌었다"
    assert "stale" in r["disc_fail_cls"], "ok:false 면 패널을 흐리게 해야 한다"
    assert r["disc_fail_note"] == "마지막으로 확인된 값"


def test_브래킷_줄과_R_이_브라우저에서_매초_계산된다():
    r = _run_render()
    assert r["brk_rows"] == 3
    assert r["brk_title"] == "페이퍼 브래킷 · 열림 3"
    assert "+1.20R" in r["brk_stat"] and "3건" in r["brk_stat"] and "+0.18R" in r["brk_stat"]
    assert r["brk_mode"].startswith("PAPER")
    # LONG: (131.80 − 132.50) / 1.25 = −0.56R · SHORT(워치리스트 가격 104): (189.40 − 104) / 1.20
    assert r["brk_r_long"] == "-0.56R", r["brk_r_long"]
    assert r["brk_r_short"] == "+71.17R", r["brk_r_short"]
    assert r["brk_r_pending"] == "—", "진입 전이면 R 은 —"
    assert r["brk_r_tick"] == "+1.00R", "가격이 바뀌면 R 셀이 매초 갱신돼야 한다"
    assert r["brk_disabled"], "close_requested 면 버튼을 잠근다"
    assert r["brk_enabled"]


def test_청산_버튼은_POST_하고_403_이면_안내한다():
    r = _run_render()
    assert r["brk_close_url"] == "/api/brackets/a1/close"
    assert r["brk_close_403"] == "실거래에서는 웹 청산이 없습니다"


def test_차트_팬_줌_상태():
    r = _run_render()
    assert r["view_follow_start"] == 189, "따라가기 = 마지막 봉 + 여백 8봉"
    assert r["latest_hidden"] is True, "따라가는 중엔 최신 버튼을 숨긴다"
    assert r["view_zoom_in"] < 120, "휠(위)은 봉 수를 줄인다"
    assert r["view_zoom_out"] == 120
    assert r["view_min"] == 20 and r["view_max"] == 600, "봉 수는 20~600"
    assert r["drag_follow"] is False, "드래그 시작 = 따라가기 해제"
    assert r["drag_start"] < 189, "오른쪽 드래그 = 과거로 팬"
    assert r["latest_shown"], "팬 중엔 최신 버튼이 보인다"
    assert r["latest_follow"] is True and r["latest_start"] == 189, "최신 버튼이 따라가기를 되살린다"
    assert r["more_limit"] == 600 and "limit=600" in r["more_url"], "왼쪽 끝에 닿으면 더 받아온다"
    assert r["empty_chart_ok"] is True, f"봉이 없을 때 죽는다: {r['empty_chart_ok']}"


def test_멀리_있는_가로선은_축을_늘리지_않고_꼬리표로만_보인다():
    r = _run_render()
    assert r["fit_unchanged"], f"TP2=140 이 축을 늘렸다: {r['fit_near']} → {r['fit_far']}"
    assert "TP2" in r["fit_far"]["tags"], f"화면 밖 선이 꼬리표로 안 나온다: {r['fit_far']}"
    assert "TP1" not in r["fit_far"]["tags"], "봉 범위 ±60% 안의 선은 그대로 그린다"
    assert r["fit_price_in"], "현재가는 항상 축 안에 있어야 한다"


def test_서버가_뒤처져도_살아있는_봉이_뭉개지지_않는다():
    """실제 버그: DB 1분봉이 90분 멈춘 동안 15초마다 loadChart 가 임시 봉을 지워
    90분 움직임이 봉 하나로 뭉개졌다. 서버 봉은 합쳐야지 바꿔치우면 안 된다."""
    r = _run_render()
    assert r["merge_prov_before"] == 3, r
    assert r["merge_bars_after"] == 6 and r["merge_prov_after"] == 3, f"임시 봉이 지워졌다: {r}"
    assert r["merge_live_kept"], "살아있는 봉을 새로 만들면 안 된다"
    assert r["merge_order_ok"], "봉 순서가 어긋났다"
    assert r["merge_replaced"], "서버가 따라잡으면 임시 봉은 서버 값으로 바뀌어야 한다"
    assert r["merge_live_still"], "서버가 따라잡아도 살아있는 봉은 남는다"


def test_옛_서버를_알린다():
    r = _run_render()
    assert r["old_notice"] == "서버가 옛 버전입니다 — `th serve` 를 껐다가 다시 켜세요", r["old_notice"]
    assert r["old_foot"], "하단 연결 표시에도 알려야 한다"
    assert r["new_notice_hidden"], "ui:3 이면 알림을 숨긴다"
    assert r["old_by_timeout"] and not r["new_by_timeout"], "15초 동안 watchlist 가 없으면 옛 서버로 본다"


def test_크로스헤어_축_꼬리표():
    r = _run_render()
    assert r["cross_set"], "포인터 이동 뒤 S.cross 에 가격·시각이 있어야 한다"
    assert r["cross_price_in_fit"], "커서 가격은 축 범위 안"
    assert r["cross_cleared"], "포인터가 나가면 지운다"


# ── v2.1 (docs/15 §6-9) ─────────────────────────────────────
def test_워치리스트_그룹_칩이_거르고_머리글_수가_따라간다():
    r = _run_render()
    assert r["grp_shown"] and r["grp_chips"] == 5, "전체·반도체·지수ETF·대형기술주·기타 칩 5개"
    assert r["grp_all_title"] == "워치리스트 · 36"
    assert r["grp_semi_rows"] == 12 and r["grp_semi_title"] == "워치리스트 · 12", "그룹으로 거르고 머리글 수도 따라간다"
    assert r["grp_semi_on"], "선택된 칩이 강조된다"
    assert r["grp_semi_search"] == 2, "검색은 그룹 안에서 동작한다"
    assert r["grp_etc_rows"] == 6 and r["grp_back_rows"] == 36
    assert r["grp_hidden_old"], "group 이 없는 옛 서버 메시지면 칩을 숨긴다"


def test_실매매_포지션_탭이_종목별_한_줄로_매초_평가손익을_센다():
    r = _run_render()
    assert r["pos_rows"] == 2 and r["pos_title"] == "실매매 포지션 · 열림 2"
    assert r["pos_wallet"] == "2,890.5 USDT" and r["pos_notional"] == "2,518 USDT"
    # LONG 포커스 종목은 S.price(117.21): (117.21 − 125.62) × 12.61 = −106.05 · ÷ 진입 노셔널 = −6.69%
    assert r["pos_long_pnl"] == "-106.05 USDT · -6.69%", r["pos_long_pnl"]
    # SHORT 는 워치리스트 가격(NVDA=104): (110 − 104) × 10 = +60 · +5.45%
    assert r["pos_short_pnl"] == "+60.00 USDT · +5.45%", r["pos_short_pnl"]
    assert r["pos_long_liq"] == "6.25%" and r["pos_short_liq"] == "25.00%", "청산까지% 도 현재가로 다시 센다"
    assert r["pos_no_stop"], "손절 없음은 빨갛게"
    assert r["pos_stop"] == "112.00 · 7.69%", r["pos_stop"]
    assert r["pos_sel"] and r["pos_lev"]
    assert r["pos_tick_pnl"] == "-70.87 USDT · -4.47%" and r["pos_tick_cls"] == "ra down", "가격이 바뀌면 셀만 매초 갱신"
    assert r["pos_tick_px"] == "120.00"
    assert r["pos_fail_rows"] == 2 and r["pos_fail_stale"] and r["pos_fail_note"], "ok:false 면 마지막 값 + 흐리게"
    assert r["pos_fail_title"] == "실매매 포지션 · 열림 2"
    assert r["pos_empty"], "빈 목록(ok:true)은 '열린 포지션이 없습니다'"
    assert r["tab_pos"] and r["tab_brk"], "탭 전환이 보이는 표·머리글을 바꾼다"
    assert r["pos_click_focus"]["method"] == "POST" and '"NVDAUSDT"' in r["pos_click_focus"]["body"], \
        "줄을 누르면 그 종목으로 포커스"


def test_주문_패널_미리보기가_서버로_가고_결과를_그린다():
    r = _run_render()
    assert r["order_sym"] == "SOXLUSDT" and r["order_lev"] == "3x", "종목은 포커스 종목(읽기전용) + 배수 칩"
    assert r["order_risk_default"] == "0.5", "위험 % 기본값 0.5"
    assert r["submit_off_empty"], "손절이 비면 주문 넣기가 잠긴다"
    assert r["prev_method"] == "POST"
    assert r["prev_body"] == {"mode": "paper", "symbol": "SOXLUSDT", "direction": "LONG", "stop": 99, "risk_pct": 0.5}, \
        r["prev_body"]
    for need in ("41.67", "4,167 USDT", "14.45 USDT", "0.99%", "0.12", "2,890 USDT"):
        assert need in r["prev_html"], f"미리보기에 {need} 가 없다: {r['prev_html']}"
    assert r["prev_warn"] == "<li>비용/R 주의</li>" and r["prev_reasons"] == ""
    assert r["submit_on"], "사유가 없으면 주문 넣기가 열린다"
    # 빠른 채우기 — 편의일 뿐, 손절폭 기준으로 계산
    assert r["quick_tp1"] == "101.5" and r["quick_tp2"] == "103"
    assert r["short_sl_m_off"] and r["short_sl_p_on"] and r["quick_stop_short"] == "101"
    assert r["long_sl_m_on"] and r["quick_stop_long"] == "99"
    # 가드 사유가 있으면 빨갛게 + 잠김
    assert r["reason_html"] == "<li>오늘 손실 한도 도달</li>" and r["submit_blocked"]


def test_주문_넣기_성공_403_503_거절():
    r = _run_render()
    assert r["submit_ready"]
    assert r["sub_body"] == {"mode": "paper", "symbol": "SOXLUSDT", "direction": "LONG", "stop": 99,
                             "entry": 100.5, "tp1": 101.5, "tp2": 103, "risk_pct": 0.5, "expire_s": 300}, r["sub_body"]
    assert r["sub_msg"] == "브래킷 열림 · a1b2c3d4" and r["sub_cls"] == "ok"
    assert r["sub_cleared"], "성공하면 진입·손절·TP 를 비운다"
    assert r["sub_kept"], "방향·위험%·만료는 남긴다"
    assert r["sub_reload"] and r["sub_locked_after"]
    assert r["sub_403"] == "실거래 주문 경로가 없습니다" and r["sub_403_cls"] == "bad"
    assert r["sub_503"] == "페이퍼 엔진이 꺼져 있습니다 — th serve 재시작"
    assert r["sub_reject_reasons"] == "<li>세션 차단: 이벤트 30분 전</li><li>열린 브래킷 3개</li>"
    assert r["sub_reject_msg"] == "가드에 걸려 열지 않았습니다" and r["sub_reject_locked"]
    assert r["sub_reject_kept"], "거절돼도 폼은 지우지 않는다 (고쳐서 다시 넣는다)"


def test_주문_패널_실매매_탭은_잠겨_있고_게이트_체크리스트를_보인다():
    r = _run_render()
    assert r["live_gate_url"] == "/api/orders/gate"
    assert r["live_locked"], "폼 비활성 + 잠김 안내 + 미리보기·주문 숨김"
    assert r["live_gate"] == ('<li class="up"><b>✓</b><span>페이퍼 30건 이상</span></li>'
                              '<li class="down"><b>✗</b><span>기대값 양수</span></li>')
    assert r["live_sum"] == "1/2 충족 · 실거래 잠김"
    assert r["live_submit_off"]
    assert r["paper_back"], "페이퍼 탭으로 돌아오면 폼이 풀린다"
    assert r["focus_clears_form"], "종목이 바뀌면 다른 종목 가격으로 넣은 손절·목표는 버린다"
