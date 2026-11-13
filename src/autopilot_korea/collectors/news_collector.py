"""뉴스·공시 수집 (§4.1) — 에이전트에는 최근 7일 항목만 유효 정보로 준다(§6 news_window_days).

- 뉴스: 네이버 검색 API(키가 있을 때, 회사명으로 최신순) 또는 m.stock.naver.com 종목 뉴스(sources/naver.py 참조).
- 공시: DART 공시검색에서 해당 회사의 최근 7일 공시를 kind="disclosure"로 함께 저장한다.
published_at은 UTC로 저장하고, 기간 판정은 한국 시간 날짜 기준이다.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from autopilot_korea.collectors.base import CollectorContext, as_utc, corp_code_for, run_job, upsert
from autopilot_korea.collectors.sources import naver
from autopilot_korea.collectors.sources.dart import parse_dart_date
from autopilot_korea.db.models import NewsItem, StockMaster

JOB = "news"
KST = naver.KST
DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


def _kst_midnight(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=KST)


def recent_news(session: Session, ticker: str, as_of: date, days: int = 7) -> list[dict[str, Any]]:
    """published_at(한국 시간 날짜)이 (as_of − days, as_of]인 뉴스·공시, 최신순."""
    start = as_utc(_kst_midnight(as_of - timedelta(days=days - 1)))
    end = as_utc(_kst_midnight(as_of + timedelta(days=1)))
    rows = session.scalars(select(NewsItem).where(NewsItem.ticker == ticker, NewsItem.published_at >= start,
                                                  NewsItem.published_at < end).order_by(NewsItem.published_at.desc())).all()
    out = []
    for r in rows:
        ts = as_utc(r.published_at)
        if start <= ts < end:
            out.append({"title": r.title, "source": r.source, "url": r.url,
                        "published_at": ts.astimezone(KST).isoformat(), "kind": r.kind})
    return out


def disclosure_items(disclosures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in disclosures:
        when = parse_dart_date(d.get("rcept_dt"))
        if when is None:
            continue
        out.append({"title": d.get("report_nm"), "source": "DART", "url": DART_VIEWER.format(rcept_no=d["rcept_no"]),
                    "published_at": _kst_midnight(when), "kind": "disclosure"})
    return out


def store_items(session: Session, ticker: str, items: list[dict[str, Any]]) -> int:
    n = 0
    for it in items:
        if not it.get("url") or not it.get("title"):
            continue
        upsert(session, NewsItem, {"ticker": ticker, "url": str(it["url"])[:500]},
               {"title": it["title"], "source": (it.get("source") or None) and str(it["source"])[:100],
                "published_at": as_utc(it["published_at"]), "kind": it.get("kind", "news")})
        n += 1
    return n


def _collect_one(ctx: CollectorContext, ticker: str) -> None:
    s = ctx.settings
    with ctx.db.session() as session:
        master = session.get(StockMaster, ticker)
        name = master.name if master else ticker
    if s.naver_client_id and s.naver_client_secret:
        items = naver.search_news(name, s.naver_client_id, s.naver_client_secret)
    else:
        items = naver.fetch_stock_news(ticker)
    if ctx.dart is not None:
        corp_code = corp_code_for(ctx, ticker)
        if corp_code:
            items += disclosure_items(ctx.dart.list_disclosures(ctx.as_of - timedelta(days=7), ctx.as_of, corp_code=corp_code))
    with ctx.db.session() as session:
        store_items(session, ticker, items)


def collect(ctx: CollectorContext, tickers: list[str]) -> dict[str, Any]:
    return run_job(ctx, JOB, _collect_one, tickers)
