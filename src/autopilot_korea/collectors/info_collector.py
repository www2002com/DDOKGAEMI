"""F. 기업 기본정보 (#25, #33, #47, #50~#52, #58, #59, #91).

- 결산월: DART 기업개황(company.json acc_mt). DART 키가 없으면 유니버스 갱신 때 넣은 FDR KRX-DESC 결산월을 쓴다.
- 액면분할(#58·#59): DART 공시검색(거래소공시 I)에서 "주식분할결정"을 찾아 원문의 1주당 가액(분할 전 ÷ 분할 후)과
  신주권상장예정일을 파싱한다. 원문 파싱이 안 되면 price_data 상장주식수의 계단식 증가(≥1.5배)로 배수를 추정하고,
  날짜는 그 증가일(없으면 공시 접수일)로 둔다. 결과는 stock_master.corporate_actions에 저장한다.
- #25 시가총액·#47 역산 주식수는 price_data, #52 최근 분기 기준일은 financial_data에서 팩트시트가 계산한다.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select

from autopilot_korea.collectors.base import CollectorContext, corp_code_for, run_job, skip_job
from autopilot_korea.collectors.dividend_collector import parse_date, table_rows
from autopilot_korea.collectors.financials_collector import _div
from autopilot_korea.collectors.sources.dart import filed_date_from_rcept_no, parse_amount
from autopilot_korea.db.models import PriceData, StockMaster

JOB = "info"
SPLIT_LOOKBACK_YEARS = 5


def fiscal_year_ends(as_of: date, settle_month: int | None) -> tuple[date | None, date | None]:
    """(직전 결산일 ≤ 기준일, 차기 결산일 > 기준일)."""
    if not settle_month:
        return None, None
    this = date(as_of.year, settle_month, calendar.monthrange(as_of.year, settle_month)[1])
    last = this if this <= as_of else date(as_of.year - 1, settle_month, calendar.monthrange(as_of.year - 1, settle_month)[1])
    nxt_year = last.year + 1
    return last, date(nxt_year, settle_month, calendar.monthrange(nxt_year, settle_month)[1])


def latest_split(actions: list[dict[str, Any]] | None, as_of: date) -> dict[str, Any] | None:
    """기준일 이전에 접수된 마지막 액면분할."""
    splits = [a for a in actions or [] if a.get("type") == "split" and (a.get("rcept_dt") or "9999") <= as_of.isoformat()]
    return max(splits, key=lambda a: a.get("rcept_dt") or "") if splits else None


def compute_info_vars(as_of: date, settle_month: int | None, market_cap: float | None, close: float | None,
                      most_recent_quarter: date | None, actions: list[dict[str, Any]] | None) -> dict[str, Any]:
    last_fy, next_fy = fiscal_year_ends(as_of, settle_month)
    split = latest_split(actions, as_of)
    return {
        "market_cap": market_cap,
        "currency": "KRW",
        "implied_shares_outstanding": _div(market_cap, close),
        "last_fiscal_year_end": last_fy.isoformat() if last_fy else None,
        "next_fiscal_year_end": next_fy.isoformat() if next_fy else None,
        "most_recent_quarter": most_recent_quarter.isoformat() if most_recent_quarter else None,
        "last_split_factor": split.get("factor") if split else None,
        "last_split_date": split.get("date") if split else None,
        "financial_currency": "KRW",
    }


def parse_split_decision(document: str) -> dict[str, Any]:
    """주식분할결정 원문 → {factor: 분할 전 액면 ÷ 분할 후 액면, date: 신주권상장예정일(없으면 효력발생일)}."""
    out: dict[str, Any] = {"factor": None, "date": None}
    fallback_date = None
    for cells in table_rows(document):
        joined = "".join("".join(cells).split())
        if "1주당가액" in joined and out["factor"] is None:
            nums = [parse_amount(c) for c in cells if parse_amount(c) is not None]
            if len(nums) >= 2 and nums[-1]:
                out["factor"] = nums[-2] / nums[-1]
        elif "신주권상장예정일" in joined:
            out["date"] = parse_date(" ".join(cells))
        elif "효력발생일" in joined and fallback_date is None:
            fallback_date = parse_date(" ".join(cells))
    out["date"] = out["date"] or fallback_date
    return out


def split_from_share_jump(shares: pd.Series, since: date) -> tuple[float | None, date | None]:
    """상장주식수가 하루 만에 1.5배 이상 늘어난 첫 날(since 이후) → (배수, 날짜)."""
    s = shares.dropna()
    s = s[[d >= since - timedelta(days=5) for d in s.index]] if len(s) else s
    ratio = s / s.shift(1)
    jumps = ratio[ratio >= 1.5]
    if jumps.empty:
        return None, None
    return round(float(jumps.iloc[0]), 2), jumps.index[0]


def _collect_splits(ctx: CollectorContext, ticker: str, corp_code: str) -> list[dict[str, Any]]:
    start = ctx.as_of - timedelta(days=365 * SPLIT_LOOKBACK_YEARS)
    items = ctx.dart.list_disclosures(start, ctx.as_of, pblntf_ty="I", corp_code=corp_code)
    actions = []
    for item in sorted((i for i in items if "주식분할결정" in i.get("report_nm", "")), key=lambda i: i["rcept_no"]):
        filed = filed_date_from_rcept_no(item["rcept_no"])
        try:
            parsed = parse_split_decision(ctx.dart.document(item["rcept_no"]))
        except Exception:  # noqa: BLE001 — 원문이 없거나 형식이 달라도 상장주식수로 추정한다
            parsed = {"factor": None, "date": None}
        if parsed["factor"] is None and filed:
            with ctx.db.session() as s:
                rows = s.execute(select(PriceData.date, PriceData.shares).where(PriceData.ticker == ticker, PriceData.date >= filed)
                                 .order_by(PriceData.date)).all()
            factor, jump_date = split_from_share_jump(pd.Series({d: v for d, v in rows}, dtype=float), filed)
            parsed = {"factor": factor, "date": parsed["date"] or jump_date}
        when = parsed["date"] or filed
        when_iso = when.isoformat() if when else None
        actions = [a for a in actions if a.get("date") != when_iso]  # 같은 분할의 정정공시는 덮어쓴다
        actions.append({"type": "split", "rcept_no": item["rcept_no"], "rcept_dt": filed.isoformat() if filed else None,
                        "date": when_iso, "factor": parsed["factor"]})
    return actions


def _collect_one(ctx: CollectorContext, ticker: str) -> None:
    corp_code = corp_code_for(ctx, ticker)
    if not corp_code:
        raise RuntimeError(f"DART 고유번호를 찾지 못했습니다: {ticker}")
    company = ctx.dart.company(corp_code)
    settle = int(company["acc_mt"]) if str(company.get("acc_mt", "")).isdigit() else None
    splits = _collect_splits(ctx, ticker, corp_code)
    with ctx.db.session() as s:
        master = s.get(StockMaster, ticker)
        if master is None:
            return
        if settle:
            master.settle_month = settle
        others = [a for a in master.corporate_actions or [] if a.get("type") != "split"]
        master.corporate_actions = others + splits


def collect(ctx: CollectorContext, tickers: list[str]) -> dict[str, Any]:
    if ctx.dart is None:
        return skip_job(ctx, JOB, "DART_API_KEY 없음 — 결산월은 유니버스(FDR) 값을 쓰고 액면분할은 수집하지 않음", tickers)
    return run_job(ctx, JOB, _collect_one, tickers)
