"""B. 배당 (#9~#12, #31, #32, #64, #65) — DART.

dividend_data에는 두 종류의 행이 있다.
- 연간 요약 행(ex_date가 None): 사업보고서 alotMatter의 주당 현금배당금(보통주, 연간 합계)·현금배당수익률·현금배당성향.
  보고서 하나가 당기·전기·전전기 3년치를 주므로, 연도마다 그 해 사업보고서의 당기 값을 우선 저장하고(접수일 정확)
  빠진 연도만 이후 보고서의 전기·전전기 값으로 채운다(접수일은 그 보고서 것 — 늦게 잡히므로 룩어헤드에 안전).
- 배당 이벤트 행(ex_date = 배당기준일): 거래소 공시 "현금ㆍ현물배당결정" 원문(document.xml)을 파싱한
  1주당 배당금(보통주식)·배당금지급 예정일자. 분기·중간 배당을 잡기 위한 것이다.

변수 규칙: #9 = 최근 사업연도 연간 DPS, #10 = #9 ÷ 종가, #11 = 현금배당성향, #12 = 최근 5개 사업연도 현금배당수익률 평균,
#31 = 배당기준일이 최근 365일 안인 이벤트 DPS 합(이벤트가 없으면 #9), #32 = #31 ÷ 종가,
#64/#65 = 기준일 이전 마지막 배당 이벤트의 DPS와 지급일(지급일이 없으면 배당기준일).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from bs4 import BeautifulSoup
from sqlalchemy import select

from autopilot_korea.collectors.base import CollectorContext, corp_code_for, run_job, skip_job, upsert
from autopilot_korea.collectors.sources.dart import filed_date_from_rcept_no, parse_amount
from autopilot_korea.db.models import DividendData

JOB = "dividend"
DATE_RE = re.compile(r"(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})")
EVENT_LOOKBACK_DAYS = 400
ANNUAL_YEARS = 6


def _clean(text: Any) -> str:
    return "".join(str(text or "").split())


def parse_date(text: str | None) -> date | None:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def table_rows(document: str) -> list[list[str]]:
    """공시 원문(HTML 또는 DART XML의 TR/TD/TE/TU)을 표 행 목록으로."""
    soup = BeautifulSoup(document, "lxml")
    rows = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th", "te", "tu"])]
        if cells:
            rows.append(cells)
    return rows


def _common_value(cells: list[str]) -> float | None:
    """["3. 1주당 배당금(원)", "보통주식", "4,601"] → 4601. 보통주 표시가 없으면 첫 숫자."""
    for i, c in enumerate(cells[:-1]):
        if "보통" in c:
            return parse_amount(cells[i + 1])
    nums = [parse_amount(c) for c in cells[1:] if parse_amount(c) is not None]
    return nums[0] if nums else None


def parse_dividend_decision(document: str) -> dict[str, Any]:
    """현금ㆍ현물배당결정 원문 → {kind, dps, yield_pct, record_date, pay_date}."""
    out: dict[str, Any] = {"kind": None, "dps": None, "yield_pct": None, "record_date": None, "pay_date": None}
    for cells in table_rows(document):
        label = _clean(cells[0])
        if "배당구분" in label and len(cells) > 1:
            out["kind"] = cells[1]
        elif "1주당배당금" in label:
            out["dps"] = _common_value(cells)
        elif "시가배당" in label:
            out["yield_pct"] = _common_value(cells)
        elif "배당기준일" in label:
            out["record_date"] = parse_date(" ".join(cells[1:]))
        elif "배당금지급" in label:
            out["pay_date"] = parse_date(" ".join(cells[1:]))
    return out


def parse_alot_matter(rows: list[dict[str, Any]], bsns_year: int) -> dict[int, dict[str, float | None]]:
    """alotMatter 응답 → {사업연도: {dps, yield_pct, payout_pct}} (당기·전기·전전기)."""
    out: dict[int, dict[str, float | None]] = {}
    for offset, col in ((0, "thstrm"), (1, "frmtrm"), (2, "lwfr")):
        vals: dict[str, float | None] = {"dps": None, "yield_pct": None, "payout_pct": None}
        for r in rows:
            se, knd = _clean(r.get("se")), str(r.get("stock_knd") or "")
            if "주당현금배당금" in se and ("보통" in knd or vals["dps"] is None):
                vals["dps"] = parse_amount(r.get(col))
            elif "현금배당수익률" in se and ("보통" in knd or vals["yield_pct"] is None):
                vals["yield_pct"] = parse_amount(r.get(col))
            elif "현금배당성향" in se and vals["payout_pct"] is None:
                vals["payout_pct"] = parse_amount(r.get(col))
        if any(v is not None for v in vals.values()):
            out[bsns_year - offset] = vals
    return out


def compute_dividend_vars(rows: list[dict[str, Any]], close: float | None, as_of: date) -> dict[str, Any]:
    """rows: dividend_data 행 dict (filed_date ≤ 기준일로 이미 거른 것)."""
    annual = sorted((r for r in rows if r.get("ex_date") is None), key=lambda r: r["fiscal_year"])
    events = sorted((r for r in rows if r.get("ex_date") is not None and r["ex_date"] <= as_of
                     and r.get("dividend_amount") is not None), key=lambda r: r["ex_date"])
    with_dps = [r for r in annual if r.get("dividend_amount") is not None]
    rate = with_dps[-1]["dividend_amount"] if with_dps else None
    payout = next((r["payout_ratio"] for r in reversed(annual) if r.get("payout_ratio") is not None), None)
    yields = [r["reported_yield"] for r in annual if r.get("reported_yield") is not None][-5:]
    window = [r["dividend_amount"] for r in events if r["ex_date"] > as_of - timedelta(days=365)]
    ttm = sum(window) if window else rate
    last = events[-1] if events else None

    def per_price(x: float | None) -> float | None:
        return x / close if x is not None and close else None

    return {
        "dividend_rate": rate,
        "dividend_yield": per_price(rate),
        "payout_ratio": payout / 100 if payout is not None else None,
        "five_year_avg_dividend_yield": sum(yields) / len(yields) / 100 if yields else None,
        "trailing_annual_dividend_rate": ttm,
        "trailing_annual_dividend_yield": per_price(ttm),
        "last_dividend_value": last["dividend_amount"] if last else None,
        "last_dividend_date": (last.get("pay_date") or last["ex_date"]).isoformat() if last else None,
    }


# ── 수집 ────────────────────────────────────────────────────────────────

def _store_annual(ctx: CollectorContext, ticker: str, corp_code: str) -> None:
    with ctx.db.session() as s:
        existing = {r.fiscal_year: r for r in s.scalars(select(DividendData).where(
            DividendData.ticker == ticker, DividendData.ex_date.is_(None))).all()}
        own = {fy for fy, r in existing.items() if r.filed_date and r.filed_date.year <= fy + 1}
    for year in range(ctx.as_of.year - 1, ctx.as_of.year - 1 - ANNUAL_YEARS, -1):
        if year in own:
            continue
        rows = ctx.dart.dividends(corp_code, year)
        if not rows:
            continue
        filed = filed_date_from_rcept_no(rows[0].get("rcept_no"))
        for fy, vals in parse_alot_matter(rows, year).items():
            if fy != year and (fy in own or fy in existing):
                continue  # 전기·전전기 값은 빈 연도만 채운다
            with ctx.db.session() as s:
                upsert(s, DividendData, {"ticker": ticker, "fiscal_year": fy, "ex_date": None},
                       {"dividend_amount": vals["dps"], "reported_yield": vals["yield_pct"],
                        "payout_ratio": vals["payout_pct"], "filed_date": filed})
            existing.setdefault(fy, None)
        own.add(year)


def _store_events(ctx: CollectorContext, ticker: str, corp_code: str) -> None:
    items = ctx.dart.list_disclosures(ctx.as_of - timedelta(days=EVENT_LOOKBACK_DAYS), ctx.as_of, pblntf_ty="I", corp_code=corp_code)
    for item in sorted((i for i in items if "배당결정" in i.get("report_nm", "")), key=lambda i: i["rcept_no"]):
        parsed = parse_dividend_decision(ctx.dart.document(item["rcept_no"]))
        if parsed["record_date"] is None or parsed["dps"] is None:
            continue
        filed = filed_date_from_rcept_no(item["rcept_no"])
        with ctx.db.session() as s:
            row = upsert(s, DividendData, {"ticker": ticker, "fiscal_year": parsed["record_date"].year, "ex_date": parsed["record_date"]},
                         {"dividend_amount": parsed["dps"], "pay_date": parsed["pay_date"], "reported_yield": parsed["yield_pct"]})
            if row.filed_date is None or (filed and filed < row.filed_date):
                row.filed_date = filed  # 정정공시가 와도 최초 접수일을 유지한다


def _collect_one(ctx: CollectorContext, ticker: str) -> None:
    corp_code = corp_code_for(ctx, ticker)
    if not corp_code:
        raise RuntimeError(f"DART 고유번호를 찾지 못했습니다: {ticker}")
    _store_annual(ctx, ticker, corp_code)
    _store_events(ctx, ticker, corp_code)


def collect(ctx: CollectorContext, tickers: list[str]) -> dict[str, Any]:
    if ctx.dart is None:
        return skip_job(ctx, JOB, "DART_API_KEY 없음", tickers)
    return run_job(ctx, JOB, _collect_one, tickers)
