"""데이터 레이어 단위 테스트 (네트워크 없음).

순수 계산 함수(카테고리 A~J), 원천 파서, 시드한 인메모리 DB로 만든 팩트시트(97개 키·신선도·룩어헤드 차단),
run_job 재시도·회로 차단기를 검증한다.
"""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from autopilot_korea.collectors import COLLECTORS, run_collectors
from autopilot_korea.collectors.balance_sheet_collector import compute_balance_vars
from autopilot_korea.collectors.base import CollectorContext, run_job
from autopilot_korea.collectors.consensus_collector import compute_consensus_vars
from autopilot_korea.collectors.dividend_collector import compute_dividend_vars, parse_alot_matter, parse_dividend_decision
from autopilot_korea.collectors.factsheet import Factsheet, build_factsheet, build_factsheets
from autopilot_korea.collectors.financials_collector import (cagr, compute_financial_vars, discrete_quarter, normalize_statement,
                                                             quarter_end, summarize_financials, ttm_at)
from autopilot_korea.collectors.governance_collector import (audit_risk_level, board_risk_level, compensation_risk_level,
                                                             compute_governance_vars, overall_risk_level, score_governance,
                                                             shareholder_rights_level)
from autopilot_korea.collectors.info_collector import compute_info_vars, fiscal_year_ends, parse_split_decision, split_from_share_jump
from autopilot_korea.collectors.momentum_collector import BENCHMARK_TICKER, compute_beta, compute_momentum_vars
from autopilot_korea.collectors.news_collector import recent_news, store_items
from autopilot_korea.collectors.price_collector import avg_trading_value, compute_price_vars, compute_quote_vars
from autopilot_korea.collectors.short_interest_collector import compute_ownership_snapshot, compute_short_vars, institutional_pct
from autopilot_korea.collectors.sources import ecos, naver
from autopilot_korea.collectors.sources.dart import DartClient, DartKeyError, DartRateLimitError, filed_date_from_rcept_no
from autopilot_korea.collectors.sources.fnguide import forward_12m_eps, parse_consensus_html, rec_key
from autopilot_korea.collectors.sources.market import PykrxSource, top_by_market_cap
from autopilot_korea.collectors.universe import liquid_universe, refresh_universe
from autopilot_korea.collectors.valuation_collector import compute_valuation_vars
from autopilot_korea.db.models import (CollectionStatus, ConsensusData, DividendData, FactorSnapshot, FinancialData, GovernanceData,
                                       PriceData, ShortInterest, StockMaster)
from autopilot_korea.db.session import Database
from autopilot_korea.sectors import classify_sector
from autopilot_korea.settings import load_settings
from autopilot_korea.variables import BY_KEY, ORDERED_KEYS

AS_OF = date(2026, 11, 13)
KST = timezone(timedelta(hours=9))


@pytest.fixture
def fast_settings():
    base = load_settings()
    schedule = {**base.schedule, "retry": {"attempts": 3, "backoff_seconds": [0, 0, 0]},
                "circuit_breaker": {"failure_threshold": 3, "cooldown_seconds": 600}}
    return dataclasses.replace(base, schedule=schedule)


@pytest.fixture
def mem_db():
    db = Database("sqlite:///:memory:")
    db.create_all()
    return db


# ── 섹터 ──────────────────────────────────────────────────────────────────

def test_classify_sector_overrides_then_ordered_keywords():
    cfg = load_settings().sectors
    assert classify_sector("005930", "아무 업종", cfg) == "정보기술"  # override 우선
    assert classify_sector("999990", "반도체 제조업", cfg) == "정보기술"
    assert classify_sector("999990", "통신 및 방송 장비 제조업", cfg) == "정보기술"  # "통신"(커뮤니케이션)보다 먼저
    assert classify_sector("999990", "전기 통신업", cfg) == "커뮤니케이션서비스"
    assert classify_sector("999990", "자동차용 엔진 및 자동차 제조업", cfg) == "경기소비재"
    assert classify_sector("999990", "기타 금융업", cfg) == "금융"
    assert classify_sector("999990", "", cfg) == "미분류"
    assert classify_sector("999990", None, cfg) == "미분류"


# ── G/H: 재무 정규화와 TTM ─────────────────────────────────────────────────

def _line(sj, account_id, name, amount, added=None):
    return {"sj_div": sj, "account_id": account_id, "account_nm": name, "thstrm_amount": amount, "thstrm_add_amount": added,
            "rcept_no": "20261114000123"}


