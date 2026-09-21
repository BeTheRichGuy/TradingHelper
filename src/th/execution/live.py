"""실거래 클라이언트 — **아직 열려 있지 않다.**

여는 조건은 `docs/07` 확장성 설계 결정에 적힌 그대로다. 여기 코드를 채우기 전에
그 조건들이 전부 ✅ 가 되어야 한다.

  1. Phase 4 백테스트 통과 (현재 ❌ — `docs/11` 최종판정 참고)
  2. 페이퍼 트레이딩 4주 이상, 표시 성적과 실제 성적의 괴리 10%p 이내
  3. 재시작 복구가 실제 중단 상황에서 검증됨
  4. 주문 권한이 있는 API 키를 별도로 발급 (읽기 전용 키와 분리)

**우위가 없는 전략을 자동화하면 손실만 자동화된다.** 지금 이 파일이 비어 있는 것은
미완성이 아니라 의도된 잠금이다.
"""

from __future__ import annotations

from .models import Fill, Order

GATE_CONDITIONS = (
    "Phase 4 백테스트 통과",
    "페이퍼 트레이딩 4주 + 괴리 10%p 이내",
    "재시작 복구 실증",
    "주문 권한 API 키 분리 발급",
)

# docs/07 게이트 체크리스트 — 웹 주문 패널의 '실매매' 탭이 그대로 보여준다 (docs/15 7절).
# **전부 met=False 다.** 하나라도 True 로 바꾸려면 그 근거 문서가 먼저 있어야 한다.
GATE_CHECKLIST: tuple[tuple[str, bool], ...] = (
    ("① 검증된 우위 (docs/14 ② 다종목 24/34)", False),
    ("② 페이퍼 60거래일 낙폭·샤프 실측", False),
    ("③ 실거래 클라이언트 구현·검수", False),
    ("④ 키 권한 최소(읽기+선물)·IP 제한", False),
    ("⑤ 수동 승인 스위치", False),
)

# 웹 주문 패널이 mode="live" 를 받았을 때의 거절 문장 (HTTP 403). docs/15 7절 그대로.
LIVE_ORDER_REFUSED_KO = "실거래 주문 경로는 아직 없습니다 (docs/07 게이트)"


def gate_status(mode: str) -> dict:
    """`GET /api/orders/gate` 본문. `live_allowed` 는 **항상 False** — 계산이 아니라 상수다."""
    return {
        "mode": mode,
        "live_allowed": False,
        "conditions": [{"text_ko": t, "met": bool(m)} for t, m in GATE_CHECKLIST],
    }


class LiveClient:
    """실거래. 모든 메서드가 예외를 던진다."""

    mode = "live"

    def __init__(self, *_a, **_kw) -> None:
        raise NotImplementedError(
            "실거래는 아직 잠겨 있다. 열기 전에 필요한 것:\n  - "
            + "\n  - ".join(GATE_CONDITIONS)
            + "\n\n지금은 TH_EXECUTION_MODE=paper 로 페이퍼 트레이딩을 쓴다."
        )

    async def place(self, order: Order) -> Order:  # pragma: no cover
        raise NotImplementedError

    async def cancel(self, order: Order) -> Order:  # pragma: no cover
        raise NotImplementedError

    async def fetch(self, order: Order) -> Order:  # pragma: no cover
        raise NotImplementedError

    async def poll_fills(self) -> list[Fill]:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover
        raise NotImplementedError
