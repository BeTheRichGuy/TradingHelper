"""워크포워드 분할 — 순수 함수.

**퍼지(purge)와 엠바고(embargo)가 핵심이다.**

트레이드는 시각 하나가 아니라 구간(진입~청산)을 차지한다. 그래서 학습 구간의
마지막 트레이드가 검증 구간까지 물려 있으면, 검증 구간의 가격 정보가 학습 쪽으로
새어 들어간다. 경계에 걸친 트레이드를 **버리고**(purge), 경계 직후 일정 시간을
**비우는**(embargo) 이유다.

지금은 자동 파라미터 튜닝을 하지 않으므로 새는 경로가 좁지만, Phase 6에서
모델을 학습시키는 순간 이 분할이 그대로 필요해진다. 그때 급히 만들면 틀린다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import Trade

EMBARGO_MINUTES = 240      # 최대 보유시간과 같게 둔다


@dataclass(frozen=True, slots=True)
class Fold:
    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    train: tuple[Trade, ...]
    test: tuple[Trade, ...]
    purged: int


def _span(trades: list[Trade]) -> tuple[datetime, datetime]:
    return min(t.signal_at for t in trades), max(t.exit_at for t in trades)


def split_in_out(
    trades: list[Trade], oos_fraction: float = 0.4, embargo_minutes: int = EMBARGO_MINUTES
) -> tuple[list[Trade], list[Trade], int]:
    """앞쪽 in-sample, 뒤쪽 out-of-sample. (IS, OOS, 버린 개수)

    경계를 **시각**으로 자른다. 개수로 자르면 경계 트레이드가 양쪽에 걸린다.
    """
    if not trades:
        return [], [], 0
    ordered = sorted(trades, key=lambda t: t.signal_at)
    start, end = _span(ordered)
    cut = start + (end - start) * (1 - oos_fraction)
    embargo = timedelta(minutes=embargo_minutes)

    is_, oos, purged = [], [], 0
    for t in ordered:
        if t.exit_at <= cut:
            is_.append(t)
        elif t.signal_at >= cut + embargo:
            oos.append(t)
        else:
            purged += 1
    return is_, oos, purged


def walk_forward(
    trades: list[Trade],
    folds: int = 4,
    embargo_minutes: int = EMBARGO_MINUTES,
) -> list[Fold]:
    """앞으로 굴리는 분할. 학습 구간은 누적, 검증 구간은 그다음 토막."""
    if not trades or folds < 2:
        return []
    ordered = sorted(trades, key=lambda t: t.signal_at)
    start, end = _span(ordered)
    total = end - start
    if total.total_seconds() <= 0:
        return []

    embargo = timedelta(minutes=embargo_minutes)
    out: list[Fold] = []
    for i in range(1, folds):
        train_end = start + total * (i / folds)
        test_start = train_end + embargo
        test_end = start + total * ((i + 1) / folds)
        if test_start >= test_end:
            continue

        train = [t for t in ordered if t.exit_at <= train_end]
        test = [t for t in ordered if test_start <= t.signal_at and t.exit_at <= test_end]
        used = {id(t) for t in train} | {id(t) for t in test}
        purged = sum(
            1
            for t in ordered
            if t.signal_at < test_end and t.exit_at > start and id(t) not in used
        )
        out.append(
            Fold(
                index=i,
                train_start=start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                train=tuple(train),
                test=tuple(test),
                purged=purged,
            )
        )
    return out
