"""테스트 대역: 결정론적 가짜 LLM과 팩트시트. 네트워크·API 키 없이 5단계 파이프라인을 끝까지 돌린다."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from autopilot_korea.agents import schemas as S
from autopilot_korea.compliance import ComplianceCheck
from autopilot_korea.variables import ORDERED_KEYS

CC = ComplianceCheck(loss_guarantee_mention=False, individual_recommendation=False, disclaimer_included=True)
SECTORS = ["정보기술", "산업재", "금융", "헬스케어", "소재", "경기소비재"]


@dataclass
class FakeFactsheet:
    ticker: str
    name: str
    sector: str
    as_of: date
    variables: dict[str, Any]
    freshness: dict[str, str] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_prompt_json(self) -> str:
        return json.dumps({k: self.variables.get(k) for k in ("market_cap", "trailing_pe", "beta")}, ensure_ascii=False)


def fake_factsheet(ticker: str, as_of: date, price: float = 10_000.0) -> FakeFactsheet:
    n = int(ticker)
    variables = {k: None for k in ORDERED_KEYS}
    variables.update({"market_cap": 1e12 + n * 1e10, "beta": 1.0, "trailing_pe": 10.0, "target_high_price": price * 2})
    return FakeFactsheet(ticker, f"종목{n}", SECTORS[n % len(SECTORS)], as_of, variables,
                         extras={"last_close": price, "avg_trading_value_20d": 5e10})


def num(ticker: str | None) -> int:
    return int(ticker) if ticker and ticker.isdigit() else 0


class FakeLLM:
    """요청의 출력 모델과 종목 번호로 결정론적 응답을 만든다."""

    def __init__(self, *, violate_optimizer_first: bool = False, cancel_add: str | None = None,
                 thesis_verdict: str = "exit", forbidden_once: bool = False):
        self.violate_optimizer_first = violate_optimizer_first
        self.cancel_add = cancel_add
        self.thesis_verdict = thesis_verdict
        self.forbidden_once = forbidden_once
        self.calls: list[Any] = []
        self.optimizer_calls = 0
        self._lock = threading.Lock()

    def generate(self, req):
        with self._lock:
            self.calls.append(req)
        m, n = req.output_model, num(req.ticker)
        if m is S.ScreeningOutput:
            score = 30 + (n * 7) % 61
            return S.ScreeningOutput(valuation_reasoning="v", valuation=12000, price_prediction_reasoning="p",
                                     price_prediction_t="보합", price_prediction_t1="상승", price_prediction_t2="상승",
                                     stock_score=score, score_reasoning=f"점수 {score}", confidence=0.7, edge="e",
                                     risk_flags=[], memory_note=f"메모 {n}", compliance_check=CC)
        if m is S.AdversarialOutput:
            stance = "bull" if ":bull:" in req.agent else "bear"
            strength = (50 + (n % 3) * 10) if stance == "bull" else (55 if n % 5 == 0 else 45)
            return S.AdversarialOutput(stance=stance, strength=strength, case_summary=f"{stance} 요약", key_points=["포인트"],
                                       evidence=[S.Evidence(title="t", source="s", date="2000-01-01", url="u")],
                                       invalidation="조건", confidence=0.6, edge="e", risk_flags=[], memory_note="",
                                       compliance_check=CC)
        if m is S.ScenarioOutput:
            cur = float(re.search(r"## 현재가\n([\d,]+)원", req.user).group(1).replace(",", ""))
            probs = (0.1, 0.3, 0.6) if n % 7 == 0 else (0.3, 0.5, 0.2)
            return S.ScenarioOutput(
                bull=S.Scenario(probability=probs[0], price_12m=cur * 1.4, narrative="불"),
                base=S.Scenario(probability=probs[1], price_12m=cur * 1.1, narrative="베이스"),
                bear=S.Scenario(probability=probs[2], price_12m=cur * 0.8, narrative="베어"),
                target_1m=cur, target_3m=cur, target_6m=cur, target_12m=cur * 1.1, expected_return=0.13,
                optimism_review="점검", risk="리스크", confidence=0.6, edge="e", risk_flags=[], memory_note="",
                compliance_check=CC)
        if m is S.OptimizerOutput:
            with self._lock:
                self.optimizer_calls += 1
                first = self.optimizer_calls == 1
            rows = json.loads(re.search(r"```json\n(.*?)\n```", req.user, re.S).group(1))
            eligible = [r for r in rows if r["sector"] != "현금성" and (r["expected_return"] or 0) > 0]
            eligible.sort(key=lambda r: (-(r["s1_score"] or 0), -(r["market_cap_억원"] or 0)))
            chosen = eligible[:14]
            cash = next(r["ticker"] for r in rows if r["sector"] == "현금성")
            w = round(0.91 / len(chosen), 6) if chosen else 0
            positions = [S.TargetPosition(ticker=r["ticker"], weight=w, thesis=f"{r['ticker']} 논지",
                                          falsification="기대수익 음전환", edge="e", risk="r") for r in chosen]
            positions.append(S.TargetPosition(ticker=cash, weight=round(1 - w * len(chosen), 6), thesis="현금성",
                                              falsification="-", edge="", risk=""))
            if self.violate_optimizer_first and first and positions:
                positions[0].weight = 0.25
            return S.OptimizerOutput(positions=positions, reasoning="최적화", compliance_check=CC)
        if m is S.RebalanceReview:
            decisions = json.loads(re.search(r"## 규칙 엔진 결정 목록\n\n(.*?)\n\n##", req.user, re.S).group(1))
            out = []
            for d in decisions:
                rd = "Cancel" if self.cancel_add and d["ticker_in"] == self.cancel_add else d["replace_decision"]
                out.append(S.SwapDecision(ticker_out=d["ticker_out"], ticker_in=d["ticker_in"], replace_decision=rd,
                                          reasoning="근거"))
            return S.RebalanceReview(decisions=out, reasoning="검토 완료", risk_flags=[], compliance_check=CC)
        if m is S.ThesisReviewOutput:
            return S.ThesisReviewOutput(thesis_intact=False, verdict=self.thesis_verdict, reasoning="전제 훼손", compliance_check=CC)
        if m is S.DartTriageOutput:
            return S.DartTriageOutput(importance="high", category="유상증자", summary="희석", compliance_check=CC)
        if m is S.DartDeepOutput:
            return S.DartDeepOutput(impact="negative", magnitude=4, thesis_impact="훼손", summary="s", rescore=True,
                                    risk_flags=[], compliance_check=CC)
        if m is S.MacroOutput:
            return S.MacroOutput(regime="완만한 회복", rates_outlook="동결", fx_outlook="안정", growth_outlook="회복",
                                 inflation_outlook="둔화", sector_tilts=[S.SectorTilt(sector="정보기술", tilt="overweight", reason="r")],
                                 risks=["r"], summary="요약", compliance_check=CC)
        if m is S.SentimentOutput:
            return S.SentimentOutput(sentiment=0.4, intensity=0.5, themes=["AI"], overheating=False, summary="s", compliance_check=CC)
        raise AssertionError(f"FakeLLM: 처리하지 않는 출력 모델 {m}")
