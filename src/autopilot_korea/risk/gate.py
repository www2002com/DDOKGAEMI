"""리스크 관리 레이어 (§7).

1. 포트폴리오 MDD −15% → 전체 재검토 + 현금성 비중 상향 (drawdown → PipelineContext.drawdown_review)
2. 종목 −25% → thesis 재평가 플래그 (positions_needing_review)
3. 실행 전 게이트: risk_flags, KOREA_REGULATION, 컴플라이언스 (pre_execution_gate)
4. 유동성: 20일 평균 거래대금 하위 제외(유니버스 단계), 일 거래대금 5% 초과 주문 분할 (split_quantity)
5. 섹터 상한 35% — Stage 4 validate가 강제
6. 회전율 상한 (turnover_scale)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from autopilot_korea.compliance import scan_text
from autopilot_korea.execution.base import AccountSnapshot
from autopilot_korea.settings import Settings


@dataclass
class PlannedOrder:
    ticker: str
    side: str  # BUY | SELL
    quantity: int
    order_type: str
    price_limit: float | None
    reasoning: str
    risk_flags: list[str] = field(default_factory=list)
    not_before: date | None = None
    needs_review: bool = False
    forced: bool = False  # 규칙상 반드시 나가야 하는 매도 (thesis exit, 차단 종목) — 회전율 축소 대상 아님
    blocked_reason: str | None = None

    @property
    def notional(self) -> float:
        return self.quantity * (self.price_limit or 0)


def max_drawdown(navs: list[float]) -> float:
    """현재 시점의 고점 대비 낙폭 (음수)."""
    if not navs:
        return 0.0
    peak = max(navs)
    return navs[-1] / peak - 1 if peak > 0 else 0.0


def positions_needing_review(account: AccountSnapshot, threshold: float) -> list[str]:
    return [t for t, h in account.holdings.items() if h.pnl_pct is not None and h.pnl_pct <= threshold]


def split_quantity(quantity: int, price: float, adv_value: float | None, max_pct: float) -> list[int]:
    """주문 금액이 20일 평균 거래대금의 max_pct를 넘으면 여러 날로 나눈다."""
    if not adv_value or adv_value <= 0 or quantity <= 0:
        return [quantity]
    cap_value = adv_value * max_pct
    n = max(1, math.ceil(quantity * price / cap_value))
    base, rem = divmod(quantity, n)
    return [q for q in (base + (1 if i < rem else 0) for i in range(n)) if q > 0]


def turnover_scale(planned_turnover: float, recent_turnover: float, monthly_cap: float) -> float:
    """이번 리밸런싱의 비강제 거래를 얼마나 줄여야 30일 누적 회전율이 상한 안에 드는가 (0~1)."""
    if planned_turnover <= 0:
        return 1.0
    room = monthly_cap - recent_turnover
    return max(0.0, min(1.0, room / planned_turnover))


def pre_execution_gate(orders: list[PlannedOrder], settings: Settings,
                       flags_by_ticker: dict[str, list[str]]) -> list[PlannedOrder]:
    """주문별로 차단 사유를 기록한다 (blocked_reason). 차단된 주문은 OrderManager가 blocked로 저장한다."""
    blocked = set(settings.constraints.risk.blocked_flags)
    max_value = settings.constraints.execution.max_order_value_krw
    for o in orders:
        flags = set(o.risk_flags) | set(flags_by_ticker.get(o.ticker, []))
        o.risk_flags = sorted(flags)
        reasons = []
        if o.quantity <= 0:
            reasons.append("수량 0 이하")
        if settings.feature("KOREA_REGULATION"):
            if o.side == "BUY" and flags & blocked:
                reasons.append(f"매수 차단 플래그 {sorted(flags & blocked)}")
            if "거래정지" in flags:
                reasons.append("거래정지 종목")
        if o.notional > max_value:
            reasons.append(f"주문 금액 {o.notional:,.0f}원 > 1건 상한 {max_value:,.0f}원")
        if hits := scan_text(o.reasoning or "", "reasoning"):
            reasons.append(f"컴플라이언스 위반 표현 {[h['match'] for h in hits]}")
        o.blocked_reason = "; ".join(reasons) or None
    return orders
