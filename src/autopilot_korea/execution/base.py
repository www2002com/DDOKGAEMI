"""브로커 공통 타입. 토스증권(live)과 내부 모의 브로커(paper)가 같은 인터페이스를 따른다."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Holding:
    ticker: str
    quantity: int
    avg_price: float
    last_price: float
    name: str = ""

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price

    @property
    def pnl_pct(self) -> float | None:
        return self.last_price / self.avg_price - 1 if self.avg_price else None


@dataclass
class AccountSnapshot:
    cash: float
    holdings: dict[str, Holding] = field(default_factory=dict)

    @property
    def total_value(self) -> float:
        return self.cash + sum(h.market_value for h in self.holdings.values())

    def weights(self) -> dict[str, float]:
        total = self.total_value
        return {t: h.market_value / total for t, h in self.holdings.items()} if total > 0 else {}


@dataclass
class OrderRequest:
    ticker: str
    side: str  # BUY | SELL
    quantity: int
    order_type: str  # LIMIT | MARKET
    price: float | None
    client_order_id: str


@dataclass
class OrderStatus:
    broker_order_id: str
    status: str  # submitted | partially_filled | filled | canceled | rejected
    filled_quantity: int = 0
    avg_fill_price: float | None = None
    message: str | None = None


class Broker(Protocol):
    name: str

    def account(self) -> AccountSnapshot: ...
    def submit(self, req: OrderRequest) -> str: ...
    def order_status(self, broker_order_id: str) -> OrderStatus: ...
    def cancel(self, broker_order_id: str) -> str: ...
    def best_quote(self, ticker: str) -> dict | None: ...
    def regulatory_flags(self, ticker: str) -> list[str]: ...


# ── KRX 호가단위 (2023년 1월 개편 후 유가·코스닥 통일) ─────────────────────

def tick_size(price: float, *, is_etf: bool = False) -> int:
    if is_etf:
        return 1 if price < 2_000 else 5
    for limit, tick in ((2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50), (200_000, 100), (500_000, 500)):
        if price < limit:
            return tick
    return 1_000


def snap_price(price: float, side: str, *, is_etf: bool = False) -> int:
    """호가단위에 맞춘다. 매수는 올림(체결 우선), 매도는 내림."""
    tick = tick_size(price, is_etf=is_etf)
    units = math.ceil(price / tick) if side == "BUY" else math.floor(price / tick)
    return int(max(units, 1) * tick)
