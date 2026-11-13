"""C. 밸류에이션 (#14, #15, #28, #34, #48, #49, #57, #60, #61, #92).

밸류에이션은 시세(price_data)·재무(financial_data)·컨센서스(consensus_data)로부터 계산되는 파생값이라
별도 원천이 없다. 주 1회 작업은 저장된 원천으로 팩트시트를 다시 계산해 factor_snapshot에 남긴다.
"""

from __future__ import annotations

from typing import Any

from autopilot_korea.collectors.base import CollectorContext, run_job
from autopilot_korea.collectors.financials_collector import _div

JOB = "valuation"


def _positive(x: float | None) -> float | None:
    return x if x is not None and x > 0 else None


def compute_valuation_vars(summary: dict[str, Any], market_cap: float | None, close: float | None,
                           shares: float | None, forward_eps: float | None) -> dict[str, Any]:
    s = summary or {}
    ni, ni_prev = s.get("ttm_net_income_controlling"), s.get("ttm_ni_ctrl_prev")
    rev, ebitda, cash, debt = s.get("ttm_revenue"), s.get("ttm_ebitda"), s.get("cash"), s.get("total_debt")
    trailing_pe = _div(market_cap, _positive(ni))
    forward_pe = _div(close, _positive(forward_eps))
    # EV = 시총 + 총차입 − 현금. 차입금 줄이 하나도 없으면 무차입으로 본다. 현금 줄이 없으면(금융업 등) 계산하지 않는다.
    ev = market_cap + (debt or 0.0) - cash if market_cap is not None and cash is not None else None
    bps = _div(s.get("equity_controlling"), shares)
    trailing_eps = _div(ni, shares)
    eps_growth = (forward_eps / trailing_eps - 1) if forward_eps is not None and trailing_eps and trailing_eps > 0 else None
    ni_growth = (ni / ni_prev - 1) if ni is not None and ni_prev and ni_prev > 0 else None
    return {
        "trailing_pe": trailing_pe,
        "forward_pe": forward_pe,
        "price_to_sales_ttm": _div(market_cap, _positive(rev)),
        "enterprise_value": ev,
        "book_value_per_share": bps,
        "price_to_book": _div(close, _positive(bps)),
        "peg_ratio": _div(forward_pe, 100 * eps_growth) if eps_growth and eps_growth > 0 else None,
        "enterprise_to_revenue": _div(ev, _positive(rev)),
        "enterprise_to_ebitda": _div(ev, _positive(ebitda)),
        "trailing_peg_ratio": _div(trailing_pe, 100 * ni_growth) if ni_growth and ni_growth > 0 else None,
    }


def _snapshot(ctx: CollectorContext, ticker: str) -> None:
    from autopilot_korea.collectors.factsheet import build_factsheet, persist_factsheet  # 순환 import 방지

    with ctx.db.session() as s:
        persist_factsheet(s, build_factsheet(s, ticker, ctx.as_of))


def collect(ctx: CollectorContext, tickers: list[str]) -> dict[str, Any]:
    return run_job(ctx, JOB, _snapshot, tickers)
