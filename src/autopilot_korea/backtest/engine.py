"""백테스트 엔진 (§14).

룩어헤드 바이어스 차단:
  1. 데이터: 리밸런싱일 D의 결정은 직전 거래일 종가까지의 데이터로 하고(signal_date), 체결은 D의 시가로 한다.
     팩트시트는 as_of 이전 데이터와 as_of 이전 공시만 쓴다 (collectors.factsheet의 가드).
  2. 모델 지식: LLM은 학습 기간의 미래를 이미 안다. 백테스트 시작일이 모델 지식 마감일 이전이면 실행을 거부한다.
  3. 웹검색: 웹검색은 현재 시점 뉴스를 돌려주므로 백테스트에서는 끈다 (strategies.PipelineStrategy).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

from sqlalchemy import select

from autopilot_korea.backtest.metrics import summarize
from autopilot_korea.db import Database
from autopilot_korea.db.models import PriceData


class LookaheadError(RuntimeError):
    pass


class Strategy(Protocol):
    name: str

    def target_weights(self, signal_date: date, current: dict[str, float]) -> dict[str, float]: ...


@dataclass
class BacktestConfig:
    start: date
    end: date
    initial_capital: float = 50_000_000
    rebalance_weekdays: tuple[int, ...] = (0, 3)  # 월·목 (주 2회)
    commission: float = 0.00015
    sell_tax: float = 0.002
    slippage: float = 0.001
    model_knowledge_cutoff: date | None = None  # 이 날짜 이전 구간은 LLM 백테스트로 쓸 수 없다


@dataclass
class BacktestResult:
    strategy: str
    dates: list[date]
    navs: list[float]
    turnovers: list[float]
    trades: list[dict] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


class PriceBook:
    """price_data의 시가·종가. 날짜 d 이전 마지막 값을 찾는다."""

    def __init__(self, db: Database, start: date, end: date, tickers: list[str] | None = None):
        q = select(PriceData).where(PriceData.date >= start - timedelta(days=10), PriceData.date <= end)
        if tickers:
            q = q.where(PriceData.ticker.in_(tickers))
        self.rows: dict[str, dict[date, tuple[float | None, float | None]]] = {}
        with db.session() as s:
            for r in s.scalars(q):
                self.rows.setdefault(r.ticker, {})[r.date] = (r.open, r.close)
        self.dates = sorted({d for series in self.rows.values() for d in series if start <= d <= end})

    def close(self, ticker: str, d: date) -> float | None:
        series = self.rows.get(ticker, {})
        past = [x for x in series if x <= d and series[x][1]]
        return series[max(past)][1] if past else None

    def open(self, ticker: str, d: date) -> float | None:
        o = self.rows.get(ticker, {}).get(d, (None, None))[0]
        return o or self.close(ticker, d)


def check_lookahead(cfg: BacktestConfig) -> None:
    if cfg.model_knowledge_cutoff and cfg.start <= cfg.model_knowledge_cutoff:
        raise LookaheadError(
            f"백테스트 시작일 {cfg.start}가 모델 지식 마감일 {cfg.model_knowledge_cutoff} 이전입니다. "
            "LLM이 이미 결과를 알고 있는 기간이라 성과가 부풀려집니다. 마감일 이후 구간으로 실행하십시오.")


def run_backtest(prices: PriceBook, strategy: Strategy, cfg: BacktestConfig,
                 benchmark: dict[date, float] | None = None) -> BacktestResult:
    check_lookahead(cfg)
    cash, qty = cfg.initial_capital, {}
    dates, navs, turnovers, trades = [], [], [], []
    for i, d in enumerate(prices.dates):
        if i > 0 and d.weekday() in cfg.rebalance_weekdays:
            signal_date = prices.dates[i - 1]
            nav_prev = navs[-1]
            current = {t: q * (prices.close(t, signal_date) or 0) / nav_prev for t, q in qty.items()} if nav_prev else {}
            target = strategy.target_weights(signal_date, current)
            cash, qty, buys, sells = _rebalance(prices, d, cash, qty, target, nav_prev, cfg, trades)
            turnovers.append(min(buys, sells) / nav_prev if nav_prev else 0.0)
        nav = cash + sum(q * (prices.close(t, d) or 0) for t, q in qty.items())
        dates.append(d)
        navs.append(nav)
    bench = [benchmark[d] for d in dates] if benchmark and all(d in benchmark for d in dates) else None
    result = BacktestResult(strategy.name, dates, navs, turnovers, trades)
    if navs:
        result.metrics = summarize(navs, dates, turnovers, bench)
    return result


def _rebalance(prices: PriceBook, d: date, cash: float, qty: dict[str, int], target: dict[str, float],
               nav: float, cfg: BacktestConfig, trades: list[dict]) -> tuple[float, dict[str, int], float, float]:
    qty = dict(qty)
    buys = sells = 0.0
    wanted = {t: int(target.get(t, 0) * nav / p) for t in set(target) | set(qty) if (p := prices.open(t, d))}
    for t, want in sorted(wanted.items(), key=lambda kv: kv[1] - qty.get(kv[0], 0)):  # 매도 먼저
        have = qty.get(t, 0)
        price = prices.open(t, d)
        if want < have:
            px = price * (1 - cfg.slippage)
            value = (have - want) * px
            cash += value * (1 - cfg.commission - cfg.sell_tax)
            sells += value
            trades.append({"date": d.isoformat(), "ticker": t, "side": "SELL", "qty": have - want, "price": px})
        elif want > have:
            px = price * (1 + cfg.slippage)
            n = min(want - have, int(cash / (px * (1 + cfg.commission))))
            if n <= 0:
                continue
            cash -= n * px * (1 + cfg.commission)
            buys += n * px
            want = have + n
            trades.append({"date": d.isoformat(), "ticker": t, "side": "BUY", "qty": n, "price": px})
        qty[t] = want
    return cash, {t: q for t, q in qty.items() if q > 0}, buys, sells
