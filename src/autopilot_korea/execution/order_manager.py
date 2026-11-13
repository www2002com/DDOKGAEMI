"""주문 수명주기: 제안 → (사람) 승인 → 제출 → 체결 동기화 → 미체결 취소.

모드별 동작:
  - dry_run: 주문을 제안 상태로 저장만 한다. 절대 제출하지 않는다.
  - paper:   auto_approve_modes에 있으면 자동 승인 후 모의 브로커로 제출한다 (needs_review 주문은 제외).
  - live:    토스증권으로 제출한다. 설정과 무관하게 사람의 승인(autopilot orders approve)이 있어야 한다.
§15: 미체결 주문은 제출 후 3거래일이 지나면 취소한다.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import select

from autopilot_korea.compliance import with_disclaimer
from autopilot_korea.db import Database
from autopilot_korea.db.models import Order
from autopilot_korea.execution.base import Broker, OrderRequest
from autopilot_korea.execution.calendar import TradingCalendar
from autopilot_korea.risk import PlannedOrder
from autopilot_korea.settings import Settings

log = logging.getLogger(__name__)
OPEN_STATES = ("submitted", "partially_filled")


class OrderManager:
    def __init__(self, settings: Settings, db: Database, broker: Broker, calendar: TradingCalendar):
        self.settings, self.db, self.broker, self.calendar = settings, db, broker, calendar

    def propose(self, run_id: str, planned: list[PlannedOrder]) -> list[int]:
        ids = []
        with self.db.session() as s:
            for p in planned:
                order = Order(
                    run_id=run_id, client_order_id=uuid.uuid4().hex[:32], ticker=p.ticker, side=p.side,
                    order_type=p.order_type, quantity=p.quantity, price_limit=p.price_limit,
                    status="blocked" if p.blocked_reason else "proposed",
                    reasoning=with_disclaimer(p.reasoning), risk_flags=p.risk_flags,
                    needs_review=p.needs_review, not_before=p.not_before, error=p.blocked_reason,
                )
                s.add(order)
                s.flush()
                ids.append(order.id)
        return ids

    def auto_approve(self, run_id: str) -> int:
        """paper 모드 전용 자동 승인. live는 어떤 설정으로도 자동 승인하지 않는다."""
        mode = self.settings.mode
        if mode == "live" or mode not in self.settings.constraints.execution.auto_approve_modes:
            return 0
        with self.db.session() as s:
            rows = s.scalars(select(Order).where(Order.run_id == run_id, Order.status == "proposed",
                                                 Order.needs_review.is_(False))).all()
            for o in rows:
                o.status, o.approved_by = "approved", f"auto:{mode}"
            return len(rows)

    def approve(self, order_ids: list[int], approver: str) -> int:
        with self.db.session() as s:
            rows = s.scalars(select(Order).where(Order.id.in_(order_ids), Order.status == "proposed")).all()
            for o in rows:
                o.status, o.approved_by = "approved", approver
            return len(rows)

    def reject(self, order_ids: list[int], approver: str) -> int:
        with self.db.session() as s:
            rows = s.scalars(select(Order).where(Order.id.in_(order_ids), Order.status.in_(("proposed", "approved")))).all()
            for o in rows:
                o.status, o.approved_by = "rejected", approver
            return len(rows)

    def submit_approved(self, today: date) -> dict[str, int]:
        if self.settings.mode == "dry_run":
            return {"submitted": 0, "skipped_dry_run": 1}
        result = {"submitted": 0, "failed": 0}
        with self.db.session() as s:
            rows = s.scalars(select(Order).where(Order.status == "approved").order_by(Order.side.desc(), Order.id)).all()
            # SELL을 먼저 낸다(side 내림차순: SELL > BUY) — 매도 대금으로 매수 현금을 확보한다.
            for o in rows:
                if o.not_before and o.not_before > today:
                    continue
                try:
                    o.broker_order_id = self.broker.submit(OrderRequest(o.ticker, o.side, o.quantity, o.order_type,
                                                                        o.price_limit, o.client_order_id))
                    o.status, o.submitted_at = "submitted", datetime.now(timezone.utc)
                    result["submitted"] += 1
                except Exception as exc:  # noqa: BLE001 — 주문 단위 격리
                    o.status, o.error = "failed", str(exc)
                    result["failed"] += 1
                    log.error("주문 제출 실패 %s %s: %s", o.ticker, o.side, exc)
        self.sync()
        return result

    def sync(self) -> int:
        updated = 0
        with self.db.session() as s:
            ids = [o.id for o in s.scalars(select(Order).where(Order.status.in_(OPEN_STATES))).all()]
        for oid in ids:
            with self.db.session() as s:
                o = s.get(Order, oid)
                st = self.broker.order_status(o.broker_order_id)
                if (st.status, st.filled_quantity) != (o.status, o.filled_quantity):
                    o.status, o.filled_quantity = st.status, st.filled_quantity
                    o.avg_fill_price = st.avg_fill_price or o.avg_fill_price
                    o.error = st.message or o.error
                    updated += 1
        return updated

    def cancel_stale(self, today: date, trading_days: int = 3) -> int:
        canceled = 0
        with self.db.session() as s:
            rows = s.scalars(select(Order).where(Order.status.in_(OPEN_STATES))).all()
            for o in rows:
                if o.submitted_at and self.calendar.trading_days_between(o.submitted_at.date(), today) >= trading_days:
                    try:
                        self.broker.cancel(o.broker_order_id)
                        o.status, o.error = "canceled", f"미체결 {trading_days}거래일 경과 자동 취소"
                        canceled += 1
                    except Exception as exc:  # noqa: BLE001
                        o.error = f"취소 실패: {exc}"
        return canceled

    def recent_turnover(self, today: date, nav: float, days: int = 30) -> float:
        """최근 30일 체결 기준 회전율 = min(매수 체결액, 매도 체결액) ÷ NAV."""
        if nav <= 0:
            return 0.0
        since = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc).timestamp() - days * 86400
        totals = {"BUY": 0.0, "SELL": 0.0}
        with self.db.session() as s:
            for o in s.scalars(select(Order).where(Order.status.in_(("filled", "partially_filled")))).all():
                at = o.submitted_at
                if at and at.replace(tzinfo=at.tzinfo or timezone.utc).timestamp() >= since:
                    totals[o.side] += o.filled_quantity * (o.avg_fill_price or 0)
        return min(totals.values()) / nav
