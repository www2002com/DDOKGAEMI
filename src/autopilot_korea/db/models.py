"""SQLAlchemy 모델.

§4.3 스키마의 10개 테이블을 그대로 두고, 운영에 필요한 상태 테이블(§8 thesis 저널·메모리·NAV,
주문, 실행 기록, 팩트시트 스냅샷, 수집 상태)을 더한다. SQLite(P1)와 PostgreSQL(운영) 모두에서 동작한다.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class TimestampMixin:
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ── 유니버스 ─────────────────────────────────────────────────────────────

class StockMaster(TimestampMixin, Base):
    __tablename__ = "stock_master"
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    market: Mapped[str] = mapped_column(String(10))  # KOSPI | KOSDAQ | ETF
    corp_code: Mapped[str | None] = mapped_column(String(8))  # DART 고유번호
    sector: Mapped[str] = mapped_column(String(40), default="미분류")
    industry: Mapped[str | None] = mapped_column(String(200))
    settle_month: Mapped[int | None] = mapped_column(Integer)  # 결산월
    in_kospi200: Mapped[bool] = mapped_column(Boolean, default=False)
    in_kosdaq150: Mapped[bool] = mapped_column(Boolean, default=False)
    is_cash_etf: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_flags: Mapped[list[Any]] = mapped_column(JSON, default=list)  # 관리종목·투자경고 등
    # 액면분할 등 기업 이벤트 [{type, rcept_no, rcept_dt, date, factor}] — #58·#59 (rcept_dt로 룩어헤드 차단)
    corporate_actions: Mapped[list[Any]] = mapped_column(JSON, default=list)


# ── §4.3 데이터 테이블 ──────────────────────────────────────────────────

class PriceData(TimestampMixin, Base):
    __tablename__ = "price_data"
    __table_args__ = (UniqueConstraint("ticker", "date"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    trading_value: Mapped[float | None] = mapped_column(Float)  # 거래대금
    market_cap: Mapped[float | None] = mapped_column(Float)
    shares: Mapped[float | None] = mapped_column(Float)  # 상장주식수
    foreign_ratio: Mapped[float | None] = mapped_column(Float)  # 외국인 보유 비율(소수, 0.46 = 46%)


class FinancialData(TimestampMixin, Base):
    """정기보고서 1건의 정규화된 재무 항목. 손익·현금흐름은 연초 누적(YTD) 값이다."""

    __tablename__ = "financial_data"
    __table_args__ = (UniqueConstraint("ticker", "quarter", "fs_div"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    quarter: Mapped[str] = mapped_column(String(7))  # 예: 2026Q3 (사업보고서는 Q4)
    period_end: Mapped[date] = mapped_column(Date)
    report_code: Mapped[str] = mapped_column(String(5))  # 11013/11012/11014/11011
    fs_div: Mapped[str] = mapped_column(String(3), default="CFS")  # CFS 연결 | OFS 별도
    filed_date: Mapped[date | None] = mapped_column(Date)  # 공시 접수일 — 룩어헤드 차단 기준(§14)
    revenue: Mapped[float | None] = mapped_column(Float)
    net_income: Mapped[float | None] = mapped_column(Float)
    ebitda: Mapped[float | None] = mapped_column(Float)
    total_debt: Mapped[float | None] = mapped_column(Float)
    items: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # 전체 정규화 계정


class DividendData(TimestampMixin, Base):
    __tablename__ = "dividend_data"
    __table_args__ = (UniqueConstraint("ticker", "fiscal_year", "ex_date"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    fiscal_year: Mapped[int] = mapped_column(Integer)
    ex_date: Mapped[date | None] = mapped_column(Date)  # 배당 기준일
    pay_date: Mapped[date | None] = mapped_column(Date)
    dividend_amount: Mapped[float | None] = mapped_column(Float)  # 주당 현금배당금(원)
    reported_yield: Mapped[float | None] = mapped_column(Float)  # DART 현금배당수익률(%)
    payout_ratio: Mapped[float | None] = mapped_column(Float)  # DART 현금배당성향(%)
    filed_date: Mapped[date | None] = mapped_column(Date)  # 공시 접수일 — 룩어헤드 차단 기준(§14)


class ShortInterest(TimestampMixin, Base):
    __tablename__ = "short_interest"
    __table_args__ = (UniqueConstraint("ticker", "date"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    short_volume: Mapped[float | None] = mapped_column(Float)  # 공매도 거래량
    short_balance: Mapped[float | None] = mapped_column(Float)  # 공매도 잔고 수량
    short_ratio: Mapped[float | None] = mapped_column(Float)  # 잔고 비중(상장주식 대비, %)


class ConsensusData(TimestampMixin, Base):
    __tablename__ = "consensus_data"
    __table_args__ = (UniqueConstraint("ticker", "date"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    date: Mapped[date] = mapped_column(Date)
    target_high: Mapped[float | None] = mapped_column(Float)
    target_low: Mapped[float | None] = mapped_column(Float)
    target_mean: Mapped[float | None] = mapped_column(Float)
    target_median: Mapped[float | None] = mapped_column(Float)
    rec_mean: Mapped[float | None] = mapped_column(Float)
    rec_key: Mapped[str | None] = mapped_column(String(20))
    n_analysts: Mapped[int | None] = mapped_column(Integer)
    forward_eps: Mapped[float | None] = mapped_column(Float)
    covered: Mapped[bool] = mapped_column(Boolean, default=True)  # 무료 FnGuide 커버리지 플래그


class GovernanceData(TimestampMixin, Base):
    __tablename__ = "governance_data"
    __table_args__ = (UniqueConstraint("ticker", "year"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    year: Mapped[int] = mapped_column(Integer)
    audit_risk: Mapped[int | None] = mapped_column(Integer)
    board_risk: Mapped[int | None] = mapped_column(Integer)
    comp_risk: Mapped[int | None] = mapped_column(Integer)
    shareholder_risk: Mapped[int | None] = mapped_column(Integer)
    overall_risk: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ScreeningResult(TimestampMixin, Base):
    """단계별 종목 결과. stage=S1(스코어), S2(불/베어 집계: score=불 강도−베어 강도)."""

    __tablename__ = "screening_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    stage: Mapped[str] = mapped_column(String(4))
    score: Mapped[float | None] = mapped_column(Float)
    score_reasoning: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ScenarioModel(TimestampMixin, Base):
    __tablename__ = "scenario_models"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    date: Mapped[date] = mapped_column(Date)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    bull_prob: Mapped[float] = mapped_column(Float)
    base_prob: Mapped[float] = mapped_column(Float)
    bear_prob: Mapped[float] = mapped_column(Float)
    target_1m: Mapped[float | None] = mapped_column(Float)
    target_3m: Mapped[float | None] = mapped_column(Float)
    target_6m: Mapped[float | None] = mapped_column(Float)
    target_12m: Mapped[float | None] = mapped_column(Float)
    expected_return: Mapped[float] = mapped_column(Float)
    risk: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PortfolioPosition(TimestampMixin, Base):
    """Stage 4가 만든 목표 포트폴리오 (실행 회차별 스냅샷)."""

    __tablename__ = "portfolio_positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    date: Mapped[date] = mapped_column(Date)
    ticker: Mapped[str] = mapped_column(String(12))
    weight: Mapped[float] = mapped_column(Float)
    thesis: Mapped[str | None] = mapped_column(Text)
    edge: Mapped[str | None] = mapped_column(Text)
    risk: Mapped[str | None] = mapped_column(Text)
    sector: Mapped[str | None] = mapped_column(String(40))
    expected_return: Mapped[float | None] = mapped_column(Float)


class AuditLog(TimestampMixin, Base):
    """렌더링된 프롬프트 전문과 응답 (§8-3). cache_key로 리플레이 캐시를 겸한다."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True)
    agent: Mapped[str] = mapped_column(String(80))  # 예: S2:bull:07_value
    stage: Mapped[str | None] = mapped_column(String(20))
    ticker: Mapped[str | None] = mapped_column(String(12))
    model: Mapped[str | None] = mapped_column(String(60))
    persona_sha256: Mapped[str | None] = mapped_column(String(64))
    cache_key: Mapped[str | None] = mapped_column(String(64), index=True)
    prompt_rendered: Mapped[str] = mapped_column(Text)
    response: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok|blocked|error|refusal|replayed
    compliance_check: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# ── 운영 상태 (§8, §15) ─────────────────────────────────────────────────

