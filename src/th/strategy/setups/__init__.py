"""셋업 모음.

⚠️ **여기 있는 셋업들은 실측에서 방향성 우위가 확인되지 않았습니다** (2026-09-08).

    S1_vwap_pullback    n=  92   기대값 −0.053R
    S3_sweep_reversal   n=  65   기대값 −0.340R
    S5_vwma_pullback    n= 929   기대값 −0.178R
    방향을 뒤집어도 결과가 같았고, 기대값이 정확히 거래비용선과 겹쳤다.

근거와 재현 방법은 `docs/08-전략-검증-결과.md`. 이 코드는 **검증된 전략이 아니라
검증을 통과하지 못한 후보**다. 화면(`th watch`)은 이걸로 방향을 제시하지 않는다.

남겨두는 이유는 두 가지다. ① 데이터 커버리지가 채워진 뒤 다시 검정하면 답이
달라질 수 있다 (지금 판정은 OI·펀딩·basis 조건이 사실상 꺼진 상태의 결과다).
② `th research` 로 새 셋업을 검정할 때의 비교 기준이 된다.

**다시 신호로 쓰려면** `04` 문서의 Phase 4 통과 기준을 넘어야 한다 —
out-of-sample 기대값 ≥ +0.20R, 표본 ≥ 300.

---

S5(VWMA 정배열/역배열 눌림)가 사용자가 지목한 방식이라 주 셋업 자리에 있다.
S1(VWAP 되돌림)·S3(스윕 반전)은 비교군이다.

S2(오프닝 레인지 돌파)·S4(밴드 평균회귀)는 같은 프로토콜만 맞추면 바로 붙는다 —
`base.Setup` 을 구현하고 `engine.SETUPS` 에 추가하면 끝이다.
"""

from .base import Condition, Direction, EntryZone, SetupResult, cond
from .s1_vwap_pullback import S1VwapPullback
from .s3_sweep_reversal import S3SweepReversal
from .s5_vwma_pullback import S5VwmaPullback

__all__ = [
    "Condition",
    "Direction",
    "EntryZone",
    "S1VwapPullback",
    "S3SweepReversal",
    "S5VwmaPullback",
    "SetupResult",
    "cond",
]
