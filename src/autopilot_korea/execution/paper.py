"""내부 모의 브로커 (paper 모드).

토스증권에는 모의투자 환경이 없으므로, 모의 운용은 이 브로커가 맡는다.
상태를 따로 저장하지 않는다: 계좌 = 초기 자본 + orders 테이블의 체결 내역. 그래서 재시작해도 일관된다.
체결 규칙: 지정가가 현재가 기준으로 체결 가능하면(매수 지정가 ≥ 현재가, 매도 지정가 ≤ 현재가) 슬리피지를 반영해 전량 체결한다.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy import select

from autopilot_korea.db import Database
from autopilot_korea.db.models import Order
from autopilot_korea.execution.base import AccountSnapshot, Holding, OrderRequest, OrderStatus
from autopilot_korea.settings import Settings

FILLED_STATES = ("filled", "partially_filled")


class PaperBroker:
    name = "paper"

    def __init__(self, settings: Settings, db: Database, price_lookup: Callable[[str], float | None]):
        self.rules = settings.constraints.execution
        self.initial_capital = float(settings.constraints.portfolio.initial_capital_krw)
        self.db = db
        self.price_lookup = price_lookup

    def account(self) -> AccountSnapshot:
        cash = self.initial_capital
        qty: dict[str, int] = {}
        cost: dict[str, float] = {}
        with self.db.session() as s:
            fills = s.scalars(select(Order).where(Order.status.in_(FILLED_STATES), Order.broker_order_id.like("paper-%"))
                              .order_by(Order.id)).all()
            for o in fills:
                value = o.filled_quantity * (o.avg_fill_price or 0)
                if o.side == "BUY":
                    cash -= value * (1 + self.rules.paper_commission_rate)
                    cost[o.ticker] = cost.get(o.ticker, 0.0) + value
                    qty[o.ticker] = qty.get(o.ticker, 0) + o.filled_quantity
                else:
                    cash += value * (1 - self.rules.paper_commission_rate - self.rules.paper_sell_tax_rate)
                    held = qty.get(o.ticker, 0)
                    if held:
                        cost[o.ticker] = cost.get(o.ticker, 0.0) * (1 - o.filled_quantity / held)
                    qty[o.ticker] = held - o.filled_quantity
        holdings = {}
        for t, q in qty.items():
            if q > 0:
                avg = cost[t] / q
                holdings[t] = Holding(ticker=t, quantity=q, avg_price=avg, last_price=self.price_lookup(t) or avg)
        return AccountSnapshot(cash=cash, holdings=holdings)

    def submit(self, req: OrderRequest) -> str:
        return f"paper-{req.client_order_id}"

    def order_status(self, broker_order_id: str) -> OrderStatus:
        with self.db.session() as s:
            order = s.scalars(select(Order).where(Order.broker_order_id == broker_order_id)).first()
        if order is None:
            return OrderStatus(broker_order_id, "rejected", message="모의 주문을 찾을 수 없습니다")
        if order.status in ("filled", "canceled", "rejected"):
            return OrderStatus(broker_order_id, order.status, order.filled_quantity, order.avg_fill_price)
        price = self.price_lookup(order.ticker)
        if not price:
            return OrderStatus(broker_order_id, "submitted", message="현재가 없음 — 체결 보류")
        slip = self.rules.paper_slippage_pct
        if order.side == "BUY":
            exec_price = price * (1 + slip)
            if order.order_type == "LIMIT" and order.price_limit is not None and order.price_limit < price:
                return OrderStatus(broker_order_id, "submitted")
            if order.order_type == "LIMIT" and order.price_limit is not None:
                exec_price = min(exec_price, order.price_limit)
            cash = self.account().cash
            if order.quantity * exec_price * (1 + self.rules.paper_commission_rate) > cash + 1:
                return OrderStatus(broker_order_id, "rejected", message="모의 계좌 현금 부족")
        else:
            exec_price = price * (1 - slip)
            if order.order_type == "LIMIT" and order.price_limit is not None and order.price_limit > price:
                return OrderStatus(broker_order_id, "submitted")
            if order.order_type == "LIMIT" and order.price_limit is not None:
                exec_price = max(exec_price, order.price_limit)
            held = self.account().holdings.get(order.ticker)
            if held is None or held.quantity < order.quantity:
                return OrderStatus(broker_order_id, "rejected", message="모의 계좌 보유 수량 부족")
        return OrderStatus(broker_order_id, "filled", order.quantity, round(exec_price, 2))

    def cancel(self, broker_order_id: str) -> str:
        return broker_order_id

    def best_quote(self, ticker: str) -> dict | None:
        return None

    def regulatory_flags(self, ticker: str) -> list[str]:
        return []