class FactorSnapshot(TimestampMixin, Base):
    """종목별 97개 변수 팩트시트와 변수별 신선도 플래그."""

    __tablename__ = "factor_snapshot"
    __table_args__ = (UniqueConstraint("ticker", "as_of"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    variables: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    freshness: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class NewsItem(TimestampMixin, Base):
    __tablename__ = "news_items"
    __table_args__ = (UniqueConstraint("ticker", "url"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    title: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(12), default="news")  # news | disclosure


class Disclosure(TimestampMixin, Base):
    """DART 공시 (주요사항보고 모니터링 + 정기보고서 감지)."""

    __tablename__ = "disclosures"
    rcept_no: Mapped[str] = mapped_column(String(20), primary_key=True)
    corp_code: Mapped[str] = mapped_column(String(8), index=True)
    ticker: Mapped[str | None] = mapped_column(String(12), index=True)
    report_nm: Mapped[str] = mapped_column(String(300))
    rcept_dt: Mapped[date] = mapped_column(Date, index=True)
    pblntf_ty: Mapped[str | None] = mapped_column(String(2))
    importance: Mapped[str | None] = mapped_column(String(10))  # high|medium|low
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MacroData(TimestampMixin, Base):
    __tablename__ = "macro_data"
    __table_args__ = (UniqueConstraint("series", "period"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series: Mapped[str] = mapped_column(String(80))
    period: Mapped[str] = mapped_column(String(10))
    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(40))


class MacroOutlook(TimestampMixin, Base):
    __tablename__ = "macro_outlook"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AdversarialOpinion(TimestampMixin, Base):
    """Stage 2 개별 에이전트 의견 (불 15 + 베어 15)."""

    __tablename__ = "adversarial_opinions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    persona: Mapped[str] = mapped_column(String(40))
    stance: Mapped[str] = mapped_column(String(4))  # bull | bear
    strength: Mapped[float] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PipelineRun(TimestampMixin, Base):
    __tablename__ = "pipeline_runs"
    run_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|completed|failed|skipped
    trigger: Mapped[str] = mapped_column(String(40))  # schedule|manual|event:<rcept_no>
    mode: Mapped[str] = mapped_column(String(10))
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True)
    client_order_id: Mapped[str] = mapped_column(String(36), unique=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    side: Mapped[str] = mapped_column(String(4))  # BUY | SELL
    order_type: Mapped[str] = mapped_column(String(6))  # MARKET | LIMIT
    quantity: Mapped[int] = mapped_column(Integer)
    price_limit: Mapped[float | None] = mapped_column(Float)
    # proposed → approved → submitted → (partially_filled) → filled | canceled | rejected | failed | blocked
    status: Mapped[str] = mapped_column(String(20), default="proposed", index=True)
    reasoning: Mapped[str | None] = mapped_column(Text)
    risk_flags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)  # Stage 5 검토가 Cancel → 자동 승인 제외
    not_before: Mapped[date | None] = mapped_column(Date)  # 분할 주문의 제출 가능일 (§7-4)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    avg_fill_price: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_by: Mapped[str | None] = mapped_column(String(60))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class NavHistory(TimestampMixin, Base):
    __tablename__ = "nav_history"
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    nav: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    invested: Mapped[float] = mapped_column(Float)
    benchmark_close: Mapped[float | None] = mapped_column(Float)
    positions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ThesisJournal(TimestampMixin, Base):
    """보유 종목 투자 논지 — 반증 가능한 서면 thesis (§3.2 인간의 역할, §8-1)."""

    __tablename__ = "thesis_journal"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | closed
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    entry_price: Mapped[float | None] = mapped_column(Float)
    thesis: Mapped[str] = mapped_column(Text)
    falsification: Mapped[str] = mapped_column(Text)  # 이 논지가 틀렸다고 판단할 조건
    target_12m: Mapped[float | None] = mapped_column(Float)
    risk: Mapped[str | None] = mapped_column(Text)
    reviews: Mapped[list[Any]] = mapped_column(JSON, default=list)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(Text)


class AgentMemory(TimestampMixin, Base):
    __tablename__ = "agent_memory"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ticker: Mapped[str | None] = mapped_column(String(12))
    note: Mapped[str] = mapped_column(Text)


class CollectionStatus(TimestampMixin, Base):
    """수집 작업별 신선도·실패 상태 (§4.3 신선도 플래그, §15 회로 차단기)."""

    __tablename__ = "collection_status"
    __table_args__ = (UniqueConstraint("job", "ticker"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(40))
    ticker: Mapped[str] = mapped_column(String(12), default="*")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10), default="never")  # ok|stale|failed|never
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
