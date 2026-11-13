"""종목별 97개 변수 팩트시트 (§4.2) — 에이전트 프롬프트의 유일한 정량 입력.

룩어헤드 차단(§14 백테스트): 시세·공매도는 date ≤ 기준일, 재무·배당은 filed_date(공시 접수일) ≤ 기준일,
컨센서스는 date ≤ 기준일, 지배구조·지분구조는 details의 filed_date ≤ 기준일, 액면분할은 rcept_dt ≤ 기준일인 데이터만 쓴다.

신선도: 값이 없으면 "missing". 있으면 변수의 기준 데이터 날짜(아래 SOURCE 표)와 기준일의 차이를 레지스트리 주기별 한도와
비교한다 — 일 1회 ≤ 4일, 주 1회 ≤ 10일, 월 1회 ≤ 40일, 분기 ≤ 135일, 연 1회(결산 공시 포함) ≤ 400일.
정적 값(통화)·달력 계산값(결산일)·이벤트(액면분할)는 항상 fresh, 호가(#21~#24)는 이번에 호가를 받았을 때만 fresh다.
시세와 재무가 섞인 변수는 레지스트리 주기를 정하는 쪽의 날짜를 따른다(예: PER은 주 1회 → 시세 날짜, PSR은 분기 → 보고서 접수일).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from autopilot_korea.collectors.balance_sheet_collector import compute_balance_vars
from autopilot_korea.collectors.base import upsert
from autopilot_korea.collectors.consensus_collector import compute_consensus_vars
from autopilot_korea.collectors.dividend_collector import compute_dividend_vars
from autopilot_korea.collectors.financials_collector import compute_financial_vars, report_dict, summarize_financials
from autopilot_korea.collectors.governance_collector import compute_governance_vars
from autopilot_korea.collectors.info_collector import compute_info_vars
from autopilot_korea.collectors.momentum_collector import BENCHMARK_TICKER, compute_momentum_vars
from autopilot_korea.collectors.price_collector import HISTORY_DAYS, avg_trading_value, compute_price_vars, compute_quote_vars
from autopilot_korea.collectors.short_interest_collector import compute_short_vars
from autopilot_korea.collectors.valuation_collector import compute_valuation_vars
from autopilot_korea.db.models import (ConsensusData, DividendData, FactorSnapshot, FinancialData, GovernanceData, PriceData,
                                       ShortInterest, StockMaster)
from autopilot_korea.db.session import Database
from autopilot_korea.variables import BY_KEY, ORDERED_KEYS

log = logging.getLogger(__name__)

FRESH_DAYS = {"일 1회": 4, "주 1회": 10, "월 1회": 40, "분기": 135, "연 1회": 400, "결산 배당공시 시": 400, "결산 후": 400}
CATEGORY_SOURCE = {"A": "price", "B": "dividend_annual", "C": "financial", "D": "price", "E": "short", "F": "price",
                   "G": "financial", "H": "financial", "I": "consensus", "J": "governance"}
SOURCE = {
    **{k: "quote" for k in ("bid", "ask", "bid_size", "ask_size")},
    "dividend_yield": "price", "trailing_annual_dividend_yield": "price", "trailing_annual_dividend_rate": "dividend_ttm",
    "last_dividend_value": "dividend_event", "last_dividend_date": "dividend_event",
    "trailing_pe": "price", "price_to_book": "price", "forward_pe": "consensus",
    "float_shares": "ownership", "insider_holdings_pct": "ownership", "institutional_holdings_pct": "ownership",
    "shares_outstanding": "price",
    "currency": "static", "financial_currency": "static", "last_fiscal_year_end": "static", "next_fiscal_year_end": "static",
    "last_split_factor": "static", "last_split_date": "static", "most_recent_quarter": "financial",
    "forward_eps": "consensus",
}


def source_of(key: str) -> str:
    return SOURCE.get(key) or CATEGORY_SOURCE[BY_KEY[key].category]


@dataclass
class Factsheet:
    ticker: str
    name: str
    sector: str
    as_of: date
    variables: dict[str, Any]  # variables.ORDERED_KEYS 전부 (없으면 None)
    freshness: dict[str, str]  # key → fresh | stale | missing
    extras: dict[str, Any] = field(default_factory=dict)  # last_close, avg_trading_value_20d, foreign_ownership, risk_flags …

    def to_prompt_json(self) -> str:
        """{"#1 전일 종가": …, …, "#97 종합 리스크": …, "_stale_or_missing": ["#38:missing", …]} (번호 순, 공백 없는 JSON)."""
        data: dict[str, Any] = {}
        flagged = []
        for key in ORDERED_KEYS:
            var = BY_KEY[key]
            data[f"#{var.no} {var.name_kr}"] = _compact(self.variables.get(key))
            status = self.freshness.get(key, "missing")
            if status != "fresh":
                flagged.append(f"#{var.no}:{status}")
        data["_stale_or_missing"] = flagged
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _clean(v: Any) -> Any:
    """NaN·inf → None, numpy 수치 → 파이썬 수치 (JSON 저장용)."""
    if v is None or isinstance(v, (str, bool)):
        return v
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _compact(v: Any) -> Any:
    if isinstance(v, float):
        return int(round(v)) if abs(v) >= 1000 else round(v, 4)
    return v


# ── 기준일 이하 데이터 적재 (룩어헤드 차단) ─────────────────────────────

def load_prices(session: Session, ticker: str, as_of: date) -> pd.DataFrame:
    rows = session.execute(select(PriceData.date, PriceData.open, PriceData.high, PriceData.low, PriceData.close,
                                  PriceData.volume, PriceData.trading_value, PriceData.market_cap, PriceData.shares,
                                  PriceData.foreign_ratio)
                           .where(PriceData.ticker == ticker, PriceData.date <= as_of,
                                  PriceData.date > as_of - timedelta(days=HISTORY_DAYS))
                           .order_by(PriceData.date)).all()
    cols = ["date", "open", "high", "low", "close", "volume", "trading_value", "market_cap", "shares", "foreign_ratio"]
    df = pd.DataFrame(rows, columns=cols).set_index("date")
    return df.astype(float)


def _last(series: pd.Series | None) -> float | None:
    if series is None:
        return None
    s = series.dropna()
    return float(s.iloc[-1]) if len(s) else None


def _row_dict(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {f: getattr(row, f) for f in fields}


def _iso_date(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def latest_governance(rows: list[GovernanceData], as_of: date) -> tuple[dict | None, dict | None]:
    """(지배구조 등급 행, 지분구조 스냅샷) — 각각 접수일 ≤ 기준일 중 최신."""
    scored = [r for r in rows if r.overall_risk is not None and (_iso_date((r.details or {}).get("filed_date")) or date.max) <= as_of]
    gov = max(scored, key=lambda r: r.year, default=None)
    snaps = [snap for r in rows for snap in ((r.details or {}).get("ownership") or {}).values()
             if (_iso_date(snap.get("filed_date")) or date.max) <= as_of]
    ownership = max(snaps, key=lambda x: x.get("filed_date") or "", default=None)
    gov_dict = None
    if gov is not None:
        gov_dict = _row_dict(gov, ("year", "audit_risk", "board_risk", "comp_risk", "shareholder_risk", "overall_risk"))
        gov_dict["filed_date"] = _iso_date(gov.details.get("filed_date"))
    return gov_dict, ownership


def build_factsheet(session: Session, ticker: str, as_of: date, quote: dict | None = None) -> Factsheet:
    master = session.get(StockMaster, ticker)
    prices = load_prices(session, ticker, as_of)
    bench = load_prices(session, BENCHMARK_TICKER, as_of)["close"]
    close = _last(prices["close"])
    shares = _last(prices["shares"])
    market_cap = _last(prices["market_cap"])
    if market_cap is None and close and shares:
        market_cap = close * shares
    if shares is None and market_cap and close:
        shares = market_cap / close

    reports = [report_dict(r) for r in session.scalars(select(FinancialData).where(
        FinancialData.ticker == ticker, FinancialData.filed_date.is_not(None), FinancialData.filed_date <= as_of)).all()]
    summary = summarize_financials(reports)
    cons_row = session.scalars(select(ConsensusData).where(ConsensusData.ticker == ticker, ConsensusData.date <= as_of)
                               .order_by(ConsensusData.date.desc())).first()
    consensus = _row_dict(cons_row, ("date", "covered", "target_high", "target_low", "target_mean", "target_median",
                                     "rec_mean", "rec_key", "n_analysts", "forward_eps")) if cons_row else None
    forward_eps = consensus["forward_eps"] if consensus and consensus["covered"] else None
    dividends = [_row_dict(r, ("fiscal_year", "ex_date", "pay_date", "dividend_amount", "reported_yield", "payout_ratio", "filed_date"))
                 for r in session.scalars(select(DividendData).where(
                     DividendData.ticker == ticker, DividendData.filed_date.is_not(None), DividendData.filed_date <= as_of)).all()]
    short_rows = session.execute(select(ShortInterest.date, ShortInterest.short_volume, ShortInterest.short_balance)
                                 .where(ShortInterest.ticker == ticker, ShortInterest.date <= as_of,
                                        ShortInterest.date > as_of - timedelta(days=70)).order_by(ShortInterest.date)).all()
    shorts = pd.DataFrame(short_rows, columns=["date", "short_volume", "short_balance"]).set_index("date").astype(float)
    governance, ownership = latest_governance(list(session.scalars(select(GovernanceData).where(GovernanceData.ticker == ticker)).all()), as_of)

    v: dict[str, Any] = {}
    v.update(compute_price_vars(prices))
    v.update(compute_quote_vars(quote))
    v.update(compute_dividend_vars(dividends, close, as_of))
    v.update(compute_valuation_vars(summary, market_cap, close, shares, forward_eps))
    v.update(compute_momentum_vars(prices, bench))
    v.update(compute_short_vars(shorts, as_of, shares, v.get("average_volume"), ownership))
    v.update(compute_info_vars(as_of, master.settle_month if master else None, market_cap, close, summary.get("period_end"),
                               master.corporate_actions if master else None))
    v.update(compute_financial_vars(summary, shares, forward_eps))
    v.update(compute_balance_vars(summary, shares))
    v.update(compute_consensus_vars(consensus))
    v.update(compute_governance_vars(governance))
    variables = {k: _clean(v.get(k)) for k in ORDERED_KEYS}

    annual_filed = [d["filed_date"] for d in dividends if d["ex_date"] is None]
    events = [d for d in dividends if d["ex_date"] is not None and d["ex_date"] <= as_of]
    recent_events = [d["filed_date"] for d in events if d["ex_date"] > as_of - timedelta(days=365)]
    dates = {
        "price": prices.index[-1] if len(prices) else None,
        "financial": summary.get("filed_date"),
        "dividend_annual": max(annual_filed, default=None),
        "dividend_event": max((d["filed_date"] for d in events), default=None),
        "dividend_ttm": max(recent_events, default=None) or max(annual_filed, default=None),
        "consensus": consensus["date"] if consensus else None,
        "short": shorts.index[-1] if len(shorts) else None,
        "ownership": _iso_date(ownership.get("filed_date")) if ownership else None,
        "governance": governance["filed_date"] if governance else None,
    }
    freshness = {k: _freshness(k, variables[k], dates, as_of, quote is not None) for k in ORDERED_KEYS}
    extras = {
        "last_close": close,
        "price_date": dates["price"].isoformat() if dates["price"] else None,
        "avg_trading_value_20d": avg_trading_value(prices),
        "foreign_ownership": _last(prices["foreign_ratio"]),
        "risk_flags": list(master.risk_flags or []) if master else [],
    }
    return Factsheet(ticker=ticker, name=master.name if master else ticker, sector=master.sector if master else "미분류",
                     as_of=as_of, variables=variables, freshness=freshness, extras=extras)


def _freshness(key: str, value: Any, dates: dict[str, date | None], as_of: date, quote_supplied: bool) -> str:
    if value is None:
        return "missing"
    src = source_of(key)
    if src == "static":
        return "fresh"
    if src == "quote":
        return "fresh" if quote_supplied else "stale"
    when = dates.get(src)
    limit = FRESH_DAYS.get(BY_KEY[key].frequency)
    if limit is None:
        return "fresh"
    if when is None:
        return "stale"
    return "fresh" if (as_of - when).days <= limit else "stale"


def persist_factsheet(session: Session, fs: Factsheet) -> None:
    upsert(session, FactorSnapshot, {"ticker": fs.ticker, "as_of": fs.as_of},
           {"variables": dict(fs.variables), "freshness": dict(fs.freshness)})


def build_factsheets(db: Database, tickers: list[str], as_of: date, quotes: Any = None, persist: bool = True) -> dict[str, Factsheet]:
    """여러 종목의 팩트시트. quotes는 QuoteProvider(best_quote) 또는 {ticker: 호가 dict}.

    한 종목의 실패는 로그만 남기고 건너뛴다(§15). persist=True면 factor_snapshot에 (ticker, as_of)로 저장한다.
    """
    out: dict[str, Factsheet] = {}
    for ticker in tickers:
        quote = None
        if quotes is not None:
            try:
                quote = quotes.best_quote(ticker) if hasattr(quotes, "best_quote") else quotes.get(ticker)
            except Exception as exc:  # noqa: BLE001 — 호가가 없으면 #21~#24만 비운다
                log.warning("호가 조회 실패 %s: %s", ticker, exc)
        try:
            with db.session() as s:
                fs = build_factsheet(s, ticker, as_of, quote)
                if persist:
                    persist_factsheet(s, fs)
            out[ticker] = fs
        except Exception:  # noqa: BLE001
            log.exception("팩트시트 생성 실패 %s", ticker)
    return out
