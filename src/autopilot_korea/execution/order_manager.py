"""주문 수명주기: 제안 → (사람) 승인 → 제출 → 체결 동기화 → 미체결 취소.

모드별 동작:
  - dry_run: 주문을 제안 상태로 저장만 한다. 절대 제출하지 않는다.
  - paper:   auto_approve_modes에 있으면 자동 승인 후 모의 브로커로 바로 제출한다.
  - live:    auto_approve_modes에 있으면 자동 승인한다(2026-11-13 사용자 결정으로 완전 자동 매매).
             단, 실행 1회의 주문 수·총액이 점검 한도를 넘으면 사람 검토로 넘긴다.
             제출은 거래일 정규장 시간에만 하고, 제출 직전 현재 호가로 지정가를 다시 맞춘다(허용 폭 안에서).
             목록에 live가 없으면 사람의 승인(autopilot orders approve)이 있어야 한다.
공통: Stage 5 검토가 Cancel로 판정한 주문(needs_review)은 자동 승인하지 않는다.
비상 정지(`autopilot halt`)가 켜져 있으면 모든 모드에서 자동 승인과 제출을 멈춘다.
새 실행이 주문을 제안하면 이전 실행의 미제출 주문은 superseded로 바뀐다(이중 주문 방지).
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
from autopilot_korea.execution.base import Broker, OrderRequest, snap_price
from autopilot_korea.execution.calendar import TradingCalendar, now_kst
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

    def supersede_pending(self, run_id: str) -> int:
        """새 계획이 이전 계획을 대체한다: 다른 실행의 제출 전 주문(proposed·approved)을 superseded로 바꾼다."""
        with self.db.session() as s:
            rows = s.scalars(select(Order).where(Order.run_id != run_id, Order.status.in_(("proposed", "approved")))).all()
            for o in rows:
                o.status, o.error = "superseded", f"{run_id} 실행의 새 주문 계획으로 대체"
            return len(rows)

    def auto_approve(self, run_id: str, nav: float = 0.0) -> int:
        """auto_approve_modes에 있는 모드에서 자동 승인한다. live는 점검 한도 안일 때만 승인한다."""
        mode, ex = self.settings.mode, self.settings.constraints.execution
        if self.settings.halted() or mode == "dry_run" or mode not in ex.auto_approve_modes:
            return 0
        with self.db.session() as s:
            rows = s.scalars(select(Order).where(Order.run_id == run_id, Order.status == "proposed",
                                                 Order.needs_review.is_(False))).all()
            if mode == "live" and (reason := self._live_gate(rows, nav)):
                for o in rows:
                    o.needs_review, o.error = True, f"자동 승인 보류 — 사람 검토 필요: {reason}"
                log.warning("live 자동 승인 보류 (%s): %s", run_id, reason)
                return 0
            for o in rows:
                o.status, o.approved_by = "approved", f"auto:{mode}"
            return len(rows)

    def _live_gate(self, rows: list[Order], nav: float) -> str | None:
        """실계좌 자동 승인 전 마지막 점검. 비정상적으로 큰 주문 묶음은 사람이 보게 한다."""
        ex = self.settings.constraints.execution
        if len(rows) > ex.live_auto_max_orders:
            return f"주문 {len(rows)}건 > 한도 {ex.live_auto_max_orders}건"
        gross = sum(o.quantity * (o.price_limit or 0) for o in rows)
        if nav <= 0 or gross > ex.live_auto_max_gross_pct * nav:
            return f"주문 총액 {gross:,.0f}원 > 계좌 평가액 {nav:,.0f}원의 {ex.live_auto_max_gross_pct:.0%}"
        return None

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

    def submit_approved(self, today: date, now: datetime | None = None) -> dict[str, int]:
        ex = self.settings.constraints.execution
        if self.settings.mode == "dry_run":
            return {"submitted": 0, "skipped_dry_run": 1}
        if self.settings.halted():
            return {"submitted": 0, "halted": 1}
        if self.settings.mode == "live" and not self.calendar.is_market_open(now or now_kst(), ex.market_open, ex.market_close):
            return {"submitted": 0, "deferred_until_market_open": 1}  # 장 시간 제출 작업(09:05)이 낸다
        result = {"submitted": 0, "failed": 0}
        with self.db.session() as s:
            rows = s.scalars(select(Order).where(Order.status == "approved").order_by(Order.side.desc(), Order.id)).all()
            # SELL을 먼저 낸다(side 내림차순: SELL > BUY) — 매도 대금으로 매수 현금을 확보한다.
            for o in rows:
                if o.not_before and o.not_before > today:
                    continue
                if self.settings.mode == "live":
                    self._reprice(o)
                    if o.quantity <= 0:
                        o.status, o.error = "canceled", "지정가 재조정 후 수량 0"
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

    def _reprice(self, o: Order) -> None:
        """장전에 정한 지정가를 제출 시점 최우선 호가로 다시 맞춘다. 허용 폭을 넘으면 계획가를 그대로 둔다.

        매수는 수량을 줄여 계획 금액 안에 머문다 — 현금 부족으로 거부되지 않게 하기 위해서다.
        """
        if o.order_type != "LIMIT" or not o.price_limit:
            return
        try:
            quote = self.broker.best_quote(o.ticker)
        except Exception as exc:  # noqa: BLE001 — 호가가 없으면 계획가로 낸다
            log.warning("호가 조회 실패 %s: %s", o.ticker, exc)
            return
        if not quote:
            return
        cap = self.settings.constraints.execution.live_reprice_max_pct
        is_etf = o.ticker in self.settings.constraints.portfolio.cash_etf_tickers
        if o.side == "BUY" and quote.get("ask"):
            new = snap_price(float(quote["ask"]), "BUY", is_etf=is_etf)
            if new <= o.price_limit * (1 + cap):
                o.quantity = min(o.quantity, int(o.quantity * o.price_limit // new))
                o.price_limit = new
        elif o.side == "SELL" and quote.get("bid"):
            new = snap_price(float(quote["bid"]), "SELL", is_etf=is_etf)
            if new >= o.price_limit * (1 - cap):
                o.price_limit = new

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
