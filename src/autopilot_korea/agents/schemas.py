"""에이전트 구조화 출력 스키마 (§9).

§9의 통합 스키마를 단계별 모델로 나눈다. 필드 이름은 §9를 따른다.
숫자 범위(점수 1~100, 확률 0~1)는 스키마 제약 대신 코드에서 보정한다(clamp) —
구조화 출력이 지원하지 않는 JSON Schema 키워드를 피하기 위해서다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from autopilot_korea.compliance import ComplianceCheck


class Evidence(BaseModel):
    title: str
    source: str
    date: str  # YYYY-MM-DD — 최근 7일 이내만 유효
    url: str


# ── Stage 1 스크리닝 ────────────────────────────────────────────────────

class ScreeningOutput(BaseModel):
    valuation_reasoning: str
    valuation: float  # 적정 주가 추정치(원)
    price_prediction_reasoning: str
    price_prediction_t: str  # 이번 라운드(다음 리밸런싱까지) 예측
    price_prediction_t1: str  # 다음 라운드 예측
    price_prediction_t2: str  # 다다음 라운드 예측
    stock_score: int  # 1~100
    score_reasoning: str  # 1줄 스코어 근거
    confidence: float  # 0~1 (CONFIDENCE 토글)
    edge: str  # 시장 기대와 내 견해의 차이 (SECOND_ORDER 토글)
    risk_flags: list[str]
    memory_note: str  # 다음 라운드에 기억할 한 줄 (MEMORY 토글, 없으면 빈 문자열)
    compliance_check: ComplianceCheck


# ── Stage 2 적대적 리서치 ────────────────────────────────────────────────

class AdversarialOutput(BaseModel):
    stance: Literal["bull", "bear"]
    strength: int  # 0~100, 이 입장 논거의 강도
    case_summary: str  # bull_case 또는 bear_case 요약
    key_points: list[str]
    evidence: list[Evidence]
    invalidation: str  # 이 논거가 틀렸다고 판단할 조건
    confidence: float
    edge: str
    risk_flags: list[str]
    memory_note: str
    compliance_check: ComplianceCheck


# ── Stage 3 시나리오 모델링 ─────────────────────────────────────────────

class Scenario(BaseModel):
    probability: float
    price_12m: float  # 시나리오별 12개월 주가(원)
    narrative: str


class ScenarioOutput(BaseModel):
    bull: Scenario
    base: Scenario
    bear: Scenario
    target_1m: float  # 확률 가중 목표주가
    target_3m: float
    target_6m: float
    target_12m: float
    expected_return: float  # 모델이 계산한 값 — 코드가 Σ(확률×수익률)로 다시 계산해 덮어쓴다
    optimism_review: str  # 자기 토론: 과도한 낙관 점검 결과
    risk: str
    confidence: float
    edge: str
    risk_flags: list[str]
    memory_note: str
    compliance_check: ComplianceCheck


# ── Stage 4 포트폴리오 구성 ─────────────────────────────────────────────

class TargetPosition(BaseModel):
    ticker: str
    weight: float  # 0~1
    thesis: str  # 반증 가능한 투자 논지
    falsification: str  # 논지가 틀렸다고 판단할 구체 조건
    edge: str
    risk: str


class OptimizerOutput(BaseModel):
    positions: list[TargetPosition]
    reasoning: str
    compliance_check: ComplianceCheck


# ── Stage 5 리밸런싱 검토 (마스터 오케스트레이터) ─────────────────────────

class SwapDecision(BaseModel):
    ticker_out: str  # 빈 문자열이면 신규 편입(Add)
    ticker_in: str  # 빈 문자열이면 편출만
    replace_decision: Literal["Add", "Cancel", "Replace"]
    reasoning: str


class RebalanceReview(BaseModel):
    decisions: list[SwapDecision]
    reasoning: str
    risk_flags: list[str]
    compliance_check: ComplianceCheck


# ── 보조 모듈 ───────────────────────────────────────────────────────────

class ThesisReviewOutput(BaseModel):
    """§7-2 종목 -25% thesis 재평가 (운영 승인 패널)."""

    thesis_intact: bool
    verdict: Literal["keep", "trim", "exit"]
    reasoning: str
    compliance_check: ComplianceCheck


class DartTriageOutput(BaseModel):
    importance: Literal["high", "medium", "low"]
    category: str  # 예: 유상증자, CB발행, 자기주식, 최대주주변경, 잠정실적, 소송 ...
    summary: str
    compliance_check: ComplianceCheck


class DartDeepOutput(BaseModel):
    impact: Literal["positive", "negative", "neutral"]
    magnitude: int  # 1~5
    thesis_impact: str
    summary: str
    rescore: bool
    risk_flags: list[str]
    compliance_check: ComplianceCheck


class SectorTilt(BaseModel):
    sector: str
    tilt: Literal["overweight", "neutral", "underweight"]
    reason: str


class MacroOutput(BaseModel):
    regime: str
    rates_outlook: str
    fx_outlook: str
    growth_outlook: str
    inflation_outlook: str
    sector_tilts: list[SectorTilt]
    risks: list[str]
    summary: str
    compliance_check: ComplianceCheck


class SentimentOutput(BaseModel):
    """감성 레이더 (분석 전용 — 주문 결정에 쓰지 않는다)."""

    sentiment: float  # -1(극단적 비관) ~ +1(극단적 낙관)
    intensity: float  # 0~1
    themes: list[str]
    overheating: bool
    summary: str
    compliance_check: ComplianceCheck


class SelfModifyProposal(BaseModel):
    """SELF_MODIFY 토글 (연구 모드 전용). 제안은 감사 로그에만 남는다."""

    proposed_change: str
    rationale: str
    compliance_check: ComplianceCheck


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