def test_normalize_statement_maps_ids_names_and_sums():
    rows = [
        _line("IS", "ifrs-full_Revenue", "매출액", "86,061", "239,768"),  # 분기보고서: 누적값을 쓴다
        _line("IS", "ifrs_ProfitLossAttributableToOwnersOfParent", "지배기업 소유주지분", "12", "25"),  # ifrs_ 접두
        _line("IS", "ifrs-full_ProfitLoss", "분기순이익", "13", "26"),
        _line("CIS", "ifrs-full_ComprehensiveIncomeAttributableToOwnersOfParent", "지배기업 소유주지분", "99", "99"),
        _line("BS", "-표준계정코드 미사용-", "단기차입금", "9,846"),  # 이름으로 찾는다
        _line("BS", "ifrs-full_EquityAttributableToOwnersOfParent", "지배기업 소유주지분", "402,001"),
        _line("BS", "ifrs-full_CurrentLeaseLiabilities", "유동리스부채", "10"),
        _line("BS", "ifrs-full_NoncurrentLeaseLiabilities", "비유동리스부채", "30"),
        _line("CF", "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "유형자산의 취득", "35,972"),
        _line("CF", "-표준계정코드 미사용-", "감가상각비", "100"),
        _line("CF", "-표준계정코드 미사용-", "무형자산상각비", "20"),
    ]
    items = normalize_statement(rows)
    assert items["revenue"] == 239768
    assert items["net_income_controlling"] == 25  # IS가 CIS보다 먼저
    assert items["net_income"] == 26
    assert items["short_term_borrowings"] == 9846
    assert items["equity_controlling"] == 402001
    assert items["lease_liabilities"] == 40  # 유동 + 비유동
    assert items["capex"] == 35972
    assert items["depreciation_amortization"] == 120
    assert items["inventories"] is None


def test_normalize_statement_financial_company_lacks_lines():
    bank = [_line("BS", "ifrs-full_Assets", "자산총계", "700,000"), _line("BS", "ifrs-full_Liabilities", "부채총계", "650,000"),
            _line("BS", "ifrs-full_Equity", "자본총계", "50,000"), _line("IS", "ifrs-full_ProfitLoss", "당기순이익", "5,000")]
    items = normalize_statement(bank)
    assert items["revenue"] is None and items["current_assets"] is None
    s = summarize_financials([{"year": 2025, "q": 4, "items": items, "period_end": date(2025, 12, 31),
                               "filed_date": date(2026, 3, 10), "fs_div": "CFS"}])
    g, h = compute_financial_vars(s, 1000, None), compute_balance_vars(s, 1000)
    assert g["net_income_to_common"] == 5000  # 지배주주 줄이 없으면 전체 순이익
    assert g["profit_margins"] is None and h["current_ratio"] is None and h["quick_ratio"] is None
    assert h["debt_to_equity"] == pytest.approx(13.0)


def _rep(year, q, ni, rev, fs="CFS", **extra):
    return {"year": year, "q": q, "fs_div": fs, "period_end": quarter_end(year, q),
            "filed_date": quarter_end(year, q) + timedelta(days=45),
            "items": {"net_income_controlling": ni, "net_income": ni, "revenue": rev, **extra}}


TTM_REPORTS = [_rep(2024, 3, 30, 300), _rep(2024, 4, 50, 400), _rep(2025, 1, 10, 100), _rep(2025, 2, 25, 210),
               _rep(2025, 3, 45, 330), _rep(2025, 4, 70, 450), _rep(2026, 1, 15, 120), _rep(2026, 2, 35, 250),
               _rep(2026, 3, 60, 390)]


def test_ttm_and_discrete_quarter_math():
    by = {(r["year"], r["q"]): r for r in TTM_REPORTS}
    assert ttm_at(by, 2026, 3, "net_income_controlling") == 60 + 70 - 45
    assert ttm_at(by, 2025, 4, "net_income_controlling") == 70  # 사업보고서면 연간 값
    assert ttm_at(by, 2025, 3, "net_income_controlling") == 45 + 50 - 30
    assert ttm_at(by, 2024, 3, "net_income_controlling") is None  # 전년 자료 없음
    assert discrete_quarter(by, 2026, 3, "net_income_controlling") == 25
    assert discrete_quarter(by, 2026, 1, "net_income_controlling") == 15
    assert discrete_quarter(by, 2025, 4, "revenue") == 450 - 330
    s = summarize_financials(TTM_REPORTS)
    assert s["ttm_revenue"] == 390 + 450 - 330
    assert s["ttm_ni_ctrl_prev"] == 65
    g = compute_financial_vars(s, shares=10, forward_eps=None)
    assert g["earnings_quarterly_growth"] == pytest.approx(25 / 20 - 1)
    assert g["net_income_to_common"] == 85 and g["trailing_eps"] == 8.5
    assert g["profit_margins"] == pytest.approx(85 / 510)


def test_cagr_rules():
    assert cagr(133.1, 100) == pytest.approx(0.1)
    assert cagr(100, 0) is None and cagr(100, -5) is None and cagr(-1, 100) is None
    assert cagr(0, 100) == pytest.approx(-1.0)


def test_cash_flow_margins_and_fcf_fallback():
    items = {"revenue": 1000, "net_income_controlling": 100, "net_income": 110, "operating_income": 150, "gross_profit": None,
             "cost_of_sales": 600, "depreciation_amortization": 50, "operating_cash_flow": 200, "capex": -80,
             "total_assets": 2000, "equity_controlling": 800, "total_equity": 900, "cash": 300, "current_assets": 700,
             "current_liabilities": 350, "inventories": 140, "total_liabilities": 1100, "short_term_borrowings": 100,
             "bonds": 200}
    s = summarize_financials([{"year": 2025, "q": 4, "items": items, "period_end": date(2025, 12, 31),
                               "filed_date": date(2026, 3, 10), "fs_div": "CFS"}])
    g, h = compute_financial_vars(s, 100, 2.0), compute_balance_vars(s, 100)
    assert g["free_cashflow"] == 120  # 200 − |−80|
    assert g["gross_margins"] == pytest.approx(0.4) and g["operating_margins"] == pytest.approx(0.15)
    assert g["ebitda_margins"] == pytest.approx(0.2) and h["ebitda"] == 200
    assert g["return_on_assets"] == pytest.approx(110 / 2000) and g["return_on_equity"] == pytest.approx(100 / 800)
    assert h["total_debt"] == 300 and h["current_ratio"] == pytest.approx(2.0) and h["quick_ratio"] == pytest.approx(560 / 350)
    assert h["debt_to_equity"] == pytest.approx(1100 / 900) and h["total_cash_per_share"] == 3
    items2 = {**items, "capex": None, "investing_cash_flow": -150}
    s2 = summarize_financials([{"year": 2025, "q": 4, "items": items2, "period_end": None, "filed_date": None, "fs_div": "CFS"}])
    assert compute_financial_vars(s2, 100, None)["free_cashflow"] == 50  # OCF + 투자활동CF


# ── C: 밸류에이션 ──────────────────────────────────────────────────────────

def test_valuation_vars():
    summary = {"ttm_net_income_controlling": 50, "ttm_ni_ctrl_prev": 40, "ttm_revenue": 400, "ttm_ebitda": 110,
               "cash": 100, "total_debt": 200, "equity_controlling": 500}
    v = compute_valuation_vars(summary, market_cap=1000, close=10, shares=100, forward_eps=0.6)
    assert v["trailing_pe"] == pytest.approx(20)
    assert v["forward_pe"] == pytest.approx(10 / 0.6)
    assert v["price_to_sales_ttm"] == pytest.approx(2.5)
    assert v["enterprise_value"] == 1100
    assert v["book_value_per_share"] == 5 and v["price_to_book"] == pytest.approx(2)
    assert v["peg_ratio"] == pytest.approx((10 / 0.6) / 20)  # 성장률 0.6/0.5 − 1 = 20%
    assert v["enterprise_to_revenue"] == pytest.approx(2.75) and v["enterprise_to_ebitda"] == pytest.approx(10)
    assert v["trailing_peg_ratio"] == pytest.approx(20 / 25)
    loss = compute_valuation_vars({**summary, "ttm_net_income_controlling": -5}, 1000, 10, 100, 0.4)
    assert loss["trailing_pe"] is None and loss["peg_ratio"] is None and loss["trailing_peg_ratio"] is None
    shrinking = compute_valuation_vars(summary, 1000, 10, 100, 0.4)  # 예상 EPS < 실적 EPS → PEG 없음
    assert shrinking["peg_ratio"] is None
    assert compute_valuation_vars({**summary, "cash": None}, 1000, 10, 100, None)["enterprise_value"] is None


# ── A/D: 시세·모멘텀 ───────────────────────────────────────────────────────

def _price_frame(n: int, start: float = 100.0, step: float = 0.5) -> pd.DataFrame:
    dates = [d.date() for d in pd.bdate_range(end=pd.Timestamp(AS_OF), periods=n)]
    close = np.array([start + i * step for i in range(n)])
    return pd.DataFrame({"open": close - 0.2, "high": close + 1, "low": close - 1, "close": close,
                         "volume": [1000.0 + i for i in range(n)], "trading_value": np.nan}, index=dates)


def test_price_vars_latest_two_days_and_averages():
    df = _price_frame(25)
    v = compute_price_vars(df)
    assert v["prev_close"] == df["close"].iloc[-2] and v["open"] == df["open"].iloc[-1]
    assert v["day_low"] == df["low"].iloc[-1] and v["day_high"] == df["high"].iloc[-1]
    assert v["volume"] == 1024 and v["average_volume"] == pytest.approx(np.mean(range(1005, 1025)))
    assert v["average_volume_10d"] == v["average_daily_volume_10d"] == pytest.approx(np.mean(range(1015, 1025)))
    assert v["regular_market_prev_close"] == v["prev_close"] and v["regular_market_volume"] == v["volume"]
    assert compute_price_vars(df.iloc[:5])["average_volume"] is None
    assert avg_trading_value(df) == pytest.approx((df["close"] * df["volume"]).iloc[-20:].mean())  # 거래대금 없으면 근사
    assert compute_quote_vars({"bid": 100, "ask": 101, "bid_size": 5, "ask_size": 7}) == {"bid": 100, "ask": 101, "bid_size": 5, "ask_size": 7}
    assert compute_quote_vars(None)["bid"] is None


def test_beta_against_benchmark():
    rng = np.random.default_rng(0)
    rb = rng.normal(0, 0.01, 260)
    dates = [d.date() for d in pd.bdate_range(end=pd.Timestamp(AS_OF), periods=261)]
    bench = pd.Series(100 * np.cumprod(np.r_[1, 1 + rb]), index=dates)
    stock = pd.Series(50 * np.cumprod(np.r_[1, 1 + 1.5 * rb]), index=dates)
    assert compute_beta(stock, bench) == pytest.approx(1.5, rel=1e-9)
    assert compute_beta(stock.iloc[-100:], bench) is None  # 관측치 120개 미만


def test_momentum_52w_and_moving_averages():
    df = _price_frame(300)
    bench = df["close"] * 2
    v = compute_momentum_vars(df, bench)
    last252 = df.iloc[-252:]
    assert v["fifty_two_week_low"] == last252["low"].min() and v["fifty_two_week_high"] == last252["high"].max()
    assert v["fifty_day_average"] == pytest.approx(df["close"].iloc[-50:].mean())
    assert v["two_hundred_day_average"] == pytest.approx(df["close"].iloc[-200:].mean())
    assert v["fifty_two_week_change"] == pytest.approx(df["close"].iloc[-1] / df["close"].iloc[-253] - 1)
    assert v["benchmark_52_week_change"] == pytest.approx(v["fifty_two_week_change"])
    assert v["beta"] is not None
    short = compute_momentum_vars(df.iloc[-100:], None)
    assert short["two_hundred_day_average"] is None and short["fifty_two_week_change"] is None and short["beta"] is None


# ── E: 공매도·지분구조 ────────────────────────────────────────────────────

OWNERSHIP = {"largest_holder_shares": 400.0, "largest_holder_pct": 40.0, "largest_holder_names": ["홍길동", "삼성물산"],
             "has_preferred": True,
             "major_holders": [{"name": "국민연금공단", "pct": 7.0, "rcept_dt": "2026-05-01", "rcept_no": "1"},
                               {"name": "삼성물산", "pct": 20.0, "rcept_dt": "2026-05-02", "rcept_no": "2"},
                               {"name": "블랙록", "pct": 5.1, "rcept_dt": "2026-01-02", "rcept_no": "3"},
                               {"name": "블랙록", "pct": 4.9, "rcept_dt": "2026-07-02", "rcept_no": "4"},
                               {"name": "미래기관", "pct": 6.0, "rcept_dt": "2026-12-01", "rcept_no": "5"}]}


def test_short_vars_and_float():
    dates = [d.date() for d in pd.bdate_range("2026-10-01", "2026-11-12")]
    shorts = pd.DataFrame({"short_volume": 10.0, "short_balance": 60.0}, index=dates)
    v = compute_short_vars(shorts, AS_OF, shares=1000, avg_volume_20d=20, ownership=OWNERSHIP)
    october = [d for d in dates if d.month == 10]
    assert v["shares_short"] == 10 and v["short_interest_date"] == "2026-11-12"
    assert v["shares_short_prior_month"] == 10 * len(october) and v["short_interest_date_prior_month"] == "2026-10-30"
    assert v["float_shares"] == 600 and v["shares_outstanding"] == 1000
    assert v["short_percent_of_outstanding"] == pytest.approx(0.06)
    assert v["short_ratio"] == pytest.approx(3.0) and v["short_percent_of_float"] == pytest.approx(0.1)
    assert v["insider_holdings_pct"] == pytest.approx(0.4)
    # 국민연금 7%만: 삼성물산은 최대주주 측, 블랙록은 최신 4.9%로 5% 미만, 미래기관은 기준일 이후 보고
    assert v["institutional_holdings_pct"] == pytest.approx(0.07)
    empty = compute_short_vars(None, AS_OF, 1000, 20, None)
    assert empty["shares_short"] is None and empty["float_shares"] is None and empty["institutional_holdings_pct"] is None


def test_ownership_snapshot_from_dart_rows():
    hyslr = [{"nm": "이건희", "stock_knd": "보통주", "trmend_posesn_stock_co": "249,273,200", "trmend_posesn_stock_qota_rt": "4.18", "rcept_no": "x"},
             {"nm": "삼성생명", "stock_knd": "보통주", "trmend_posesn_stock_co": "508,157,148", "trmend_posesn_stock_qota_rt": "8.51"},
             {"nm": "삼성생명", "stock_knd": "우선주", "trmend_posesn_stock_co": "1,268,546", "trmend_posesn_stock_qota_rt": "0.15"},
             {"nm": "계", "stock_knd": "보통주", "trmend_posesn_stock_co": "1,200,000,000", "trmend_posesn_stock_qota_rt": "20.10"}]
    totals = [{"se": "보통주", "istc_totqy": "5,969,782,550", "tesstk_co": "-"},
              {"se": "우선주", "istc_totqy": "822,886,700", "tesstk_co": "-"},
              {"se": "합계", "istc_totqy": "6,792,669,250"}]
    snap = compute_ownership_snapshot(hyslr, totals, [{"repror": "국민연금공단", "stkrt": "7.35", "rcept_dt": "2024-11-15", "rcept_no": "1"}])
    assert snap["largest_holder_shares"] == 1_200_000_000 and snap["largest_holder_pct"] == pytest.approx(20.10)
    assert snap["largest_holder_names"] == ["삼성생명", "이건희"] and snap["has_preferred"] is True
    assert snap["common_shares"] == 5_969_782_550 and snap["treasury_shares"] is None
    assert institutional_pct(snap, AS_OF) == pytest.approx(0.0735)
    no_total = compute_ownership_snapshot(hyslr[:2], totals[:1], [])
    assert no_total["largest_holder_pct"] == pytest.approx(12.69) and no_total["has_preferred"] is False


# ── B: 배당 ──────────────────────────────────────────────────────────────

DIVIDEND_HTML = """<table>
<tr><td>1. 배당구분</td><td>분기배당</td></tr>
<tr><td>2. 배당종류</td><td>현금배당</td></tr>
<tr><td>3. 1주당 배당금(원)</td><td>보통주식</td><td>4,601</td></tr>
<tr><td>종류주식</td><td>4,602</td></tr>
<tr><td>4. 시가배당률(%)</td><td>보통주식</td><td>1.7</td></tr>
<tr><td>5. 배당금총액(원)</td><td>30,003,024,457,217</td></tr>
<tr><td>6. 배당기준일</td><td>2026-09-30</td></tr>
<tr><td>7. 배당금지급 예정일자</td><td>2026-11-27</td></tr>
<tr><td>10. 이사회결의일(결정일)</td><td>2026-10-29</td></tr></table>"""


def test_parse_dividend_decision():
    out = parse_dividend_decision(DIVIDEND_HTML)
    assert out == {"kind": "분기배당", "dps": 4601, "yield_pct": 1.7, "record_date": date(2026, 9, 30), "pay_date": date(2026, 11, 27)}
    xml_style = DIVIDEND_HTML.replace("<td>", "<TE>").replace("</td>", "</TE>").replace("<tr>", "<TR>").replace("</tr>", "</TR>")
    assert parse_dividend_decision(xml_style)["dps"] == 4601  # DART XML 셀(TE)도 읽는다


def test_parse_alot_matter_three_years():
    rows = [{"se": "주당액면가액(원)", "stock_knd": "-", "thstrm": "100", "frmtrm": "5,000", "lwfr": "5,000"},
            {"se": "(연결)현금배당성향(%)", "stock_knd": "-", "thstrm": "21.90", "frmtrm": "14.10", "lwfr": "17.80"},
            {"se": "현금배당수익률(%)", "stock_knd": "보통주", "thstrm": "3.70", "frmtrm": "1.70", "lwfr": "1.60"},
            {"se": "현금배당수익률(%)", "stock_knd": "우선주", "thstrm": "4.50", "frmtrm": "2.10", "lwfr": "2.00"},
            {"se": "주당 현금배당금(원)", "stock_knd": "보통주", "thstrm": "1,416", "frmtrm": "42,500", "lwfr": "28,500"},
            {"se": "주당 현금배당금(원)", "stock_knd": "우선주", "thstrm": "1,417", "frmtrm": "42,550", "lwfr": "28,550"}]
    out = parse_alot_matter(rows, 2018)
    assert out[2018] == {"dps": 1416, "yield_pct": 3.7, "payout_pct": 21.9}
    assert out[2017]["dps"] == 42500 and out[2016]["yield_pct"] == 1.6


def _div(fy, ex, amount, filed, pay=None, yld=None, payout=None):
    return {"fiscal_year": fy, "ex_date": ex, "pay_date": pay, "dividend_amount": amount, "reported_yield": yld,
            "payout_ratio": payout, "filed_date": filed}


def test_dividend_vars_ttm_and_fallback():
    annual = [_div(y, None, 1000 + y - 2020, date(y + 1, 3, 15), yld=y - 2018.0, payout=25.0) for y in range(2019, 2026)]
    events = [_div(2025, date(2025, 12, 31), 400, date(2026, 1, 30), pay=date(2026, 4, 17)),
              _div(2026, date(2026, 3, 31), 300, date(2026, 4, 29), pay=date(2026, 5, 20)),
              _div(2026, date(2026, 6, 30), 300, date(2026, 7, 30), pay=date(2026, 8, 20)),
              _div(2026, date(2026, 9, 30), 300, date(2026, 10, 29), pay=date(2026, 11, 27)),
              _div(2025, date(2025, 9, 30), 250, date(2025, 10, 30))]  # 365일 밖
    v = compute_dividend_vars(annual + events, close=50_000, as_of=AS_OF)
    assert v["dividend_rate"] == 1005 and v["dividend_yield"] == pytest.approx(1005 / 50_000)
    assert v["payout_ratio"] == pytest.approx(0.25)
    assert v["five_year_avg_dividend_yield"] == pytest.approx(np.mean([3, 4, 5, 6, 7]) / 100)
    assert v["trailing_annual_dividend_rate"] == 1300 and v["trailing_annual_dividend_yield"] == pytest.approx(0.026)
    assert v["last_dividend_value"] == 300 and v["last_dividend_date"] == "2026-11-27"
    only_annual = compute_dividend_vars(annual, 50_000, AS_OF)
    assert only_annual["trailing_annual_dividend_rate"] == 1005 and only_annual["last_dividend_value"] is None


# ── F: 기본정보·액면분할 ────────────────────────────────────────────────────

SPLIT_HTML = """<table>
<tr><td>1. 주식분할 내용</td><td>구분</td><td>분할 전</td><td>분할 후</td></tr>
<tr><td>1주당 가액(원)</td><td>5,000</td><td>100</td></tr>
<tr><td>발행주식총수</td><td>보통주식(주)</td><td>128,386,494</td><td>6,419,324,700</td></tr>
<tr><td>3. 주식분할 일정</td><td>주주총회예정일</td><td>2018-03-23</td></tr>
<tr><td>신주권상장예정일</td><td>2018-05-04</td></tr></table>"""


def test_split_parse_and_share_jump():
    assert parse_split_decision(SPLIT_HTML) == {"factor": 50.0, "date": date(2018, 5, 4)}
    shares = pd.Series([100.0, 100.0, 500.0, 500.0], index=[date(2026, 1, d) for d in (5, 6, 7, 8)])
    assert split_from_share_jump(shares, date(2026, 1, 1)) == (5.0, date(2026, 1, 7))
    assert split_from_share_jump(shares.iloc[:2], date(2026, 1, 1)) == (None, None)


def test_info_vars():
    assert fiscal_year_ends(AS_OF, 12) == (date(2025, 12, 31), date(2026, 12, 31))
    assert fiscal_year_ends(AS_OF, 3) == (date(2026, 3, 31), date(2027, 3, 31))
    actions = [{"type": "split", "rcept_dt": "2018-01-31", "date": "2018-05-04", "factor": 50.0},
               {"type": "split", "rcept_dt": "2026-12-01", "date": "2027-01-10", "factor": 2.0}]  # 기준일 이후 공시
    v = compute_info_vars(AS_OF, 12, 1_000_000, 100, date(2026, 9, 30), actions)
    assert v["implied_shares_outstanding"] == 10_000 and v["currency"] == v["financial_currency"] == "KRW"
    assert v["last_split_factor"] == 50.0 and v["last_split_date"] == "2018-05-04"
    assert v["most_recent_quarter"] == "2026-09-30" and v["last_fiscal_year_end"] == "2025-12-31"


# ── I: 컨센서스 ──────────────────────────────────────────────────────────

FNGUIDE_HTML = """<html><input type="hidden" id="cmp_cd" value="005930" />
<table><caption>증권사별 적정주가 &amp; 투자의견</caption>
<tr><th>추정기관</th><th>추정일자</th><th>적정주가</th><th>투자의견</th></tr>
<tr><th>적정주가</th><th>직전 적정주가</th><th>증감율</th><th>투자의견</th><th>직전 투자의견</th></tr>
<tr><td>Consensus</td><td></td><td>400,000</td><td>410,000</td><td>-2.4</td><td>4.25</td><td>4.25</td></tr>
<tr><td>A증권</td><td>2026/11/12</td><td>300,000</td><td>300,000</td><td>0.00</td><td>5.00</td><td>5.00</td></tr>
<tr><td>B증권</td><td>2026/11/11</td><td>400,000</td><td>480,000</td><td>-16.67</td><td>4.00</td><td>4.00</td></tr>
<tr><td>C증권</td><td>2026/11/10</td><td>450,000</td><td>450,000</td><td>0.00</td><td>4.00</td><td>4.00</td></tr>
<tr><td>D증권</td><td>2026/11/02</td><td>450,000</td><td>450,000</td><td>0.00</td><td>4.00</td><td>4.00</td></tr>
</table>
<script>
 consensus.init({
     cmp_cd: '005930',
     perforTrend: {"data":[{"NAME":"EPS","UNIT":"(원)","VAL1":"6563.57","VAL2":"45654.80","VAL3":"73054.65"}],"header":[{"YYMM":"2025/12","EP_CHK":" ","CD":"VAL1"},{"YYMM":"2026/12","EP_CHK":"E","CD":"VAL2"},{"YYMM":"2027/12","EP_CHK":"E","CD":"VAL3"}]},
     perforTrendChart: {"data":[]},
 });
</script></html>"""


def test_fnguide_parse_and_conversion():
    out = parse_consensus_html(FNGUIDE_HTML, AS_OF)
    assert out["covered"] is True and out["n_analysts"] == 4
    assert out["target_mean"] == 400_000  # Consensus 행
    assert out["target_high"] == 450_000 and out["target_low"] == 300_000 and out["target_median"] == 425_000
    assert out["rec_mean"] == pytest.approx(1.75) and out["rec_key"] == "buy"  # 6 − 4.25
    w = ((date(2026, 12, 31).month - AS_OF.month) + (31 - 13) / 30) / 12
    assert out["forward_eps"] == pytest.approx(w * 45654.80 + (1 - w) * 73054.65)
    uncovered = parse_consensus_html('<table><caption>증권사별 적정주가</caption><tr><td>관련 데이터가 없습니다.</td></tr></table>', AS_OF)
    assert uncovered["covered"] is False and uncovered["target_mean"] is None and uncovered["n_analysts"] is None
    assert [rec_key(x) for x in (1.0, 2.0, 3.0, 4.0, 5.0)] == ["strong_buy", "buy", "hold", "underperform", "sell"]
    v = compute_consensus_vars({"covered": True, "target_high": 1, "target_low": 2, "target_mean": 3, "target_median": 4,
                                "rec_mean": 2.0, "rec_key": "buy", "n_analysts": 7})
    assert v["number_of_analyst_opinions"] == 7 and v["recommendation_key"] == "buy"
    assert compute_consensus_vars({"covered": False, "target_mean": 3})["target_mean_price"] is None


def test_forward_12m_eps_edges():
    est = [(date(2026, 12, 31), 100.0), (date(2027, 12, 31), 200.0)]
    assert forward_12m_eps(est, date(2026, 1, 1)) == pytest.approx(100 * (11 + 30 / 30) / 12 + 200 * (1 - (11 + 30 / 30) / 12))
    assert forward_12m_eps(est[:1], AS_OF) == 100.0
    assert forward_12m_eps([(date(2025, 12, 31), 50.0)], AS_OF) is None


# ── J: 지배구조 ──────────────────────────────────────────────────────────

def test_governance_levels():
    assert audit_risk_level("적정", "해당사항 없음") == 1
    assert audit_risk_level("적정", "계속기업 관련 중요한 불확실성") == 3
    assert audit_risk_level("한정") == 4
    assert audit_risk_level("부적정") == 5 and audit_risk_level("의견거절") == 5
    assert audit_risk_level(None) is None
    assert [board_risk_level(10, n) for n in (5, 4, 3, 1, 0)] == [1, 2, 3, 4, 5]
    assert [compensation_risk_level(p, 1000) for p in (5, 20, 40, 90, 100)] == [1, 2, 3, 4, 5]
    assert compensation_risk_level(1, 0) == 5 and compensation_risk_level(1, None) is None
    assert [shareholder_rights_level(p) for p in (20, 40, 60)] == [2, 3, 4]
    assert shareholder_rights_level(60, True) == 5 and shareholder_rights_level(20, True) == 3
    assert overall_risk_level({"audit_risk": 1, "board_risk": 2, "compensation_risk": None, "shareholder_rights_risk": 3}) == \
        round((0.30 * 1 + 0.25 * 2 + 0.25 * 3) / 0.80, 1)
    assert overall_risk_level({"audit_risk": None}) is None


def test_score_governance_from_dart_rows():
    scored = score_governance(
        audit_rows=[{"bsns_year": "제57기(당기)", "adtor": "삼일", "adt_opinion": "적정", "emphs_matter": "해당사항 없음"},
                    {"bsns_year": "제56기(전기)", "adt_opinion": "한정"}],
        director_rows=[{"drctr_co": "11", "otcmp_drctr_co": "6"}],
        pay_rows=[{"fscl_year": "당기", "mendng_totamt": "29,607,000,000"}, {"fscl_year": "전기", "mendng_totamt": "1"}],
        ownership={"largest_holder_pct": 20.1, "has_preferred": True}, net_income=34_000_000_000_000)
    assert scored["levels"] == {"audit_risk": 1, "board_risk": 1, "compensation_risk": 1, "shareholder_rights_risk": 3,
                                "overall_risk": round((0.30 + 0.25 + 0.20 + 0.25 * 3) / 1.0, 1)}
    assert compute_governance_vars({"audit_risk": 1, "comp_risk": 2, "shareholder_risk": 3})["compensation_risk"] == 2


# ── 원천 클라이언트 (가짜 세션) ─────────────────────────────────────────────

class FakeResp:
    def __init__(self, payload=None, content=b""):
        self.payload, self.content = payload, content

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append((url, params))
        return self.responses.pop(0)


def test_dart_client_status_codes_and_paging(tmp_path):
    page1 = {"status": "000", "total_page": 2, "list": [{"rcept_no": "20261112000001", "report_nm": "현금ㆍ현물배당결정   ", "stock_code": "005930 "}]}
    page2 = {"status": "000", "total_page": 2, "list": [{"rcept_no": "20261112000002", "report_nm": "주식분할결정", "stock_code": ""}]}
    sess = FakeSession([FakeResp(page1), FakeResp(page2), FakeResp({"status": "013", "message": "조회된 데이타가 없습니다."}),
                        FakeResp({"status": "020", "message": "요청 제한"}), FakeResp({"status": "010", "message": "미등록 키"})])
    client = DartClient("k" * 40, cache_dir=tmp_path, session=sess, min_interval=0)
    items = client.list_disclosures("20261101", date(2026, 11, 12), pblntf_ty="I")
    assert [i["rcept_no"] for i in items] == ["20261112000001", "20261112000002"]
    assert items[0]["report_nm"] == "현금ㆍ현물배당결정" and items[0]["stock_code"] == "005930"
    assert sess.calls[0][1]["end_de"] == "20261112" and sess.calls[1][1]["page_no"] == 2 and "corp_code" not in sess.calls[0][1]
    assert client.dividends("00126380", 2025) == []  # 013 → 빈 결과
    with pytest.raises(DartRateLimitError):
        client.company("00126380")
    with pytest.raises(DartKeyError):
        client.company("00126380")
    assert filed_date_from_rcept_no("20261029800101") == date(2026, 10, 29)


def test_dart_corp_codes_zip_cache(tmp_path):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", "<result><list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
                                    "<stock_code>005930</stock_code></list><list><corp_code>00000001</corp_code>"
                                    "<stock_code> </stock_code></list></result>")
    sess = FakeSession([FakeResp(content=buf.getvalue())])
    client = DartClient("k" * 40, cache_dir=tmp_path, session=sess, min_interval=0)
    assert client.corp_codes() == {"005930": "00126380"}
    assert (tmp_path / "dart_corpcode.zip").exists()
    assert DartClient("k" * 40, cache_dir=tmp_path, session=FakeSession([])).corp_codes() == {"005930": "00126380"}  # 캐시 사용


