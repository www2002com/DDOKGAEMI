"""A. 시세·호가 (#1~#8, #16~#24) — KRX 일별 시세(pykrx, 대체 FDR) + 토스증권 호가.

KRX 일별 시세는 정규장 기준 데이터다(시간외 단일가·대체거래소 체결은 포함하지 않는다).
따라서 정규장 변수 #5~#8, #17은 #1~#4, #16과 같은 값이다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select

from autopilot_korea.collectors.base import CollectorContext, run_job
from autopilot_korea.db.models import PriceData

JOB = "price"
HISTORY_DAYS = 420  # 252거래일(52주·베타 250일) + 200일 이평을 덮는 달력일
OVERLAP_DAYS = 7  # 증분 수집 시 최근 며칠은 다시 받아 수정치를 반영한다


def _f(x: Any) -> float | None:
    return None if x is None or pd.isna(x) else float(x)


def compute_price_vars(prices: pd.DataFrame) -> dict[str, Any]:
    """prices: 날짜 오름차순, 컬럼 open/high/low/close/volume (기준일 이하만)."""
    out: dict[str, Any] = {k: None for k in ("prev_close", "open", "day_low", "day_high", "volume", "average_volume",
                                              "average_volume_10d", "average_daily_volume_10d")}
    if prices is None or prices.empty:
        return _with_regular(out)
    last = prices.iloc[-1]
    out.update(open=_f(last.get("open")), day_low=_f(last.get("low")), day_high=_f(last.get("high")), volume=_f(last.get("volume")))
    if len(prices) >= 2:
        out["prev_close"] = _f(prices["close"].iloc[-2])
    vol = prices["volume"].dropna()
    if len(vol) >= 20:
        out["average_volume"] = float(vol.iloc[-20:].mean())
    if len(vol) >= 10:
        out["average_volume_10d"] = out["average_daily_volume_10d"] = float(vol.iloc[-10:].mean())
    return _with_regular(out)


def _with_regular(out: dict[str, Any]) -> dict[str, Any]:
    out.update(regular_market_prev_close=out["prev_close"], regular_market_open=out["open"],
               regular_market_day_low=out["day_low"], regular_market_day_high=out["day_high"],
               regular_market_volume=out["volume"])
    return out


def compute_quote_vars(quote: dict[str, Any] | None) -> dict[str, Any]:
    """#21~#24 — 토스증권 최우선 호가(QuoteProvider.best_quote)."""
    q = quote or {}
    return {k: _f(q.get(k)) for k in ("bid", "ask", "bid_size", "ask_size")}


def avg_trading_value(prices: pd.DataFrame, window: int = 20) -> float | None:
    """20일 평균 거래대금. 거래대금이 없으면 종가 × 거래량으로 근사한다."""
    if prices is None or prices.empty:
        return None
    tail = prices.iloc[-window:]
    tv = tail["trading_value"] if "trading_value" in tail else pd.Series(index=tail.index, dtype=float)
    tv = tv.where(tv.notna(), tail["close"] * tail["volume"])
    tv = tv.dropna()
    return float(tv.mean()) if len(tv) else None


def store_prices(session, ticker: str, df: pd.DataFrame) -> int:
    """일별 시세 upsert (ticker, date)."""
    if df is None or df.empty:
        return 0
    dates = list(df.index)
    existing = {r.date: r for r in session.scalars(select(PriceData).where(
        PriceData.ticker == ticker, PriceData.date >= min(dates), PriceData.date <= max(dates))).all()}
    for d, row in df.iterrows():
        values = {c: _f(row.get(c)) for c in ("open", "high", "low", "close", "volume", "trading_value", "market_cap", "shares")}
        if d in existing:
            for k, v in values.items():
                if v is not None:
                    setattr(existing[d], k, v)
        else:
            session.add(PriceData(ticker=ticker, date=d, **values))
    return len(df)


def _start_date(session, ticker: str, as_of: date) -> date:
    last = session.scalars(select(PriceData.date).where(PriceData.ticker == ticker, PriceData.date <= as_of)
                           .order_by(PriceData.date.desc())).first()
    full = as_of - timedelta(days=HISTORY_DAYS)
    return max(full, last - timedelta(days=OVERLAP_DAYS)) if last else full


def _collect_one(ctx: CollectorContext, ticker: str) -> None:
    with ctx.db.session() as s:
        start = _start_date(s, ticker, ctx.as_of)
    df = ctx.market.ohlcv(ticker, start, ctx.as_of)
    if df is None or df.empty:
        raise RuntimeError(f"시세가 비어 있습니다: {ticker} {start}~{ctx.as_of}")
    try:
        foreign = ctx.market.foreign_ownership(ticker, ctx.as_of)
    except Exception:  # noqa: BLE001 — 외국인 보유율은 보조 정보다
        foreign = None
    with ctx.db.session() as s:
        store_prices(s, ticker, df)
        if foreign is not None:
            latest = s.scalars(select(PriceData).where(PriceData.ticker == ticker, PriceData.date <= ctx.as_of)
                               .order_by(PriceData.date.desc())).first()
            if latest:
                latest.foreign_ratio = foreign


def collect(ctx: CollectorContext, tickers: list[str]) -> dict[str, Any]:
    return run_job(ctx, JOB, _collect_one, tickers)
