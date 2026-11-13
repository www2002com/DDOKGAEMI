"""E. 공매도·지분구조 (#36~#46).

- 공매도(#38~#42, #45, #46): KRX 공매도 통계(pykrx). FDR 대체 원천은 공매도를 주지 않으므로 KRX 로그인이 없으면
  작업을 건너뛰고 팩트시트에서 "missing"이 된다. 공매도 잔고는 보고 의무 발생일 기준(T+2 공표)이다.
- 지분구조(#36, #43, #44): DART 정기보고서의 최대주주 현황(hyslrSttus)·주식의 총수(stockTotqySttus)와
  대량보유 상황보고(majorstock). 정기보고서 단위 데이터라 분기 작업(governance)이 collect_ownership으로 수집해
  governance_data.details["ownership"][보고서코드]에 접수일과 함께 저장한다.
- #44 기관 보유 비중은 근사치다: 최대주주 특수관계인 명단에 없는 5% 이상 대량보유 보고자(대표보고자)의
  최신 보유비율 합. 5% 미만 기관·외국인 보유분은 잡히지 않고, 보고자가 개인이어도 포함된다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select

from autopilot_korea.collectors.base import CollectorContext, corp_code_for, run_job, skip_job
from autopilot_korea.collectors.financials_collector import _div, quarter_end
from autopilot_korea.collectors.sources.dart import REPORT_CODES, filed_date_from_rcept_no, parse_amount, parse_dart_date
from autopilot_korea.db.models import GovernanceData, ShortInterest, StockMaster

JOB = "short_interest"
TOTAL_NAMES = ("계", "합계", "소계")


def _clean(name: Any) -> str:
    return "".join(str(name or "").split())


def compute_ownership_snapshot(hyslr_rows: list[dict], totals_rows: list[dict], major_rows: list[dict]) -> dict[str, Any]:
    """DART 응답 → 지분구조 스냅샷. 비율은 퍼센트 수치(DART 원 단위) 그대로 둔다."""
    common = [r for r in hyslr_rows if "보통" in str(r.get("stock_knd", ""))]
    total = next((r for r in common if _clean(r.get("nm")) in TOTAL_NAMES), None)
    people = [r for r in common if _clean(r.get("nm")) not in TOTAL_NAMES]
    if total is not None:
        shares, pct = parse_amount(total.get("trmend_posesn_stock_co")), parse_amount(total.get("trmend_posesn_stock_qota_rt"))
    else:
        shares = sum(parse_amount(r.get("trmend_posesn_stock_co")) or 0 for r in people) if people else None
        pct = sum(parse_amount(r.get("trmend_posesn_stock_qota_rt")) or 0 for r in people) if people else None
    common_row = next((r for r in totals_rows if "보통" in str(r.get("se", ""))), None)
    has_preferred = any(
        "보통" not in str(r.get("se", "")) and _clean(r.get("se")) not in ("합계", "비고")
        and (parse_amount(r.get("istc_totqy")) or 0) > 0
        for r in totals_rows
    )
    majors = [{"name": _clean(r.get("repror")), "pct": parse_amount(r.get("stkrt")),
               "rcept_dt": (parse_dart_date(r.get("rcept_dt")) or date.min).isoformat(), "rcept_no": r.get("rcept_no")}
              for r in major_rows]
    return {
        "largest_holder_shares": shares,
        "largest_holder_pct": pct,
        "largest_holder_names": sorted({_clean(r.get("nm")) for r in people}),
        "has_preferred": has_preferred,
        "common_shares": parse_amount(common_row.get("istc_totqy")) if common_row else None,
        "treasury_shares": parse_amount(common_row.get("tesstk_co")) if common_row else None,
        "major_holders": majors,
    }


def institutional_pct(snapshot: dict[str, Any] | None, as_of: date) -> float | None:
    """#44 근사 — 모듈 docstring 참조. 기준일 이전 보고서만, 보고자별 최신 비율."""
    if not snapshot:
        return None
    latest: dict[str, tuple[str, str, float | None]] = {}
    for m in snapshot.get("major_holders") or []:
        if m["rcept_dt"] > as_of.isoformat():
            continue
        key = (m["rcept_dt"], str(m.get("rcept_no") or ""))
        if m["name"] not in latest or key > latest[m["name"]][:2]:
            latest[m["name"]] = (*key, m.get("pct"))
    insiders = set(snapshot.get("largest_holder_names") or [])
    total = sum(p for name, (_, _, p) in latest.items() if name not in insiders and p is not None and p >= 5)
    return total / 100