def test_ecos_parse():
    payload = {"KeyStatisticList": {"list_total_count": 2, "row": [
        {"CLASS_NAME": "환율", "KEYSTAT_NAME": "원/달러 환율(종가)", "DATA_VALUE": "1330.7", "CYCLE": "20261112", "UNIT_NAME": "원"},
        {"CLASS_NAME": "x", "KEYSTAT_NAME": "y", "DATA_VALUE": "", "CYCLE": "2026Q2", "UNIT_NAME": "%"}]}}
    rows, total = ecos.parse_key_statistics(payload)
    assert total == 2 and rows[0] == {"name": "원/달러 환율(종가)", "value": 1330.7, "unit": "원", "period": "20261112", "class_name": "환율"}
    assert rows[1]["value"] is None
    with pytest.raises(ecos.EcosError):
        ecos.parse_key_statistics({"RESULT": {"CODE": "ERROR-301", "MESSAGE": "bad"}})
    assert ecos.key_statistics("sample", session=FakeSession([FakeResp(payload)]))[0]["period"] == "20261112"


def test_naver_parsers():
    payload = [{"total": 1, "items": [{"officeId": "016", "articleId": "0002722968", "officeName": "헤럴드경제",
                                       "datetime": "202611130952", "title": "&quot;삼성&quot; <b>상승</b>", "mobileNewsUrl": None}]}]
    items = naver.parse_stock_news(payload)
    assert items[0]["title"] == '"삼성" 상승' and items[0]["source"] == "헤럴드경제"
    assert items[0]["url"] == "https://n.news.naver.com/mnews/article/016/0002722968"
    assert items[0]["published_at"] == datetime(2026, 11, 13, 9, 52, tzinfo=KST)
    board = {"result": {"lastOffset": "-5", "posts": [{"title": "삼전 &amp; 하닉"}, {"title": "공시"}]}}
    sess = FakeSession([FakeResp(board), FakeResp({"result": {"posts": []}})])
    assert naver.fetch_board_titles("005930", pages=2, session=sess) == ["삼전 & 하닉", "공시"]
    assert sess.calls[1][1]["offset"] == "-5"


