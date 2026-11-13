"""H. 재무안정성 (#73~#81) — DART fnlttSinglAcntAll 재무상태표 + TTM.

재무상태표 항목은 financials_collector가 같은 보고서에서 함께 저장한다. 이 작업은 같은 수집 함수를 다시 불러
빠진 보고서만 채운다(이미 저장된 보고서는 건너뛰므로 financials 직후에 돌면 DART 호출이 거의 없다).
"""

from __future__ import annotations

from typing import Any

from autopilot_korea.collectors.base import CollectorContext, run_job, skip_job
from autopilot_korea.collectors.financials_collector import _div, fetch_reports

JOB = "balance_sheet"


def compute_balance_vars(summary: dict[str, Any], shares: float | None) -> dict[str, Any]:
    """H 카테고리 변수. #80 부채비율은 한국식(부채총계 ÷ 자본총계)."""
    s = summary or {}
    ca, cl = s.get("current_assets"), s.get("current_liabilities")
    inventories = s.get("inventories") or 0.0  # 재고자산 줄이 없는 회사(서비스업)는 0으로 본다
    return {
        "total_cash": s.get("cash"),
        "total_cash_per_share": _div(s.get("cash"), shares),
        "ebitda": s.get("ttm_ebitda"),
        "total_debt": s.get("total_debt"),
        "quick_ratio": _div(ca - inventories, cl) if ca is not None else None,
        "current_ratio": _div(ca, cl),
        "total_revenue": s.get("ttm_revenue"),
        "debt_to_equity": _div(s.get("total_liabilities"), s.get("total_equity")),
        "revenue_per_share": _div(s.get("ttm_revenue"), shares),
    }


def collect(ctx: CollectorContext, tickers: list[str]) -> dict[str, Any]:
    if ctx.dart is None:
        return skip_job(ctx, JOB, "DART_API_KEY 없음", tickers)
    return run_job(ctx, JOB, fetch_reports, tickers)
