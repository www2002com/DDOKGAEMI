"""G. 수익성·현금흐름 (#35, #53~#56, #82~#90) — DART fnlttSinglAcntAll.

정기보고서 1건 = financial_data 1행. 손익·현금흐름은 연초 누적(YTD), 재무상태표는 기말 잔액으로 items에 저장하고
filed_date(접수일)를 함께 둔다 — 팩트시트는 filed_date ≤ 기준일인 보고서만 쓴다(§14 룩어헤드 차단).

TTM 규칙: 최근 보고서가 사업보고서(Q4)면 그 값, 아니면 최근 YTD + 직전 사업연도 연간 − 직전 사업연도 같은 분기 YTD.
분기 단독값 = 해당 분기 YTD − 같은 해 직전 분기 YTD (1분기는 YTD 그대로).
별도재무제표(OFS)에는 지배주주 구분이 없으므로 지배주주 순이익·자본이 없으면 전체 순이익·자본총계로 대신한다.
은행·보험처럼 매출·유동자산 줄이 없는 회사는 해당 값이 None이며 예외를 내지 않는다.

분기 말일은 결산월 m을 기준으로 bsns_year 해의 m월 말을 회계연도 말로 보고 3개월씩 거슬러 계산한다
(12월 결산은 3·6·9·12월 말; 비12월 결산 법인은 근사).
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select

from autopilot_korea.collectors.base import CollectorContext, corp_code_for, run_job, skip_job, upsert
from autopilot_korea.collectors.sources.dart import REPORT_CODES, filed_date_from_rcept_no, parse_amount
from autopilot_korea.db.models import FinancialData, StockMaster

JOB = "financials"
MAPPING_PATH = Path(__file__).with_name("account_mapping.json")
FLOW_KEYS = ("revenue", "cost_of_sales", "gross_profit", "operating_income", "net_income", "net_income_controlling",
             "operating_cash_flow", "investing_cash_flow", "capex", "depreciation_amortization")
DEBT_KEYS = ("short_term_borrowings", "current_portion_long_term_debt", "bonds", "long_term_borrowings", "lease_liabilities")
FALLBACK = {"net_income_controlling": "net_income", "equity_controlling": "total_equity"}
FILING_GRACE_DAYS = 14  # 분기 말 직후에는 보고서가 없으므로 이 기간이 지난 분기만 조회한다


@lru_cache(maxsize=1)
def account_mapping() -> dict[str, dict[str, Any]]:
    data = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _norm_name(name: str | None) -> str:
    s = re.sub(r"\s+", "", name or "")
    return re.sub(r"^(\(?[0-9IVXⅠ-Ⅹ]+[.)]|\([0-9]+\))", "", s)


def _norm_id(account_id: str | None) -> str:
    s = (account_id or "").strip()
    return "ifrs-full_" + s[len("ifrs_"):] if s.startswith("ifrs_") else s


def _line_value(row: dict[str, Any]) -> float | None:
    """손익(IS/CIS)은 누적(thstrm_add_amount)이 있으면 그것, 없으면 당기금액. 재무상태표·현금흐름표는 당기금액(현금흐름은 누적)."""
    if row.get("sj_div") in ("IS", "CIS"):
        added = parse_amount(row.get("thstrm_add_amount"))
        if added is not None:
            return added
    return parse_amount(row.get("thstrm_amount"))


def normalize_statement(rows: list[dict[str, Any]], mapping: dict[str, dict[str, Any]] | None = None) -> dict[str, float | None]:
    """fnlttSinglAcntAll 행 목록 → 정규화 키별 값 (account_mapping.json)."""
    mapping = mapping or account_mapping()
    lines = [(r.get("sj_div"), _norm_id(r.get("account_id")), _norm_name(r.get("account_nm")), _line_value(r)) for r in rows]
    out: dict[str, float | None] = {}
    for key, spec in mapping.items():
        ids = set(spec.get("ids", []))
        names = [_norm_name(n) for n in spec.get("names", [])]
        value: float | None = None
        for sj in spec.get("sj", []):
            sj_lines = [ln for ln in lines if ln[0] == sj and ln[3] is not None]
            if spec.get("sum"):
                matched = [ln[3] for ln in sj_lines if ln[1] in ids or ln[2] in names]
                value = sum(matched) if matched else None
            else:
                by_id = next((ln[3] for i in spec.get("ids", []) for ln in sj_lines if ln[1] == i), None)
                value = by_id if by_id is not None else next((ln[3] for n in names for ln in sj_lines if ln[2] == n), None)
            if value is not None:
                break
        out[key] = value
    return out


def total_debt(items: dict[str, Any]) -> float | None:
    """단기차입 + 유동성장기부채 + 사채 + 장기차입 + 리스부채. 하나도 없으면 None."""
    parts = [items.get(k) for k in DEBT_KEYS if items.get(k) is not None]
    return float(sum(parts)) if parts else None


def _month_end(y: int, m: int) -> date:
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, calendar.monthrange(y, m)[1])


def quarter_end(bsns_year: int, q: int, settle_month: int | None = 12) -> date:
    m = settle_month or 12
    return _month_end(bsns_year, m - 3 * (4 - q))


# ── TTM 계산 (순수 함수) ────────────────────────────────────────────────

def _item(r: dict[str, Any] | None, key: str) -> float | None:
    if r is None:
        return None
    items = r.get("items") or {}
    value = items.get(key)
    if value is None and key in FALLBACK:
        value = items.get(FALLBACK[key])
    return value


def ytd(by: dict[tuple[int, int], dict], year: int, q: int, key: str) -> float | None:
    return _item(by.get((year, q)), key)


def discrete_quarter(by: dict[tuple[int, int], dict], year: int, q: int, key: str) -> float | None:
    cur = ytd(by, year, q, key)
    if cur is None or q == 1:
        return cur
    prev = ytd(by, year, q - 1, key)
    return None if prev is None else cur - prev


def ttm_at(by: dict[tuple[int, int], dict], year: int, q: int, key: str) -> float | None:
    cur = ytd(by, year, q, key)
    if cur is None or q == 4:
        return cur
    fy, same = ytd(by, year - 1, 4, key), ytd(by, year - 1, q, key)
    if fy is None or same is None:
        return None
    return cur + fy - same


def _div(a: float | None, b: float | None, positive_denominator: bool = True) -> float | None:
    if a is None or b is None or b == 0 or (positive_denominator and b < 0):
        return None
    return a / b


def cagr(latest: float | None, base: float | None, years: int = 3) -> float | None:
    """base ≤ 0이면 None. latest < 0도 None, latest == 0이면 −100%."""
    if latest is None or base is None or base <= 0 or latest < 0:
        return None
    return (latest / base) ** (1 / years) - 1


def report_dict(row: FinancialData) -> dict[str, Any]:
    return {"year": int(row.quarter[:4]), "q": int(row.quarter[-1]), "items": dict(row.items or {}),
            "period_end": row.period_end, "filed_date": row.filed_date, "fs_div": row.fs_div}


def summarize_financials(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """보고서 목록(룩어헤드 필터 후) → TTM·잔액 요약. 최근 보고서와 같은 fs_div만 쓴다."""
    if not reports:
        return {}
    latest = max(reports, key=lambda r: (r["year"], r["q"], r["fs_div"] == "CFS"))
    by = {(r["year"], r["q"]): r for r in reports if r["fs_div"] == latest["fs_div"]}
    y, q = latest["year"], latest["q"]
    s: dict[str, Any] = {"year": y, "q": q, "period_end": latest.get("period_end"), "filed_date": latest.get("filed_date"),
                         "fs_div": latest["fs_div"]}
    for key in FLOW_KEYS:
        s[f"ttm_{key}"] = ttm_at(by, y, q, key)
    if s["ttm_gross_profit"] is None and s["ttm_revenue"] is not None and s["ttm_cost_of_sales"] is not None:
        s["ttm_gross_profit"] = s["ttm_revenue"] - s["ttm_cost_of_sales"]
    if s["ttm_capex"] is not None:
        s["ttm_capex"] = abs(s["ttm_capex"])  # 회사마다 부호 표기가 달라 크기만 쓴다
    op, da = s["ttm_operating_income"], s["ttm_depreciation_amortization"]
    s["ttm_ebitda"] = op + da if op is not None and da is not None else None
    s["ttm_ni_ctrl_prev"] = ttm_at(by, y - 1, q, "net_income_controlling")
    s["q_ni_ctrl"] = discrete_quarter(by, y, q, "net_income_controlling")
    s["q_ni_ctrl_prev_year"] = discrete_quarter(by, y - 1, q, "net_income_controlling")
    for key in ("total_assets", "total_liabilities", "total_equity", "equity_controlling", "current_assets",
                "current_liabilities", "inventories", "cash"):
        s[key] = _item(latest, key)
    s["total_debt"] = total_debt(latest.get("items") or {})
    annual_years = sorted(yy for (yy, qq) in by if qq == 4)
    if annual_years:
        fy = annual_years[-1]
        s["annual_year"] = fy
        s["cagr_net_income"] = cagr(ytd(by, fy, 4, "net_income_controlling"), ytd(by, fy - 3, 4, "net_income_controlling"))
        s["cagr_revenue"] = cagr(ytd(by, fy, 4, "revenue"), ytd(by, fy - 3, 4, "revenue"))
    return s


def compute_financial_vars(summary: dict[str, Any], shares: float | None, forward_eps: float | None) -> dict[str, Any]:
    """G 카테고리 변수. 비율은 소수(0.123 = 12.3%), 금액은 원."""
    s = summary or {}
    rev, ni = s.get("ttm_revenue"), s.get("ttm_net_income_controlling")
    ni_total = s.get("ttm_net_income") if s.get("ttm_net_income") is not None else ni
    ocf, capex, icf = s.get("ttm_operating_cash_flow"), s.get("ttm_capex"), s.get("ttm_investing_cash_flow")
    if ocf is not None and capex is not None:
        fcf = ocf - capex
    elif ocf is not None and icf is not None:
        fcf = ocf + icf  # 유형자산 취득 줄이 없으면 투자활동현금흐름 전체로 대신한다
    else:
        fcf = None
    q_now, q_prev = s.get("q_ni_ctrl"), s.get("q_ni_ctrl_prev_year")
    equity = s.get("equity_controlling")
    return {
        "profit_margins": _div(ni, rev),
        "earnings_quarterly_growth": (q_now / q_prev - 1) if q_now is not None and q_prev and q_prev > 0 else None,
        "net_income_to_common": ni,
        "trailing_eps": _div(ni, shares),
        "forward_eps": forward_eps,
        "return_on_assets": _div(ni_total, s.get("total_assets")),
        "return_on_equity": _div(ni, equity),
        "free_cashflow": fcf,
        "operating_cashflow": ocf,
        "earnings_growth": s.get("cagr_net_income"),
        "revenue_growth": s.get("cagr_revenue"),
        "gross_margins": _div(s.get("ttm_gross_profit"), rev),
        "ebitda_margins": _div(s.get("ttm_ebitda"), rev),
        "operating_margins": _div(s.get("ttm_operating_income"), rev),
    }


# ── 수집 ────────────────────────────────────────────────────────────────

def fetch_reports(ctx: CollectorContext, ticker: str) -> int:
    """아직 저장되지 않은 정기보고서를 받아 financial_data에 넣는다. 연결(CFS)이 없으면 별도(OFS). 받은 건수를 돌려준다."""
    corp_code = corp_code_for(ctx, ticker)
    if not corp_code:
        raise RuntimeError(f"DART 고유번호를 찾지 못했습니다: {ticker}")
    with ctx.db.session() as s:
        master = s.get(StockMaster, ticker)
        settle = master.settle_month if master and master.settle_month else 12
        stored = set(s.scalars(select(FinancialData.quarter).where(FinancialData.ticker == ticker)).all())
    added = 0
    for year in range(ctx.as_of.year - 4, ctx.as_of.year + 1):
        for q, code in REPORT_CODES.items():
            label = f"{year}Q{q}"
            period_end = quarter_end(year, q, settle)
            # 3년 CAGR에는 사업보고서 4년치, TTM·전년 TTM에는 최근 3년의 분기 보고서가 필요하다.
            if label in stored or period_end + timedelta(days=FILING_GRACE_DAYS) > ctx.as_of or (q < 4 and year < ctx.as_of.year - 2):
                continue
            fs_div, rows = "CFS", ctx.dart.financial_statements(corp_code, year, code, "CFS")
            if not rows:
                fs_div, rows = "OFS", ctx.dart.financial_statements(corp_code, year, code, "OFS")
            if not rows:
                continue
            items = normalize_statement(rows)
            ni = items.get("net_income_controlling") if items.get("net_income_controlling") is not None else items.get("net_income")
            op, da = items.get("operating_income"), items.get("depreciation_amortization")
            with ctx.db.session() as s:
                upsert(s, FinancialData, {"ticker": ticker, "quarter": label, "fs_div": fs_div}, {
                    "period_end": period_end, "report_code": code, "filed_date": filed_date_from_rcept_no(rows[0].get("rcept_no")),
                    "revenue": items.get("revenue"), "net_income": ni,
                    "ebitda": op + da if op is not None and da is not None else None,
                    "total_debt": total_debt(items), "items": items,
                })
            added += 1
    return added


def collect(ctx: CollectorContext, tickers: list[str]) -> dict[str, Any]:
    if ctx.dart is None:
        return skip_job(ctx, JOB, "DART_API_KEY 없음", tickers)
    return run_job(ctx, JOB, fetch_reports, tickers)