class FakeStock:
    """pykrx.stock 모듈 대역 (한글 컬럼 그대로)."""

    idx = pd.to_datetime(["2026-11-11", "2026-11-12"])

    def get_market_ohlcv_by_date(self, f, t, ticker):
        return pd.DataFrame({"시가": [10, 11], "고가": [12, 13], "저가": [9, 10], "종가": [11, 12], "거래량": [100, 200],
                             "거래대금": [1100, 2400], "등락률": [0.0, 9.1]}, index=self.idx)

    def get_market_cap_by_date(self, f, t, ticker):
        return pd.DataFrame({"시가총액": [1100, 1200], "거래량": [100, 200], "거래대금": [1100, 2400], "상장주식수": [100, 100]}, index=self.idx)

    def get_index_ohlcv_by_date(self, f, t, code):
        assert code == "1028"
        return pd.DataFrame({"시가": [1, 1], "고가": [1, 1], "저가": [1, 1], "종가": [800.0, 810.0], "거래량": [1, 1], "거래대금": [1, 1]}, index=self.idx)

    def get_index_portfolio_deposit_file(self, code, d, alternative=False):
        return {"1028": ["005930", "000660"], "2203": ["247540"]}[code]

    def get_shorting_volume_by_date(self, f, t, ticker):
        return pd.DataFrame({"공매도": [5, 6], "매수": [100, 200], "비중": [0.05, 0.03]}, index=self.idx)

    def get_shorting_balance_by_date(self, f, t, ticker):
        return pd.DataFrame({"공매도잔고": [50, 55], "상장주식수": [100, 100], "공매도금액": [1, 1], "시가총액": [1, 1], "비중": [0.5, 0.55]},
                            index=self.idx)

    def get_exhaustion_rates_of_foreign_investment_by_date(self, f, t, ticker):
        return pd.DataFrame({"상장주식수": [100, 100], "보유수량": [40, 46], "지분율": [40.0, 46.07], "한도수량": [100, 100],
                             "한도소진률": [40.0, 46.07]}, index=self.idx)


