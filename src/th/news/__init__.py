"""뉴스·이벤트 매매 엔진 (docs/17).

패키지 최상위는 아무것도 끌어오지 않는다. `th.sessions` 와 같은 이유다 — 여기를
임포트했다고 텔레그램·Claude·DB 드라이버가 딸려 오면 안 된다. 각 모듈을 직접 임포트한다.

  models     자료형 (NewsItem · Judgment · Plan · Leg)
  sources/   수집 소스 (텔레그램 · RSS · SEC · Finnhub)
  tickers    텍스트 → 유니버스 심볼
  dedupe     중복 키
  store      DB
  collect    수집 루프 (th stream 안)
"""
