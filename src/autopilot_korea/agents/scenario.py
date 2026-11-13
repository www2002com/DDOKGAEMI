"""Stage 3 — 시나리오 모델링 (§3.3, §3.4).

후보 종목마다 에이전트 1개가 확률 가중 불/베이스/베어 시나리오와 1·3·6·12개월 목표주가를 만든다.
과도한 낙관 방지: 프롬프트에서 자기 반박(optimism_review)을 요구하고, 코드에서
  - 확률 합을 1로 정규화하고,
  - 기대수익을 Σ(확률 × 시나리오 수익률)로 다시 계산해 덮어쓰며,
  - 불 시나리오 주가가 컨센서스 최고 목표가의 1.5배를 넘으면 플래그를 단다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from autopilot_korea.agents import memory, prompts
from autopilot_korea.agents.adversarial import ResearchSummary
from autopilot_korea.agents.llm import LLMRequest
from autopilot_korea.agents.runtime import PipelineContext, last_price, parallel_map
from autopilot_korea.agents.schemas import ScenarioOutput, clamp
from autopilot_korea.db.models import ScenarioModel

OPTIMISM_MULTIPLE = 1.5


@dataclass
class ScenarioResult:
    ticker: str
    probs: dict[str, float]
    targets: dict[str, float]
    expected_return: float
    confidence: float
    risk: str
    flags: list[str] = field(default_factory=list)
    output: ScenarioOutput | None = None


def normalize_probs(bull: float, base: float, bear: float) -> tuple[dict[str, float], bool]:
    raw = {"bull": max(bull, 0.0), "base": max(base, 0.0), "bear": max(bear, 0.0)}
    total = sum(raw.values())
    if total <= 0:
        return {"bull": 1 / 3, "base": 1 / 3, "bear": 1 / 3}, True
    adjusted = abs(total - 1.0) > 0.01
    return {k: v / total for k, v in raw.items()}, adjusted


def expected_return(probs: dict[str, float], prices_12m: dict[str, float], current: float) -> float:
    """§3.4 확률 가중 기대수익 = Σ(시나리오 확률 × 시나리오 수익률)."""
    return sum(probs[k] * (prices_12m[k] / current - 1.0) for k in probs)


def _research_block(summary: ResearchSummary | None) -> str:
    if summary is None:
        return ""
    return prompts.join_blocks(
        "## Stage 2 적대적 리서치 결과",
        f"불 강도(중앙값) {summary.bull_strength:.0f} / 베어 강도(중앙값) {summary.bear_strength:.0f} → {summary.flag}",
        f"불 케이스: {summary.bull_case}\n- " + "\n- ".join(summary.bull_points) if summary.bull_points else f"불 케이스: {summary.bull_case}",
        f"베어 케이스: {summary.bear_case}\n- " + "\n- ".join(summary.bear_points) if summary.bear_points else f"베어 케이스: {summary.bear_case}",
    )


def model_one(ctx: PipelineContext, ticker: str, research: ResearchSummary | None) -> ScenarioResult:
    persona = ctx.personas.get(ctx.settings.personas["stage_personas"]["stage3_scenario"])
    fs = ctx.factsheets[ticker]
    current = last_price(fs)
    if not current:
        raise ValueError(f"{ticker}: 현재가가 없어 시나리오 수익률을 계산할 수 없습니다")
    with ctx.db.session() as s:
        notes = memory.recent_notes(s, persona.id, ctx.settings.features.get("memory_notes_limit", 10)) if ctx.feature("MEMORY") else []
    user = prompts.join_blocks(
        prompts.factsheet_block(fs),
        f"## 현재가\n{current:,.0f}원",
        _research_block(research),
        prompts.macro_block(ctx.macro),
        prompts.memory_block(notes),
    )
    out: ScenarioOutput = ctx.llm.generate(LLMRequest(
        route="scenario", agent=f"S3:{persona.id}", stage="S3", ticker=ticker, run_id=ctx.run_id,
        system_blocks=prompts.system_blocks(ctx.settings, persona, prompts.TASK_SCENARIO),
        user=user, output_model=ScenarioOutput, persona_sha256=persona.sha256,
    ))

    probs, adjusted = normalize_probs(out.bull.probability, out.base.probability, out.bear.probability)
    prices = {"bull": out.bull.price_12m, "base": out.base.price_12m, "bear": out.bear.price_12m}
    er = expected_return(probs, prices, current)
    flags = list(out.risk_flags)
    if adjusted:
        flags.append("확률합_정규화")
    if abs(er - out.expected_return) > 0.02:
        flags.append("기대수익_재계산_차이")
    target_high = fs.variables.get("target_high_price")
    if target_high and out.bull.price_12m > OPTIMISM_MULTIPLE * float(target_high):
        flags.append("낙관_점검필요")
    targets = {"1m": out.target_1m, "3m": out.target_3m, "6m": out.target_6m, "12m": out.target_12m}
    confidence = clamp(out.confidence, 0, 1) if ctx.feature("CONFIDENCE") else 1.0

    with ctx.db.session() as s:
        s.add(ScenarioModel(
            run_id=ctx.run_id, date=ctx.as_of, ticker=ticker,
            bull_prob=probs["bull"], base_prob=probs["base"], bear_prob=probs["bear"],
            target_1m=out.target_1m, target_3m=out.target_3m, target_6m=out.target_6m, target_12m=out.target_12m,
            expected_return=er, risk=out.risk,
            payload={**out.model_dump(), "current_price": current, "expected_return_model": out.expected_return, "flags": flags},
        ))
        if ctx.feature("MEMORY"):
            memory.add_note(s, persona.id, out.memory_note, ticker)
    return ScenarioResult(ticker, probs, targets, er, confidence, out.risk, flags, out)


def run_scenarios(ctx: PipelineContext, tickers: list[str], research: dict[str, ResearchSummary]) -> dict[str, ScenarioResult]:
    results = parallel_map(lambda t: model_one(ctx, t, research.get(t)), tickers, ctx.rules.pipeline.scenario_agents)
    return {t: r for t, r, _ in results if r is not None}