def test_pykrx_source_with_mock_module():
    src = PykrxSource("id", "pw", stock=FakeStock())
    df = src.ohlcv("005930", date(2026, 11, 11), date(2026, 11, 12))
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "trading_value", "market_cap", "shares"]
    assert df.index[-1] == date(2026, 11, 12) and df["trading_value"].iloc[-1] == 2400 and df["shares"].iloc[-1] == 100
    assert src.index_closes(date(2026, 11, 11), date(2026, 11, 12)).iloc[-1] == 810.0
    assert src.index_constituents("KOSPI200", AS_OF) == (["005930", "000660"], False)
    short = src.short_selling("005930", date(2026, 11, 11), date(2026, 11, 12))
    assert short.loc[date(2026, 11, 12)].to_dict() == {"short_volume": 6.0, "short_balance": 55.0, "short_ratio": 0.55}
    assert src.foreign_ownership("005930", AS_OF) == pytest.approx(0.4607)


def test_top_by_market_cap_approximation():
    listing = pd.DataFrame({"ticker": ["005930", "005935", "000660", "123450", "999990"],
                            "name": ["삼성전자", "삼성전자우", "SK하이닉스", "에이스팩", "작은회사"],
                            "market": "KOSPI", "market_cap": [5e14, 1e14, 4e14, 3e14, 1e9]})
    assert top_by_market_cap(listing, "KOSPI", 2) == ["005930", "000660"]  # 우선주·스팩 제외


# ── 시드 DB → 팩트시트 ────────────────────────────────────────────────────

def _fin_items(ni, rev):
    return {"net_income_controlling": ni, "net_income": ni + 1, "revenue": rev, "operating_income": rev * 0.2,
            "depreciation_amortization": rev * 0.05, "operating_cash_flow": rev * 0.25, "capex": rev * 0.1,
            "total_assets": 5000, "total_liabilities": 2000, "total_equity": 3000, "equity_controlling": 2900,
            "current_assets": 1500, "current_liabilities": 700, "inventories": 300, "cash": 400,
            "short_term_borrowings": 100, "long_term_borrowings": 200}