def compute_short_vars(shorts: pd.DataFrame | None, as_of: date, shares: float | None, avg_volume_20d: float | None,
                       ownership: dict[str, Any] | None) -> dict[str, Any]:
    """shorts: 날짜 오름차순 인덱스, 컬럼 short_volume/short_balance (기준일 이하만)."""
    holder_shares = (ownership or {}).get("largest_holder_shares")
    holder_pct = (ownership or {}).get("largest_holder_pct")
    float_shares = shares - holder_shares if shares is not None and holder_shares is not None else None
    out: dict[str, Any] = {
        "float_shares": float_shares,
        "shares_outstanding": shares,
        "shares_short": None, "shares_short_prior_month": None, "short_interest_date_prior_month": None,
        "short_interest_date": None, "short_percent_of_outstanding": None,
        "insider_holdings_pct": holder_pct / 100 if holder_pct is not None else None,
        "institutional_holdings_pct": institutional_pct(ownership, as_of),
        "short_ratio": None, "short_percent_of_float": None,
    }
    if shorts is None or shorts.empty:
        return out
    vol = shorts["short_volume"].dropna() if "short_volume" in shorts else pd.Series(dtype=float)
    if len(vol):
        out["shares_short"] = float(vol.iloc[-1])
        out["short_interest_date"] = vol.index[-1].isoformat()
    prev_end = as_of.replace(day=1) - timedelta(days=1)
    prev = vol[[prev_end.replace(day=1) <= d <= prev_end for d in vol.index]] if len(vol) else vol
    if len(prev):
        out["shares_short_prior_month"] = float(prev.sum())
        out["short_interest_date_prior_month"] = prev.index[-1].isoformat()
    bal = shorts["short_balance"].dropna() if "short_balance" in shorts else pd.Series(dtype=float)
    if len(bal):
        balance = float(bal.iloc[-1])
        out["short_percent_of_outstanding"] = _div(balance, shares)
        out["short_ratio"] = _div(balance, avg_volume_20d)
        out["short_percent_of_float"] = _div(balance, float_shares)
    return out


# ── 수집 ────────────────────────────────────────────────────────────────

def collect_ownership(ctx: CollectorContext, ticker: str) -> dict[str, Any] | None:
    """최근 정기보고서의 지분구조를 governance_data.details["ownership"][reprt_code]에 저장한다."""
    corp_code = corp_code_for(ctx, ticker)
    if not corp_code:
        raise RuntimeError(f"DART 고유번호를 찾지 못했습니다: {ticker}")
    with ctx.db.session() as s:
        master = s.get(StockMaster, ticker)
        settle = master.settle_month if master and master.settle_month else 12
    periods = [(y, q) for y in (ctx.as_of.year, ctx.as_of.year - 1) for q in (4, 3, 2, 1)
               if quarter_end(y, q, settle) + timedelta(days=14) <= ctx.as_of]
    for year, q in sorted(periods, reverse=True)[:3]:
        code = REPORT_CODES[q]
        holders = ctx.dart.largest_holders(corp_code, year, code)
        if not holders:
            continue
        snap = compute_ownership_snapshot(holders, ctx.dart.stock_totals(corp_code, year, code), ctx.dart.major_holdings(corp_code))
        filed = filed_date_from_rcept_no(holders[0].get("rcept_no"))
        snap.update(bsns_year=year, reprt_code=code, filed_date=filed.isoformat() if filed else None)
        with ctx.db.session() as s:
            row = s.scalars(select(GovernanceData).filter_by(ticker=ticker, year=year)).first()
            if row is None:
                row = GovernanceData(ticker=ticker, year=year, details={})
                s.add(row)
            details = dict(row.details or {})
            details["ownership"] = {**(details.get("ownership") or {}), code: snap}
            row.details = details
        return snap
    return None


def _collect_one(ctx: CollectorContext, ticker: str) -> None:
    start = min(ctx.as_of - timedelta(days=45), (ctx.as_of.replace(day=1) - timedelta(days=1)).replace(day=1))
    df = ctx.market.short_selling(ticker, start, ctx.as_of)
    if df is None or df.empty:
        raise RuntimeError(f"공매도 통계가 비어 있습니다: {ticker}")
    with ctx.db.session() as s:
        existing = {r.date: r for r in s.scalars(select(ShortInterest).where(
            ShortInterest.ticker == ticker, ShortInterest.date >= start)).all()}
        for d, row in df.iterrows():
            values = {k: None if pd.isna(row.get(k)) else float(row.get(k)) for k in ("short_volume", "short_balance", "short_ratio")}
            if d in existing:
                for k, v in values.items():
                    if v is not None:
                        setattr(existing[d], k, v)
            else:
                s.add(ShortInterest(ticker=ticker, date=d, **values))


def collect(ctx: CollectorContext, tickers: list[str]) -> dict[str, Any]:
    if not tickers or ctx.market.short_selling(tickers[0], ctx.as_of, ctx.as_of) is None:
        return skip_job(ctx, JOB, f"시세 원천({getattr(ctx.market, 'name', '?')})이 공매도 통계를 제공하지 않음 — KRX 로그인 필요", tickers)
    return run_job(ctx, JOB, _collect_one, tickers)
