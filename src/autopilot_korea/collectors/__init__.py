"""데이터 레이어 (§4): 97개 변수 수집 모듈 11종 + 매크로, 유니버스, 팩트시트.

작업 이름은 config/schedule.yaml의 collectors.*.jobs와 같다. 각 수집기는 collect(ctx, tickers)로
원천에서 받아 DB에 upsert하고, 결과 요약 dict(job, status, ok, failed, stale, skipped …)를 돌려준다.
"""

from __future__ import annotations

from typing import Any, Callable

from autopilot_korea.collectors import (balance_sheet_collector, consensus_collector, dividend_collector, financials_collector,
                                        governance_collector, info_collector, macro_collector, momentum_collector, news_collector,
                                        price_collector, short_interest_collector, valuation_collector)
from autopilot_korea.collectors.base import CollectorContext, QuoteProvider, build_context, run_job

COLLECTORS: dict[str, Callable[[CollectorContext, list[str]], dict[str, Any]]] = {
    "price": price_collector.collect,
    "dividend": dividend_collector.collect,
    "valuation": valuation_collector.collect,
    "momentum": momentum_collector.collect,
    "short_interest": short_interest_collector.collect,
    "info": info_collector.collect,
    "financials": financials_collector.collect,
    "balance_sheet": balance_sheet_collector.collect,
    "consensus": consensus_collector.collect,
    "governance": governance_collector.collect,
    "news": news_collector.collect,
    "macro": macro_collector.collect,
}


def run_collectors(ctx: CollectorContext, jobs: list[str], tickers: list[str]) -> dict[str, dict[str, Any]]:
    """작업을 주어진 순서대로 실행한다. 한 작업의 실패가 다음 작업을 막지 않는다."""
    unknown = [j for j in jobs if j not in COLLECTORS]
    if unknown:
        raise ValueError(f"알 수 없는 수집 작업: {unknown} (가능: {sorted(COLLECTORS)})")
    results: dict[str, dict[str, Any]] = {}
    for job in jobs:
        try:
            results[job] = COLLECTORS[job](ctx, list(tickers))
        except Exception as exc:  # noqa: BLE001 — 작업 단위 격리 (§15)
            results[job] = {"job": job, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
    return results


__all__ = ["COLLECTORS", "CollectorContext", "QuoteProvider", "build_context", "run_collectors", "run_job"]