def seed(db: Database, *, q3_filed: date = date(2026, 11, 10)) -> None:
    with db.session() as s:
        s.add(StockMaster(ticker="005930", name="삼성전자", market="KOSPI", sector="정보기술", settle_month=12, in_kospi200=True,
                          risk_flags=["테스트"], corporate_actions=[
                              {"type": "split", "rcept_no": "20180131800001", "rcept_dt": "2018-01-31", "date": "2018-05-04", "factor": 50.0}]))
        dates = [d.date() for d in pd.bdate_range(end=pd.Timestamp(AS_OF), periods=300)]
        for i, d in enumerate(dates):
            close = 100 + i * 0.5
            s.add(PriceData(ticker="005930", date=d, open=close, high=close + 1, low=close - 1, close=close, volume=1000 + i,
                            trading_value=close * (1000 + i), market_cap=close * 1000, shares=1000,
                            foreign_ratio=0.5 if i == 299 else None))
            s.add(PriceData(ticker=BENCHMARK_TICKER, date=d, close=300 + i * 0.3 + 5 * math.sin(i)))
        reports = {"2022Q4": (50, 300), "2023Q4": (55, 330), "2024Q3": (30, 300), "2024Q4": (50, 400), "2025Q1": (10, 100),
                   "2025Q2": (25, 210), "2025Q3": (45, 330), "2025Q4": (70, 450), "2026Q1": (15, 120), "2026Q2": (35, 250),
                   "2026Q3": (60, 390)}
        for label, (ni, rev) in reports.items():
            y, q = int(label[:4]), int(label[-1])
            filed = q3_filed if label == "2026Q3" else quarter_end(y, q) + timedelta(days=60 if q == 4 else 45)
            s.add(FinancialData(ticker="005930", quarter=label, period_end=quarter_end(y, q), report_code="11011", fs_div="CFS",
                                filed_date=filed, revenue=rev, net_income=ni, items=_fin_items(ni, rev)))
        for y in range(2021, 2026):
            s.add(DividendData(ticker="005930", fiscal_year=y, ex_date=None, dividend_amount=1000 + y - 2021,
                               reported_yield=2.0, payout_ratio=30.0, filed_date=date(y + 1, 3, 15)))
        for ex, filed in ((date(2026, 3, 31), date(2026, 4, 29)), (date(2026, 6, 30), date(2026, 7, 30)),
                          (date(2026, 9, 30), date(2026, 10, 29))):
            s.add(DividendData(ticker="005930", fiscal_year=2026, ex_date=ex, pay_date=ex + timedelta(days=50),
                               dividend_amount=370, filed_date=filed))
        for d in pd.bdate_range("2026-10-01", "2026-11-12"):
            s.add(ShortInterest(ticker="005930", date=d.date(), short_volume=10, short_balance=50, short_ratio=0.5))
        s.add(ConsensusData(ticker="005930", date=date(2026, 11, 1), target_high=400, target_low=200, target_mean=300,
                            target_median=290, rec_mean=2.0, rec_key="buy", n_analysts=20, forward_eps=12.5, covered=True))
        s.add(ConsensusData(ticker="005930", date=date(2026, 11, 20), target_high=999, target_mean=999, forward_eps=99, covered=True))
        snap = {**OWNERSHIP, "filed_date": "2026-11-10", "reprt_code": "11014"}
        old_snap = {**OWNERSHIP, "largest_holder_shares": 500.0, "filed_date": "2026-08-14", "reprt_code": "11012"}
        s.add(GovernanceData(ticker="005930", year=2025, audit_risk=1, board_risk=2, comp_risk=1, shareholder_risk=4,
                             overall_risk=2.0, details={"filed_date": "2026-03-15", "ownership": {"11014": snap, "11012": old_snap}}))


def test_build_factsheet_from_seeded_db(mem_db):
    seed(mem_db)
    with mem_db.session() as s:
        fs = build_factsheet(s, "005930", AS_OF)
    assert isinstance(fs, Factsheet) and fs.name == "삼성전자" and fs.sector == "정보기술"
    assert list(fs.variables) == list(ORDERED_KEYS) and len(fs.variables) == 97
    assert set(fs.freshness) == set(ORDERED_KEYS) and set(fs.freshness.values()) <= {"fresh", "stale", "missing"}
    v, f = fs.variables, fs.freshness
    close = 100 + 299 * 0.5
    assert fs.extras["last_close"] == close and fs.extras["foreign_ownership"] == 0.5 and fs.extras["risk_flags"] == ["테스트"]
    assert fs.extras["avg_trading_value_20d"] == pytest.approx(np.mean([(100 + i * 0.5) * (1000 + i) for i in range(280, 300)]))
    assert v["prev_close"] == close - 0.5 and v["market_cap"] == close * 1000 and v["shares_outstanding"] == 1000
    assert v["net_income_to_common"] == 85 and v["trailing_pe"] == pytest.approx(close * 1000 / 85)
    assert v["forward_eps"] == 12.5 and v["forward_pe"] == pytest.approx(close / 12.5)
    assert v["total_debt"] == 300 and v["enterprise_value"] == pytest.approx(close * 1000 + 300 - 400)
    assert v["earnings_growth"] == pytest.approx((70 / 50) ** (1 / 3) - 1)
    assert v["most_recent_quarter"] == "2026-09-30" and v["last_split_factor"] == 50.0
    assert v["dividend_rate"] == 1004 and v["trailing_annual_dividend_rate"] == 1110 and v["last_dividend_value"] == 370
    assert v["float_shares"] == 600  # 최신(11014) 지분 스냅샷
    assert v["shares_short"] == 10 and v["short_percent_of_float"] == pytest.approx(50 / 600)
    assert v["target_mean_price"] == 300 and v["overall_risk"] == 2.0 and v["beta"] is not None
    assert v["currency"] == "KRW" and v["bid"] is None
    for key in ("prev_close", "trailing_pe", "price_to_sales_ttm", "beta", "dividend_rate", "shares_short", "float_shares",
                "target_mean_price", "overall_risk", "currency", "last_split_date", "return_on_equity"):
        assert f[key] == "fresh", key
    assert f["bid"] == "missing" and f["last_dividend_date"] == "fresh"
    data = json.loads(fs.to_prompt_json())
    keys = list(data)
    assert keys[0] == "#1 전일 종가" and "#14 PER (TTM)" in keys and keys[-1] == "_stale_or_missing"
    assert keys.index("#14 PER (TTM)") < keys.index("#97 종합 리스크")
    assert "#21:missing" in data["_stale_or_missing"] and "#1:fresh" not in data["_stale_or_missing"]
    assert len(keys) == 98


def test_quotes_and_persist_via_build_factsheets(mem_db):
    seed(mem_db)

    class Quotes:
        def best_quote(self, ticker):
            return {"bid": 249.0, "ask": 250.0, "bid_size": 10, "ask_size": 12}

    out = build_factsheets(mem_db, ["005930", "NOPE00"], AS_OF, quotes=Quotes(), persist=True)
    fs = out["005930"]
    assert fs.variables["bid"] == 249.0 and fs.freshness["ask_size"] == "fresh"
    assert out["NOPE00"].variables["market_cap"] is None  # 데이터 없는 종목도 실패하지 않는다
    with mem_db.session() as s:
        snap = s.scalars(select(FactorSnapshot).where(FactorSnapshot.ticker == "005930")).one()
        assert snap.as_of == AS_OF and snap.variables["ask"] == 250.0 and len(snap.variables) == 97


def test_lookahead_guard_ignores_future_filings(mem_db):
    seed(mem_db, q3_filed=date(2026, 11, 20))  # 3분기보고서가 기준일 뒤에 접수됨
    with mem_db.session() as s:
        s.add(DividendData(ticker="005930", fiscal_year=2026, ex_date=date(2026, 11, 5), dividend_amount=9999,
                           filed_date=date(2026, 11, 14)))
        s.add(GovernanceData(ticker="005930", year=2026, audit_risk=5, board_risk=5, comp_risk=5, shareholder_risk=5,
                             overall_risk=5.0, details={"filed_date": "2027-03-15"}))
    with mem_db.session() as s:
        fs = build_factsheet(s, "005930", AS_OF)
    v = fs.variables
    assert v["most_recent_quarter"] == "2026-06-30"
    assert v["net_income_to_common"] == 35 + 70 - 25  # 2026 반기 기준 TTM
    assert v["target_mean_price"] == 300 and v["forward_eps"] == 12.5  # 11/20 컨센서스 무시
    assert v["last_dividend_value"] == 370  # 기준일 뒤 접수된 배당 이벤트 무시
    assert v["overall_risk"] == 2.0  # 2027년 접수 지배구조 무시
    assert v["float_shares"] == 600
    with mem_db.session() as s:
        early = build_factsheet(s, "005930", date(2026, 9, 1))
    assert early.variables["float_shares"] == 500  # 8/14 반기 스냅샷 (11/10 것은 미래)
    assert early.variables["prev_close"] is not None and early.extras["price_date"] <= "2026-09-01"


