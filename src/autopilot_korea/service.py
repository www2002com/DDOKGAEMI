"""CLI와 스케줄러가 함께 쓰는 운영 작업 (데이터 수집·유니버스 갱신·정기보고서 감지 수집)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from autopilot_korea.agents.aux_agents import today_kst
from autopilot_korea.db.models import StockMaster


def collector_context(orch: Any, as_of: date | None = None):
    from autopilot_korea.collectors.base import build_context

    return build_context(orch.settings, orch.db, as_of or today_kst(), quotes=orch.quote_provider())


def tracked_tickers(orch: Any) -> list[str]:
    """수집 대상 = 유니버스(stock_master) + 현재 보유 + 현금성 ETF."""
    with orch.db.session() as s:
        universe = set(s.scalars(select(StockMaster.ticker)).all())
    held = set(orch.broker.account().holdings)
    return sorted(universe | held | orch.rules.portfolio.cash_etf_tickers)


def refresh_universe(orch: Any, as_of: date | None = None) -> list[str]:
    from autopilot_korea.collectors.universe import refresh_universe as refresh

    return refresh(collector_context(orch, as_of))


def collect(orch: Any, jobs: list[str], tickers: list[str] | None = None, as_of: date | None = None) -> dict[str, Any]:
    from autopilot_korea.collectors import run_collectors

    ctx = collector_context(orch, as_of)
    with orch.db.session() as s:
        empty = s.scalars(select(StockMaster.ticker)).first() is None
    if empty and not tickers:
        refresh_universe(orch, as_of)
    return run_collectors(ctx, jobs, tickers or tracked_tickers(orch))


def collect_on_new_filings(orch: Any, jobs: list[str], as_of: date | None = None, lookback_days: int = 1) -> dict[str, Any]:
    """정기보고서(사업·반기·분기, DART 공시유형 A)가 새로 나온 종목만 재무·지배구조를 다시 수집한다 (§4.3 분기 갱신)."""
    dart = orch.dart()
    if dart is None:
        return {"skipped": "DART_API_KEY 없음"}
    as_of = as_of or today_kst()
    tracked = set(tracked_tickers(orch))
    filings = dart.list_disclosures(as_of - timedelta(days=lookback_days), as_of, pblntf_ty="A")
    tickers = sorted({f["stock_code"] for f in filings if f.get("stock_code") in tracked})
    if not tickers:
        return {"new_filings": 0}
    return {"new_filings": len(tickers), "result": collect(orch, jobs, tickers, as_of)}
