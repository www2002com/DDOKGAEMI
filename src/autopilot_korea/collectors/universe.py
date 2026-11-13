"""유니버스: KOSPI200 + KOSDAQ150 (§3.3) + 현금성 ETF, 유동성 필터 (§7-4).

KRX 로그인이 없으면 지수 구성종목을 받을 수 없어 시총 상위 200/150 근사를 쓴다(sources/market.py 참조).
근사 여부는 collection_status(job="universe")의 error 칸에 남긴다.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from autopilot_korea.collectors.base import JOB_ROW, CollectorContext, upsert, utcnow
from autopilot_korea.collectors.sources.market import KOSDAQ150, KOSPI200, dept_flags
from autopilot_korea.db.models import CollectionStatus, PriceData, StockMaster
from autopilot_korea.sectors import classify_sector

log = logging.getLogger(__name__)
CASH_SECTOR = "현금성"


def _value(info: pd.Series | None, key: str) -> Any:
    if info is None:
        return None
    v = info.get(key)
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else v


def refresh_universe(ctx: CollectorContext) -> list[str]:
    """지수 구성종목을 stock_master에 반영하고 주식 티커 목록(현금성 ETF 제외)을 돌려준다."""
    k200, approx_k = ctx.market.index_constituents(KOSPI200, ctx.as_of)
    kq150, approx_q = ctx.market.index_constituents(KOSDAQ150, ctx.as_of)
    listing = ctx.market.listing(ctx.as_of).drop_duplicates("ticker").set_index("ticker")
    corp_codes = ctx.dart.corp_codes() if ctx.dart is not None else {}
    members = {t: (t in set(k200), t in set(kq150)) for t in set(k200) | set(kq150)}
    with ctx.db.session() as s:
        for m in s.scalars(select(StockMaster).where(StockMaster.in_kospi200 | StockMaster.in_kosdaq150)).all():
            if m.ticker not in members:
                m.in_kospi200 = m.in_kosdaq150 = False
        for ticker, (in_k, in_q) in members.items():
            info = listing.loc[ticker] if ticker in listing.index else None
            industry = _value(info, "industry")
            master = s.get(StockMaster, ticker)
            if master is None:
                master = StockMaster(ticker=ticker, name=ticker, market="KOSPI", risk_flags=[], corporate_actions=[])
                s.add(master)
            master.name = str(_value(info, "name") or master.name)[:100]
            master.market = "KOSPI" if in_k else "KOSDAQ"
            master.corp_code = corp_codes.get(ticker) or master.corp_code
            master.industry = str(industry)[:200] if industry else master.industry
            master.sector = classify_sector(ticker, master.industry, ctx.settings.sectors)
            settle = _value(info, "settle_month")
            master.settle_month = master.settle_month or (int(settle) if settle else None)
            master.in_kospi200, master.in_kosdaq150, master.is_cash_etf = in_k, in_q, False
            master.risk_flags = dept_flags(_value(info, "dept"))  # KOREA_REGULATION 게이트 입력 (관리종목 등)
        for etf in ctx.settings.constraints.portfolio.cash_etfs:
            upsert(s, StockMaster, {"ticker": etf.ticker}, {"name": etf.name, "market": "ETF", "is_cash_etf": True,
                                                            "sector": CASH_SECTOR, "in_kospi200": False, "in_kosdaq150": False})
        status = upsert(s, CollectionStatus, {"job": "universe", "ticker": JOB_ROW}, {})
        status.last_attempt_at = status.last_success_at = utcnow()
        status.status, status.consecutive_failures = "ok", 0
        status.error = "지수 구성종목 근사(시총 상위 200/150, KRX 로그인 없음)" if (approx_k or approx_q) else None
    if approx_k or approx_q:
        log.warning("지수 구성종목을 시총 상위로 근사했습니다 (KOSPI200=%s, KOSDAQ150=%s)", approx_k, approx_q)
    return sorted(members)


def liquid_universe(session: Session, as_of: date, exclude_bottom_pct: float) -> list[str]:
    """KOSPI200 + KOSDAQ150 중 20일 평균 거래대금 하위 exclude_bottom_pct를 뺀 티커 (거래대금 큰 순).

    거래대금이 저장되지 않은 날은 종가 × 거래량으로 근사한다. 기준일 이전 시세가 없는 종목은 유동성을 확인할 수 없어 제외한다.
    """
    tickers = list(session.scalars(select(StockMaster.ticker).where(
        (StockMaster.in_kospi200 | StockMaster.in_kosdaq150) & StockMaster.is_cash_etf.is_(False))).all())
    if not tickers:
        return []
    rows = session.execute(select(PriceData.ticker, PriceData.date, PriceData.close, PriceData.volume, PriceData.trading_value)
                           .where(PriceData.ticker.in_(tickers), PriceData.date <= as_of,
                                  PriceData.date > as_of - timedelta(days=60))).all()
    if not rows:
        return []
    df = pd.DataFrame(rows, columns=["ticker", "date", "close", "volume", "trading_value"]).sort_values("date")
    df["tv"] = df["trading_value"].astype(float).fillna(df["close"].astype(float) * df["volume"].astype(float))
    adv = df.dropna(subset=["tv"]).groupby("ticker")["tv"].apply(lambda x: x.iloc[-20:].mean()).sort_values(ascending=False)
    n_exclude = int(len(adv) * exclude_bottom_pct)
    return list(adv.index[:len(adv) - n_exclude])