def test_stale_flags_when_data_is_old(mem_db):
    seed(mem_db)
    later = AS_OF + timedelta(days=60)
    with mem_db.session() as s:
        fs = build_factsheet(s, "005930", later)
    assert fs.freshness["prev_close"] == "stale" and fs.freshness["target_mean_price"] == "stale"
    assert fs.freshness["price_to_sales_ttm"] == "fresh"  # 분기 한도 135일 이내
    assert fs.freshness["currency"] == "fresh"


# ── 유니버스·뉴스 ────────────────────────────────────────────────────────

def test_liquid_universe_excludes_bottom_by_trading_value(mem_db):
    with mem_db.session() as s:
        for n in range(1, 11):
            t = f"{n:06d}"
            s.add(StockMaster(ticker=t, name=t, market="KOSPI", in_kospi200=True))
            for d in pd.bdate_range(end=pd.Timestamp(AS_OF), periods=25):
                s.add(PriceData(ticker=t, date=d.date(), close=100.0, volume=n * 10.0,
                                trading_value=None if n == 3 else n * 1000.0))
        s.add(StockMaster(ticker="459580", name="CD", market="ETF", is_cash_etf=True))
    with mem_db.session() as s:
        out = liquid_universe(s, AS_OF, 0.10)
    assert len(out) == 9 and out[0] == "000010" and "000001" not in out and "000003" in out  # 3번은 종가×거래량 근사
    with mem_db.session() as s:
        assert liquid_universe(s, date(2000, 1, 1), 0.1) == []


class FakeMarket:
    name = "fake"

    def index_constituents(self, index, as_of):
        return (["005930", "000660"], True) if index == "KOSPI200" else (["247540"], True)

    def listing(self, as_of):
        return pd.DataFrame({"ticker": ["005930", "000660", "247540"], "name": ["삼성전자", "SK하이닉스", "에코프로비엠"],
                             "market": ["KOSPI", "KOSPI", "KOSDAQ"], "industry": ["반도체 제조업", "반도체 제조업", "일차전지 및 이차전지 제조업"],
                             "settle_month": [12, 12, 12], "market_cap": [5e14, 4e14, 1e13], "shares": [1, 1, 1], "close": [1, 1, 1]})

    def short_selling(self, *a):
        return None


def test_refresh_universe_with_fake_market(mem_db, fast_settings):
    with mem_db.session() as s:
        s.add(StockMaster(ticker="111110", name="탈락", market="KOSPI", in_kospi200=True))
    ctx = CollectorContext(fast_settings, mem_db, AS_OF, FakeMarket())
    assert refresh_universe(ctx) == ["000660", "005930", "247540"]
    with mem_db.session() as s:
        m = s.get(StockMaster, "247540")
        assert m.in_kosdaq150 and m.market == "KOSDAQ" and m.sector == "산업재" and m.settle_month == 12
        assert not s.get(StockMaster, "111110").in_kospi200
        etf = s.get(StockMaster, "459580")
        assert etf.is_cash_etf and etf.sector == "현금성"
        status = s.scalars(select(CollectionStatus).where(CollectionStatus.job == "universe")).one()
        assert "근사" in status.error


def test_recent_news_window(mem_db):
    items = [{"title": f"t{d}", "source": "s", "url": f"u{d}", "published_at": datetime(2026, 11, d, 9, 0, tzinfo=KST), "kind": "news"}
             for d in (6, 7, 13)]
    items.append({"title": "late", "source": "s", "url": "late", "published_at": datetime(2026, 11, 14, 0, 30, tzinfo=KST), "kind": "news"})
    items.append({"title": "공시", "source": "DART", "url": "d", "published_at": datetime(2026, 11, 7, tzinfo=KST), "kind": "disclosure"})
    with mem_db.session() as s:
        store_items(s, "005930", items)
    with mem_db.session() as s:
        out = recent_news(s, "005930", AS_OF, 7)
    assert [o["title"] for o in out] == ["t13", "t7", "공시"]  # (11/6, 11/13] — 11/6과 11/14 제외
    assert out[0]["published_at"].startswith("2026-11-13T09:00") and out[-1]["kind"] == "disclosure"


# ── run_job: 재시도·회로 차단기 ───────────────────────────────────────────

def test_run_job_retries_then_isolates_ticker_failure(mem_db, fast_settings):
    calls: dict[str, int] = {}

    def fn(ctx, ticker):
        calls[ticker] = calls.get(ticker, 0) + 1
        if ticker == "BAD" or (ticker == "FLAKY" and calls[ticker] < 3):
            raise RuntimeError("boom")

    ctx = CollectorContext(fast_settings, mem_db, AS_OF, market=None)
    res = run_job(ctx, "price", fn, ["A", "BAD", "FLAKY", "B"])
    assert res["ok"] == ["A", "FLAKY", "B"] and res["failed"] == ["BAD"] and res["status"] == "partial"
    assert calls == {"A": 1, "BAD": 3, "FLAKY": 3, "B": 1}
    with mem_db.session() as s:
        bad = s.scalars(select(CollectionStatus).where(CollectionStatus.job == "price", CollectionStatus.ticker == "BAD")).one()
        job = s.scalars(select(CollectionStatus).where(CollectionStatus.job == "price", CollectionStatus.ticker == "*")).one()
        assert bad.status == "failed" and bad.consecutive_failures == 1 and "boom" in bad.error
        assert job.status == "ok" and job.consecutive_failures == 0


def test_circuit_breaker_opens_and_recovers(mem_db, fast_settings):
    calls = []
    down = {"value": True}

    def fn(ctx, ticker):
        calls.append(ticker)
        if down["value"]:
            raise ConnectionError("source down")

    ctx = CollectorContext(fast_settings, mem_db, AS_OF, market=None)
    tickers = ["T1", "T2", "T3", "T4", "T5"]
    res = run_job(ctx, "consensus", fn, tickers)
    assert res["status"] == "circuit_open" and res["failed"] == ["T1", "T2", "T3"] and res["skipped"] == ["T4", "T5"]
    assert len(calls) == 9  # 3종목 × 3회
    again = run_job(ctx, "consensus", fn, tickers)
    assert again["status"] == "circuit_open" and again["skipped"] == tickers and len(calls) == 9  # 쿨다운 중 호출 없음
    with mem_db.session() as s:
        job = s.scalars(select(CollectionStatus).where(CollectionStatus.job == "consensus", CollectionStatus.ticker == "*")).one()
        assert job.status == "failed" and job.consecutive_failures == 3
        job.last_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=601)
    down["value"] = False
    recovered = run_job(ctx, "consensus", fn, tickers)
    assert recovered["status"] == "ok" and recovered["ok"] == tickers
    with mem_db.session() as s:
        t1 = s.scalars(select(CollectionStatus).where(CollectionStatus.job == "consensus", CollectionStatus.ticker == "T1")).one()
        assert t1.status == "ok" and t1.consecutive_failures == 0
    # 이전 성공 이력이 있는 종목이 다시 실패하면 stale(직전 스냅샷 폴백)로 보고된다
    down["value"] = True
    assert run_job(ctx, "consensus", fn, ["T1"])["stale"] == ["T1"]


def test_run_collectors_job_names_and_skips(mem_db, fast_settings):
    assert set(COLLECTORS) == {"price", "dividend", "valuation", "momentum", "short_interest", "info", "financials",
                               "balance_sheet", "consensus", "governance", "news", "macro"}
    ctx = CollectorContext(fast_settings, mem_db, AS_OF, FakeMarket(), dart=None)
    res = run_collectors(ctx, ["dividend", "financials", "short_interest", "governance", "info"], ["005930"])
    assert all(r["status"] == "skipped" for r in res.values())
    with pytest.raises(ValueError):
        run_collectors(ctx, ["nope"], [])


def test_registry_frequencies_have_thresholds():
    from autopilot_korea.collectors.factsheet import FRESH_DAYS, source_of

    for key in ORDERED_KEYS:
        var = BY_KEY[key]
        src = source_of(key)
        assert src in {"price", "quote", "financial", "dividend_annual", "dividend_ttm", "dividend_event", "consensus", "short",
                       "ownership", "governance", "static"}
        if src not in ("static", "quote"):
            assert var.frequency in FRESH_DAYS, key


# ── DART 의존 수집 경로 (가짜 DART 클라이언트) ─────────────────────────────

