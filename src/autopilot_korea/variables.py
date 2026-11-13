"""97개 변수 레지스트리 (설계안 §4.2, 화이트페이퍼 Exhibit 2B의 한국 대응).

번호·원문 변수명·한국 대응·카테고리·소스·수집 주기를 한곳에 고정한다.
팩트시트(collectors/factsheet.py)는 이 레지스트리의 key로 값을 채운다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Variable:
    no: int
    key: str
    name_en: str
    name_kr: str
    category: str  # A~J
    source: str
    frequency: str


CATEGORIES: dict[str, str] = {
    "A": "시세·호가",
    "B": "배당",
    "C": "밸류에이션",
    "D": "모멘텀·기술지표",
    "E": "공매도·지분구조",
    "F": "기업 기본정보",
    "G": "수익성·현금흐름",
    "H": "재무안정성",
    "I": "애널리스트",
    "J": "지배구조 리스크",
}

# 카테고리별 수집 모듈 (§4.3)
CATEGORY_COLLECTOR: dict[str, str] = {
    "A": "price_collector",
    "B": "dividend_collector",
    "C": "valuation_collector",
    "D": "momentum_collector",
    "E": "short_interest_collector",
    "F": "info_collector",
    "G": "financials_collector",
    "H": "balance_sheet_collector",
    "I": "consensus_collector",
    "J": "governance_collector",
}

_P = "KRX 일별 시세 (pykrx, 대체: FinanceDataReader)"
_Q = "토스증권 호가 API"
_DART_FS = "DART fnlttSinglAcntAll"

VARIABLES: tuple[Variable, ...] = (
    # A. 시세·호가 (17)
    Variable(1, "prev_close", "Previous Close Price", "전일 종가", "A", _P, "일 1회"),
    Variable(2, "open", "Opening Price", "시가", "A", _P, "일 1회"),
    Variable(3, "day_low", "Lowest Price of the Day", "저가", "A", _P, "일 1회"),
    Variable(4, "day_high", "Highest Price of the Day", "고가", "A", _P, "일 1회"),
    Variable(5, "regular_market_prev_close", "Previous Close Price (Regular Market)", "전일 종가 (정규장)", "A", _P, "일 1회"),
    Variable(6, "regular_market_open", "Opening Price (Regular Market)", "시가 (정규장)", "A", _P, "일 1회"),
    Variable(7, "regular_market_day_low", "Lowest Price of the Day (Regular Market)", "저가 (정규장)", "A", _P, "일 1회"),
    Variable(8, "regular_market_day_high", "Highest Price of the Day (Regular Market)", "고가 (정규장)", "A", _P, "일 1회"),
    Variable(16, "volume", "Daily Volume of Shares Traded", "일 거래량", "A", _P, "일 1회"),
    Variable(17, "regular_market_volume", "Daily Volume of Shares Traded (Regular Market)", "일 거래량 (정규장)", "A", _P, "일 1회"),
    Variable(18, "average_volume", "Average Volume of Shares Traded", "평균 거래량 (20일)", "A", _P + " → 20일 이동평균", "일 1회"),
    Variable(19, "average_volume_10d", "Average Volume of Shares Traded (Last 10 Days)", "평균 거래량 (10일)", "A", _P + " → 10일 이동평균", "일 1회"),
    Variable(20, "average_daily_volume_10d", "Average Daily Volume of Shares Traded (Last 10 Days)", "일평균 거래량 (10일)", "A", _P + " → 10일 이동평균", "일 1회"),
    Variable(21, "bid", "Bid Price", "매수 호가", "A", _Q, "스코어링 시점"),
    Variable(22, "ask", "Ask Price", "매도 호가", "A", _Q, "스코어링 시점"),
    Variable(23, "bid_size", "Bid Size", "매수 호가 잔량", "A", _Q, "스코어링 시점"),
    Variable(24, "ask_size", "Ask Size", "매도 호가 잔량", "A", _Q, "스코어링 시점"),
    # B. 배당 (8)
    Variable(9, "dividend_rate", "Annual Dividend Rate", "연간 배당금", "B", "DART alotMatter", "결산 배당공시 시"),
    Variable(10, "dividend_yield", "Dividend Yield", "배당수익률", "B", "연간 배당금 ÷ 현재가", "주 1회"),
    Variable(11, "payout_ratio", "Payout Ratio", "배당성향", "B", "DART alotMatter (현금배당성향)", "결산 후"),
    Variable(12, "five_year_avg_dividend_yield", "Five-Year Average Dividend Yield", "5년 평균 배당수익률", "B", "DART alotMatter 5년치", "연 1회"),
    Variable(31, "trailing_annual_dividend_rate", "Annual Dividend Rate (TTM)", "연간 배당금 (TTM)", "B", "최근 4분기 배당금 합산", "분기"),
    Variable(32, "trailing_annual_dividend_yield", "Dividend Yield (TTM)", "배당수익률 (TTM)", "B", "TTM 배당금 ÷ 현재가", "주 1회"),
    Variable(64, "last_dividend_value", "Last Dividend Value", "직전 배당금", "B", "DART 배당 공시", "분기"),
    Variable(65, "last_dividend_date", "Last Dividend Payment Date", "직전 배당 지급일", "B", "DART 배당 공시", "분기"),
    # C. 밸류에이션 (10)
    Variable(14, "trailing_pe", "P/E Ratio (TTM)", "PER (TTM)", "C", "시총 ÷ TTM 지배주주순이익", "주 1회"),
    Variable(15, "forward_pe", "P/E Ratio (Forward)", "예상 PER", "C", "현재가 ÷ FnGuide 예상 EPS", "월 1회"),
    Variable(28, "price_to_sales_ttm", "P/S Ratio (TTM)", "PSR", "C", "시총 ÷ TTM 매출", "분기"),
    Variable(34, "enterprise_value", "Enterprise Value", "EV", "C", "시총 + 총차입 − 현금", "분기"),
    Variable(48, "book_value_per_share", "Book Value per Share", "BPS", "C", "지배주주 자본 ÷ 발행주식", "분기"),
    Variable(49, "price_to_book", "P/B Ratio", "PBR", "C", "현재가 ÷ BPS", "주 1회"),
    Variable(57, "peg_ratio", "PEG Ratio", "PEG", "C", "예상 PER ÷ 이익성장률(%)", "분기"),
    Variable(60, "enterprise_to_revenue", "EV/Revenue", "EV/매출", "C", "EV ÷ TTM 매출", "분기"),
    Variable(61, "enterprise_to_ebitda", "EV/EBITDA", "EV/EBITDA", "C", "EV ÷ TTM EBITDA", "분기"),
    Variable(92, "trailing_peg_ratio", "PEG Ratio (Trailing)", "PEG (Trailing)", "C", "PER(TTM) ÷ TTM 이익성장률(%)", "분기"),
    # D. 모멘텀·기술지표 (7)
    Variable(13, "beta", "Beta", "베타", "D", "일별 수익률 vs KOSPI200, 250일 회귀", "주 1회"),
    Variable(26, "fifty_two_week_low", "52-Week Low", "52주 저가", "D", "252거래일 최저가", "주 1회"),
    Variable(27, "fifty_two_week_high", "52-Week High", "52주 고가", "D", "252거래일 최고가", "주 1회"),
    Variable(29, "fifty_day_average", "50-Day Moving Average", "50일 이평", "D", "50일 SMA", "주 1회"),
    Variable(30, "two_hundred_day_average", "200-Day Moving Average", "200일 이평", "D", "200일 SMA", "주 1회"),
    Variable(62, "fifty_two_week_change", "52-Week Price Change %", "52주 등락률", "D", "(현재가 − 252일 전 종가) ÷ 252일 전 종가", "주 1회"),
    Variable(63, "benchmark_52_week_change", "S&P 500 52-Week Change %", "KOSPI200 52주 등락률", "D", "KOSPI200 지수 동일 계산", "주 1회"),
    # E. 공매도·지분구조 (11)
    Variable(36, "float_shares", "Float Shares", "유통주식수", "E", "발행주식수 − 최대주주 등 지분 (DART hyslrSttus)", "분기"),
    Variable(37, "shares_outstanding", "Shares Outstanding", "발행주식총수", "E", "KRX 상장주식수", "분기"),
    Variable(38, "shares_short", "Shares Sold Short", "공매도 거래량", "E", "KRX 공매도 통계", "일 1회"),
    Variable(39, "shares_short_prior_month", "Shares Sold Short (Previous Month)", "전월 공매도 거래량", "E", "KRX 공매도 통계 월 집계", "월 1회"),
    Variable(40, "short_interest_date_prior_month", "Short Interest Date (Prev Month)", "전월 공매도 기준일", "E", "KRX 공매도 통계", "월 1회"),
    Variable(41, "short_interest_date", "Most Recent Short Interest Date", "최근 공매도 기준일", "E", "KRX 공매도 통계", "일 1회"),
    Variable(42, "short_percent_of_outstanding", "Short Interest % of Outstanding", "공매도 비중 (발행주식 대비)", "E", "KRX 공매도 잔고 비율", "일 1회"),
    Variable(43, "insider_holdings_pct", "Insider Holdings %", "최대주주 지분율", "E", "DART hyslrSttus", "분기"),
    Variable(44, "institutional_holdings_pct", "Institutional Holdings %", "기관 보유 비중", "E", "DART majorstock (5% 대량보유 기관 합산, 근사)", "분기"),
    Variable(45, "short_ratio", "Short Ratio (Days to Cover)", "숏커버 소요일", "E", "공매도 잔고 ÷ 20일 평균 거래량", "주 1회"),
    Variable(46, "short_percent_of_float", "Short Interest % of Float", "공매도 비중 (유통주식 대비)", "E", "공매도 잔고 ÷ 유통주식수", "주 1회"),
    # F. 기업 기본정보 (9)
    Variable(25, "market_cap", "Market Capitalization", "시가총액", "F", "KRX 시가총액", "일 1회"),
    Variable(33, "currency", "Trading Currency", "거래 통화", "F", "KRW 고정", "정적"),
    Variable(47, "implied_shares_outstanding", "Implied Shares Outstanding", "시가총액 역산 발행주식", "F", "시총 ÷ 현재가", "일 1회"),
    Variable(50, "last_fiscal_year_end", "Last Fiscal Year End Date", "직전 결산일", "F", "DART company (acc_mt)", "연 1회"),
    Variable(51, "next_fiscal_year_end", "Next Fiscal Year End Date", "차기 결산일", "F", "DART company (acc_mt)", "연 1회"),
    Variable(52, "most_recent_quarter", "Most Recent Quarter Date", "최근 분기 기준일", "F", "DART 정기보고서 기준일", "분기"),
    Variable(58, "last_split_factor", "Last Stock Split Factor", "최근 액면분할 비율", "F", "DART 공시검색(주식분할) + 상장주식수 변화", "이벤트"),
    Variable(59, "last_split_date", "Last Stock Split Date", "최근 액면분할 일자", "F", "DART 공시검색(주식분할)", "이벤트"),
    Variable(91, "financial_currency", "Financial Currency", "재무 통화", "F", "KRW 고정", "정적"),
    # G. 수익성·현금흐름 (14)
    Variable(35, "profit_margins", "Profit Margins", "순이익률", "G", _DART_FS, "분기"),
    Variable(53, "earnings_quarterly_growth", "Earnings Growth (Quarterly)", "분기 이익 성장률 (YoY)", "G", _DART_FS, "분기"),
    Variable(54, "net_income_to_common", "Net Income to Common", "지배주주 순이익 (TTM)", "G", _DART_FS, "분기"),
    Variable(55, "trailing_eps", "EPS (TTM)", "EPS (TTM)", "G", "TTM 지배주주순이익 ÷ 발행주식", "분기"),
    Variable(56, "forward_eps", "EPS (Forward)", "예상 EPS", "G", "FnGuide 컨센서스", "월 1회"),
    Variable(82, "return_on_assets", "ROA", "ROA", "G", _DART_FS, "분기"),
    Variable(83, "return_on_equity", "ROE", "ROE", "G", _DART_FS, "분기"),
    Variable(84, "free_cashflow", "Free Cash Flow", "FCF", "G", "영업현금흐름 − 유형자산 취득 (TTM)", "분기"),
    Variable(85, "operating_cashflow", "Operating Cash Flow", "영업현금흐름 (TTM)", "G", _DART_FS, "분기"),
    Variable(86, "earnings_growth", "Earnings Growth Rate", "이익 성장률 (3년 CAGR)", "G", _DART_FS + " 사업보고서", "연 1회"),
    Variable(87, "revenue_growth", "Revenue Growth Rate", "매출 성장률 (3년 CAGR)", "G", _DART_FS + " 사업보고서", "연 1회"),
    Variable(88, "gross_margins", "Gross Profit Margins", "매출총이익률", "G", _DART_FS, "분기"),
    Variable(89, "ebitda_margins", "EBITDA Margins", "EBITDA 마진", "G", _DART_FS, "분기"),
    Variable(90, "operating_margins", "Operating Margins", "영업이익률", "G", _DART_FS, "분기"),
    # H. 재무안정성 (9)
    Variable(73, "total_cash", "Total Cash", "현금성자산", "H", _DART_FS, "분기"),
    Variable(74, "total_cash_per_share", "Cash per Share", "주당 현금", "H", "현금성자산 ÷ 발행주식", "분기"),
    Variable(75, "ebitda", "EBITDA", "EBITDA (TTM)", "H", "영업이익 + 감가상각비 (현금흐름표)", "분기"),
    Variable(76, "total_debt", "Total Debt", "총차입", "H", "단기차입 + 유동성장기부채 + 사채 + 장기차입 (리스부채 포함)", "분기"),
    Variable(77, "quick_ratio", "Quick Ratio", "당좌비율", "H", "(유동자산 − 재고자산) ÷ 유동부채", "분기"),
    Variable(78, "current_ratio", "Current Ratio", "유동비율", "H", "유동자산 ÷ 유동부채", "분기"),
    Variable(79, "total_revenue", "Total Revenue", "매출액 (TTM)", "H", _DART_FS, "분기"),
    Variable(80, "debt_to_equity", "Debt-to-Equity Ratio", "부채비율", "H", "부채총계 ÷ 자본총계", "분기"),
    Variable(81, "revenue_per_share", "Revenue per Share", "주당 매출", "H", "TTM 매출 ÷ 발행주식", "분기"),
    # I. 애널리스트 (7)
    Variable(66, "target_high_price", "Highest Target Price", "최고 목표주가", "I", "FnGuide 컨센서스", "월 1회"),
    Variable(67, "target_low_price", "Lowest Target Price", "최저 목표주가", "I", "FnGuide 컨센서스", "월 1회"),
    Variable(68, "target_mean_price", "Mean Target Price", "평균 목표주가", "I", "FnGuide 컨센서스", "월 1회"),
    Variable(69, "target_median_price", "Median Target Price", "중앙 목표주가", "I", "FnGuide 증권사별 목표주가 중앙값", "월 1회"),
    Variable(70, "recommendation_mean", "Recommendation Mean", "컨센서스 평균", "I", "FnGuide 투자의견 (1=강력매수 ~ 5=매도)", "월 1회"),
    Variable(71, "recommendation_key", "Recommendation Key", "컨센서스 등급", "I", "FnGuide 투자의견 → 등급", "월 1회"),
    Variable(72, "number_of_analyst_opinions", "Number of Analyst Opinions", "컨센서스 의견 수", "I", "FnGuide 추정기관수", "월 1회"),
    # J. 지배구조 리스크 (5) — 원본(Yahoo governance)의 직접 대응이 없어 한국형 자체 산출 (1=저위험 ~ 5=고위험)
    Variable(93, "audit_risk", "Audit Risk Level", "감사 리스크", "J", "DART accnutAdtorNmNdAdtOpinion", "연 1회"),
    Variable(94, "board_risk", "Board Risk Level", "이사회 리스크", "J", "DART outcmpnyDrctrNdChangeSttus", "연 1회"),
    Variable(95, "compensation_risk", "Compensation Risk Level", "보상 리스크", "J", "DART 이사·감사 보수 공시", "연 1회"),
    Variable(96, "shareholder_rights_risk", "Shareholder Rights Risk", "주주권 리스크", "J", "DART stockTotqySttus·hyslrSttus", "연 1회"),
    Variable(97, "overall_risk", "Overall Risk Level", "종합 리스크", "J", "93~96 가중 평균", "연 1회"),
)

BY_NO: dict[int, Variable] = {v.no: v for v in VARIABLES}
BY_KEY: dict[str, Variable] = {v.key: v for v in VARIABLES}

# 화이트페이퍼와 동일하게 번호 순으로 정렬한 키 목록 — 팩트시트 직렬화 순서
ORDERED_KEYS: tuple[str, ...] = tuple(BY_NO[n].key for n in sorted(BY_NO))


def by_category(category: str) -> list[Variable]:
    return sorted((v for v in VARIABLES if v.category == category), key=lambda v: v.no)
