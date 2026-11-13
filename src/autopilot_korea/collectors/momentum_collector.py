"""D. 모멘텀·기술지표 (#13, #26, #27, #29, #30, #62, #63).

종목 지표는 price_data로 팩트시트 생성 시 계산한다. 이 작업은 벤치마크인 KOSPI200 지수 일별 종가를
price_data에 ticker="KOSPI200"으로 저장한다(종목 티커와 겹치지 않는 가상 티커).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select

from autopilot_korea.collectors.base import CollectorContext, run_job
from autopilot_korea.collectors.price_collector import HISTORY_DAYS, OVERLAP_DAYS, store_prices
from autopilot_korea.db.models import PriceData

JOB = "momentum"
BENCHMARK_TICKER = "KOSPI200"
BETA_WINDOW, BETA_MIN_OBS = 250, 120
YEAR_DAYS = 252


def compute_beta(stock_close: pd.Series, bench_close: pd.Series) -> float | None:
    """최근 250개 일별 수익률의 cov/var (공통 거래일 기준, 최소 120개)."""
    joined = pd.concat([stock_close.rename("s"), bench_close.rename("b")], axis=1, join="inner").dropna()
    rets = joined.pct_change(fill_method=None).dropna().iloc[-BETA_WINDOW:]
    if len(rets) < BETA_MIN_OBS:
        return None
    var = rets["b"].var()
    return float(rets["s"].cov(rets["b"]) / var) if var else None


def change_52w(close: pd.Series) -> float | None:
    """(현재가 − 252거래일 전 종가) ÷ 252거래일 전 종가."""
    close = close.dropna()
    if len(close) <= YEAR_DAYS:
        return None
    base = close.iloc[-YEAR_DAYS - 1]
    return float(close.iloc[-1] / base - 1) if base else None


def compute_momentum_vars(prices: pd.DataFrame, bench_close: pd.Series | None) -> dict[str, Any]:
    out: dict[str, Any] = {k: None for k in ("beta", "fifty_two_week_low", "fifty_two_week_high", "fifty_day_average",
                                              "two_hundred_day_average", "fifty_two_week_change", "benchmark_52_week_change")}
    if bench_close is not None and len(bench_close):
        out["benchmark_52_week_change"] = change_52w(bench_close)
    if prices is None or prices.empty:
        return out
    close = prices["close"].dropna()
    year = prices.iloc[-YEAR_DAYS:]
    low = year["low"].where(year["low"].notna(), year["close"]) if "low" in year else year["close"]
    high = year["high"].where(year["high"].notna(), year["close"]) if "high" in year else year["close"]
    out.update(
        beta=compute_beta(close, bench_close) if bench_close is not None else None,
        fifty_two_week_low=float(low.min()) if low.notna().any() else None,
        fifty_two_week_high=float(high.max()) if high.notna().any() else None,
        fifty_day_average=float(close.iloc[-50:].mean()) if len(close) >= 50 else None,
        two_hundred_day_average=float(close.iloc[-200:].mean()) if len(close) >= 200 else None,
        fifty_two_week_change=change_52w(close),
    )
    return out


def _collect_benchmark(ctx: CollectorContext, _: str) -> None:
    with ctx.db.session() as s:
        last = s.scalars(select(PriceData.date).where(PriceData.ticker == BENCHMARK_TICKER, PriceData.date <= ctx.as_of)
                         .order_by(PriceData.date.desc())).first()
    full = ctx.as_of - timedelta(days=HISTORY_DAYS)
    start = max(full, last - timedelta(days=OVERLAP_DAYS)) if last else full
    closes = ctx.market.index_closes(start, ctx.as_of)
    if closes is None or closes.empty:
        raise RuntimeError(f"KOSPI200 지수 시세가 비어 있습니다: {start}~{ctx.as_of}")
    with ctx.db.session() as s:
        store_prices(s, BENCHMARK_TICKER, pd.DataFrame({"close": closes}))


def collect(ctx: CollectorContext, tickers: list[str]) -> dict[str, Any]:
    """종목 목록과 무관하게 벤치마크만 수집한다 (종목 지표는 price 작업의 데이터로 계산)."""
    return run_job(ctx, JOB, _collect_benchmark, [BENCHMARK_TICKER])