class FakeDart:
    """DartClient와 같은 메서드 이름·응답 필드를 쓰는 대역."""

    def __init__(self):
        self.calls: list[str] = []

    def corp_codes(self):
        return {"005930": "00126380"}

    def company(self, corp_code):
        self.calls.append("company")
        return {"status": "000", "acc_mt": "12"}

    def list_disclosures(self, bgn_de, end_de, pblntf_ty=None, corp_code=None):
        self.calls.append("list")
        items = [{"rcept_no": "20261029800101", "rcept_dt": "20261029", "report_nm": "현금ㆍ현물배당결정", "stock_code": "005930"},
                 {"rcept_no": "20240131800001", "rcept_dt": "20240131", "report_nm": "주식분할결정", "stock_code": "005930"},
                 {"rcept_no": "20261101000001", "rcept_dt": "20261101", "report_nm": "기업설명회(IR)개최", "stock_code": "005930"}]
        lo, hi = str(bgn_de).replace("-", "")[:8], str(end_de).replace("-", "")[:8]
        return [i for i in items if lo <= i["rcept_dt"] <= hi]

    def document(self, rcept_no):
        self.calls.append("document")
        return DIVIDEND_HTML if rcept_no == "20261029800101" else SPLIT_HTML

    def financial_statements(self, corp_code, year, reprt_code, fs_div="CFS"):
        self.calls.append(f"fs:{year}:{reprt_code}:{fs_div}")
        if fs_div != "CFS":
            return []
        q = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}[reprt_code]
        scale = (year - 2020) * 10 + q
        rcept = (quarter_end(year, q) + timedelta(days=45)).strftime("%Y%m%d") + "000001"
        is_amt = {"thstrm_add_amount": None} if q == 4 else {}
        return [{**_line("IS", "ifrs-full_Revenue", "매출액", str(1000 * scale), None if q == 4 else str(1000 * scale)), **is_amt, "rcept_no": rcept},
                {**_line("IS", "ifrs-full_ProfitLossAttributableToOwnersOfParent", "지배기업 소유주지분", str(100 * scale)), "rcept_no": rcept},
                {**_line("IS", "dart_OperatingIncomeLoss", "영업이익", str(150 * scale)), "rcept_no": rcept},
                {**_line("BS", "ifrs-full_Assets", "자산총계", "50000"), "rcept_no": rcept},
                {**_line("BS", "ifrs-full_Liabilities", "부채총계", "20000"), "rcept_no": rcept},
                {**_line("BS", "ifrs-full_Equity", "자본총계", "30000"), "rcept_no": rcept},
                {**_line("BS", "ifrs-full_CashAndCashEquivalents", "현금및현금성자산", "4000"), "rcept_no": rcept},
                {**_line("CF", "ifrs-full_CashFlowsFromUsedInOperatingActivities", "영업활동현금흐름", str(200 * scale)), "rcept_no": rcept}]

    def dividends(self, corp_code, bsns_year, reprt_code="11011"):
        self.calls.append(f"alot:{bsns_year}")
        rcept = f"{bsns_year + 1}0315000001"
        return [{"rcept_no": rcept, "se": "주당 현금배당금(원)", "stock_knd": "보통주", "thstrm": str(1000 + bsns_year - 2020),
                 "frmtrm": str(999 + bsns_year - 2020), "lwfr": str(998 + bsns_year - 2020)},
                {"rcept_no": rcept, "se": "현금배당수익률(%)", "stock_knd": "보통주", "thstrm": "2.0", "frmtrm": "2.0", "lwfr": "2.0"},
                {"rcept_no": rcept, "se": "(연결)현금배당성향(%)", "stock_knd": "-", "thstrm": "25.0", "frmtrm": "25.0", "lwfr": "25.0"}]

    def largest_holders(self, corp_code, bsns_year, reprt_code):
        rcept = f"{bsns_year}1114000001" if reprt_code == "11014" else f"{bsns_year + 1}0315000001"
        return [{"rcept_no": rcept, "nm": "홍길동", "stock_knd": "보통주", "trmend_posesn_stock_co": "300", "trmend_posesn_stock_qota_rt": "30.0"},
                {"rcept_no": rcept, "nm": "계열사", "stock_knd": "보통주", "trmend_posesn_stock_co": "100", "trmend_posesn_stock_qota_rt": "10.0"}]

    def stock_totals(self, corp_code, bsns_year, reprt_code):
        return [{"se": "보통주", "istc_totqy": "1,000", "tesstk_co": "10"}, {"se": "우선주", "istc_totqy": "100"}]

    def major_holdings(self, corp_code):
        return [{"rcept_no": "20260501000001", "rcept_dt": "2026-05-01", "repror": "국민연금공단", "stkrt": "7.0"}]

    def audit_opinions(self, corp_code, bsns_year, reprt_code="11011"):
        if bsns_year != 2025:
            return []
        return [{"rcept_no": "20260315000001", "bsns_year": "제57기(당기)", "adtor": "삼일", "adt_opinion": "적정", "emphs_matter": "-"}]

    def outside_directors(self, corp_code, bsns_year, reprt_code="11011"):
        return [{"drctr_co": "9", "otcmp_drctr_co": "3"}]

    def director_pay(self, corp_code, bsns_year, reprt_code="11011"):
        return [{"fscl_year": "-", "mendng_totamt": "20"}]


def test_dart_collect_paths_end_to_end(mem_db, fast_settings):
    with mem_db.session() as s:
        s.add(StockMaster(ticker="005930", name="삼성전자", market="KOSPI", in_kospi200=True, corporate_actions=[]))
    dart = FakeDart()
    ctx = CollectorContext(fast_settings, mem_db, AS_OF, FakeMarket(), dart=dart)
    res = run_collectors(ctx, ["financials", "balance_sheet", "dividend", "info", "governance"], ["005930"])
    assert all(r["status"] == "ok" for r in res.values()), res
    assert not any(":2026:11011" in c for c in dart.calls)  # 아직 나올 수 없는 2026 사업보고서는 조회하지 않는다
    n_fs_calls = len([c for c in dart.calls if c.startswith("fs:")])
    COLLECTORS["balance_sheet"](ctx, ["005930"])
    assert len([c for c in dart.calls if c.startswith("fs:")]) == n_fs_calls  # 이미 받은 보고서는 다시 받지 않는다
    with mem_db.session() as s:
        fins = {r.quarter: r for r in s.scalars(select(FinancialData)).all()}
        assert set(fins) == {"2022Q4", "2023Q4", "2024Q1", "2024Q2", "2024Q3", "2024Q4", "2025Q1", "2025Q2", "2025Q3", "2025Q4",
                             "2026Q1", "2026Q2", "2026Q3"}
        q3 = fins["2026Q3"]
        assert q3.filed_date == date(2026, 11, 14) and q3.period_end == date(2026, 9, 30) and q3.items["revenue"] == 63000
        assert q3.net_income == 6300 and q3.items["inventories"] is None
        annual = {r.fiscal_year: r for r in s.scalars(select(DividendData).where(DividendData.ex_date.is_(None))).all()}
        assert annual[2025].dividend_amount == 1005 and annual[2025].filed_date == date(2026, 3, 15)
        assert annual[2019].dividend_amount == 999 and annual[2019].filed_date > date(2020, 12, 31)  # 이후 보고서의 전기·전전기 값으로 채운 연도
        event = s.scalars(select(DividendData).where(DividendData.ex_date.is_not(None))).one()
        assert event.dividend_amount == 4601 and event.pay_date == date(2026, 11, 27) and event.filed_date == date(2026, 10, 29)
        master = s.get(StockMaster, "005930")
        assert master.settle_month == 12
        assert master.corporate_actions == [{"type": "split", "rcept_no": "20240131800001", "rcept_dt": "2024-01-31",
                                             "date": "2018-05-04", "factor": 50.0}]
        gov = {g.year: g for g in s.scalars(select(GovernanceData)).all()}
        assert gov[2025].audit_risk == 1 and gov[2025].board_risk == 2 and gov[2025].shareholder_risk == 4
        assert gov[2025].details["filed_date"] == "2026-03-15"
        assert gov[2026].details["ownership"]["11014"]["largest_holder_shares"] == 400
    with mem_db.session() as s:
        fs = build_factsheet(s, "005930", AS_OF)
    v = fs.variables
    assert v["most_recent_quarter"] == "2026-06-30"  # 3분기보고서는 11/14 접수 → 11/13 기준으로는 아직 없다
    assert v["dividend_rate"] == 1005 and v["last_dividend_value"] == 4601 and v["last_split_factor"] == 50.0
    assert v["audit_risk"] == 1 and v["insider_holdings_pct"] is None  # 11/14 접수 지분 스냅샷도 미래
    calls_before = len(dart.calls)
    COLLECTORS["info"](ctx, ["005930"])
    assert "company" not in dart.calls[calls_before:]  # 두 번째 info 수집은 결산월을 다시 묻지 않는다
