"""J. 지배구조 리스크 (#93~#97) — 원본(Yahoo governance)에 직접 대응이 없어 DART 사업보고서로 자체 산출한다.

등급은 정수 1(저위험)~5(고위험).
- #93 감사: 적정 → 1, 적정이지만 강조사항(계속기업 불확실성 등) 기재 → 3, 한정 → 4, 부적정·의견거절 → 5.
- #94 이사회: 사외이사 비율 ≥ 50% → 1, ≥ 1/3 → 2, ≥ 25% → 3, > 0 → 4, 0 → 5.
- #95 보상: 이사·감사 보수총액 ÷ 당기순이익(지배) < 1% → 1, < 3% → 2, < 5% → 3, < 10% → 4, 그 이상 또는 순이익 ≤ 0 → 5.
- #96 주주권: 최대주주 등 지분율 < 30% → 2, < 50% → 3, ≥ 50% → 4, 우선주(종류주식)가 있으면 +1 (최대 5).
- #97 종합: 감사 .30, 이사회 .25, 보상 .20, 주주권 .25 가중 평균(있는 항목만, 가중치 재정규화), 소수 첫째 자리.

분기 작업이므로 최근 정기보고서의 지분구조 스냅샷(short_interest_collector.collect_ownership)도 함께 수집한다.
details["filed_date"]는 사업보고서 접수일이며 팩트시트는 이 날짜 ≤ 기준일인 행만 쓴다.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from autopilot_korea.collectors.base import CollectorContext, corp_code_for, run_job, skip_job
from autopilot_korea.collectors.short_interest_collector import collect_ownership, compute_ownership_snapshot
from autopilot_korea.collectors.sources.dart import filed_date_from_rcept_no, parse_amount
from autopilot_korea.db.models import FinancialData, GovernanceData

JOB = "governance"
WEIGHTS = {"audit_risk": 0.30, "board_risk": 0.25, "compensation_risk": 0.20, "shareholder_rights_risk": 0.25}
NO_MATTER = {"", "-", "해당사항없음", "해당사항이없음", "없음", "해당없음", "N/A", "n/a"}


def _clean(text: Any) -> str:
    return "".join(str(text or "").split())


def audit_risk_level(opinion: str | None, emphasis: str | None = None) -> int | None:
    op = _clean(opinion)
    if not op:
        return None
    if "의견거절" in op or "부적정" in op:  # "부적정"은 "적정"을 포함하므로 먼저 본다
        return 5
    if "한정" in op:
        return 4
    if "적정" in op:
        return 1 if _clean(emphasis) in NO_MATTER else 3
    return None


def board_risk_level(directors: float | None, outside: float | None) -> int | None:
    if not directors or outside is None:
        return None
    ratio = outside / directors
    if ratio >= 0.5:
        return 1
    if ratio >= 1 / 3:
        return 2
    if ratio >= 0.25:
        return 3
    return 4 if ratio > 0 else 5


def compensation_risk_level(total_pay: float | None, net_income: float | None) -> int | None:
    if total_pay is None or net_income is None:
        return None
    if net_income <= 0:
        return 5
    ratio = total_pay / net_income
    for limit, level in ((0.01, 1), (0.03, 2), (0.05, 3), (0.10, 4)):
        if ratio < limit:
            return level
    return 5


def shareholder_rights_level(largest_holder_pct: float | None, has_preferred: bool = False) -> int | None:
    """largest_holder_pct는 퍼센트 수치(예: 20.1)."""
    if largest_holder_pct is None:
        return None
    level = 2 if largest_holder_pct < 30 else 3 if largest_holder_pct < 50 else 4
    return min(level + (1 if has_preferred else 0), 5)


def overall_risk_level(levels: dict[str, int | None]) -> float | None:
    parts = [(WEIGHTS[k], v) for k, v in levels.items() if k in WEIGHTS and v is not None]
    if not parts:
        return None
    return round(sum(w * v for w, v in parts) / sum(w for w, _ in parts), 1)


def score_governance(audit_rows: list[dict], director_rows: list[dict], pay_rows: list[dict],
                     ownership: dict[str, Any] | None, net_income: float | None) -> dict[str, Any]:
    """DART 사업보고서 응답 → 등급과 근거(details)."""
    audit = next((r for r in audit_rows if "당기" in str(r.get("bsns_year", ""))), audit_rows[0] if audit_rows else {})
    emphasis = audit.get("emphs_matter") if audit.get("emphs_matter") not in (None, "") else audit.get("adt_reprt_spcmnt_matter")
    board = director_rows[0] if director_rows else {}
    current_pay = [r for r in pay_rows if "당기" in str(r.get("fscl_year", ""))] or pay_rows
    pays = [parse_amount(r.get("mendng_totamt")) for r in current_pay]
    total_pay = sum(p for p in pays if p is not None) if any(p is not None for p in pays) else None
    own = ownership or {}
    levels = {
        "audit_risk": audit_risk_level(audit.get("adt_opinion"), emphasis),
        "board_risk": board_risk_level(parse_amount(board.get("drctr_co")), parse_amount(board.get("otcmp_drctr_co"))),
        "compensation_risk": compensation_risk_level(total_pay, net_income),
        "shareholder_rights_risk": shareholder_rights_level(own.get("largest_holder_pct"), bool(own.get("has_preferred"))),
    }
    levels["overall_risk"] = overall_risk_level(levels)
    details = {
        "audit": {"auditor": audit.get("adtor"), "opinion": audit.get("adt_opinion"), "emphasis": emphasis},
        "board": {"directors": parse_amount(board.get("drctr_co")), "outside": parse_amount(board.get("otcmp_drctr_co"))},
        "comp": {"total_pay": total_pay, "net_income": net_income},
        "shareholder": {"largest_holder_pct": own.get("largest_holder_pct"), "has_preferred": own.get("has_preferred")},
    }
    return {"levels": levels, "details": details}


def compute_governance_vars(row: dict[str, Any] | None) -> dict[str, Any]:
    r = row or {}
    return {"audit_risk": r.get("audit_risk"), "board_risk": r.get("board_risk"), "compensation_risk": r.get("comp_risk"),
            "shareholder_rights_risk": r.get("shareholder_risk"), "overall_risk": r.get("overall_risk")}


def _collect_one(ctx: CollectorContext, ticker: str) -> None:
    corp_code = corp_code_for(ctx, ticker)
    if not corp_code:
        raise RuntimeError(f"DART 고유번호를 찾지 못했습니다: {ticker}")
    collect_ownership(ctx, ticker)
    for year in (ctx.as_of.year - 1, ctx.as_of.year - 2):
        audit = ctx.dart.audit_opinions(corp_code, year)
        if not audit:
            continue  # 사업보고서가 아직 없다
        holders = ctx.dart.largest_holders(corp_code, year, "11011")
        ownership = compute_ownership_snapshot(holders, ctx.dart.stock_totals(corp_code, year, "11011"), [])
        with ctx.db.session() as s:
            fin = s.scalars(select(FinancialData).where(FinancialData.ticker == ticker, FinancialData.quarter == f"{year}Q4")
                            .order_by(FinancialData.fs_div)).first()
        net_income = fin.net_income if fin else None
        scored = score_governance(audit, ctx.dart.outside_directors(corp_code, year), ctx.dart.director_pay(corp_code, year),
                                  ownership, net_income)
        lv = scored["levels"]
        filed = filed_date_from_rcept_no(audit[0].get("rcept_no"))
        with ctx.db.session() as s:
            row = s.scalars(select(GovernanceData).filter_by(ticker=ticker, year=year)).first()
            if row is None:
                row = GovernanceData(ticker=ticker, year=year, details={})
                s.add(row)
            row.audit_risk, row.board_risk, row.comp_risk = lv["audit_risk"], lv["board_risk"], lv["compensation_risk"]
            row.shareholder_risk, row.overall_risk = lv["shareholder_rights_risk"], lv["overall_risk"]
            row.details = {**(row.details or {}), **scored["details"], "filed_date": filed.isoformat() if filed else None}
        return


def collect(ctx: CollectorContext, tickers: list[str]) -> dict[str, Any]:
    if ctx.dart is None:
        return skip_job(ctx, JOB, "DART_API_KEY 없음", tickers)
    return run_job(ctx, JOB, _collect_one, tickers)
