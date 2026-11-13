"""Stage 1 — 스크리닝 (§3.3).

입력: 유니버스 전 종목의 97개 변수 팩트시트 + 최근 7일 뉴스
실행: 종목당 1회 LLM 호출 (route: screening, 페르소나: stage1_screening)
출력: 종목당 1~100점 + 1줄 근거 → 상위 N(50) 종목. 동점이면 시가총액이 큰 종목이 앞선다.
저장: screening_results (stage=S1)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from autopilot_korea.agents import memory, prompts
from autopilot_korea.agents.llm import LLMRequest
from autopilot_korea.agents.runtime import PipelineContext, last_price, parallel_map
from autopilot_korea.agents.schemas import ScreeningOutput, clamp
from autopilot_korea.db.models import ScreeningResult


@dataclass
class ScreenedStock:
    ticker: str
    score: float
    reasoning: str
    confidence: float
    market_cap: float
    output: ScreeningOutput


def _user_message(ctx: PipelineContext, ticker: str) -> str:
    fs = ctx.factsheets[ticker]
    persona_id = ctx.settings.personas["stage_personas"]["stage1_screening"]
    with ctx.db.session() as s:
        notes = memory.recent_notes(s, persona_id, ctx.settings.features.get("memory_notes_limit", 10)) if ctx.feature("MEMORY") else []
        prev = memory.last_screening(s, ticker, ctx.run_id) if ctx.feature("LAST_REASONING") else None
    if prev and prev.get("price_then") and (now := last_price(fs)):
        prev["realized_return_since"] = round(now / float(prev["price_then"]) - 1, 4)
    return prompts.join_blocks(
        prompts.factsheet_block(fs),
        prompts.news_block(ctx.news.get(ticker, []), ctx.rules.pipeline.news_window_days),
        prompts.macro_block(ctx.macro),
        prompts.sentiment_block(ctx.sentiment.get(ticker)) if ctx.feature("SOCIAL") else "",
        prompts.memory_block(notes),
        prompts.last_reasoning_block(prev),
    )


def score_one(ctx: PipelineContext, ticker: str, *, persona_id: str | None = None, task: str | None = None,
              stage: str = "S1") -> ScreenedStock:
    """persona_id·task는 백테스트 통제군과 강건성 스윕(§14)이 바꿔 쓴다. 운영 경로는 기본값을 쓴다."""
    persona = ctx.personas.get(persona_id or ctx.settings.personas["stage_personas"]["stage1_screening"])
    out: ScreeningOutput = ctx.llm.generate(LLMRequest(
        route="screening", agent=f"{stage}:{persona.id}", stage=stage, ticker=ticker, run_id=ctx.run_id,
        system_blocks=prompts.system_blocks(ctx.settings, persona, task or prompts.TASK_SCREENING),
        user=_user_message(ctx, ticker), output_model=ScreeningOutput, persona_sha256=persona.sha256,
    ))
    fs = ctx.factsheets[ticker]
    score = clamp(float(out.stock_score), 1, 100)
    confidence = clamp(out.confidence, 0, 1) if ctx.feature("CONFIDENCE") else 1.0
    with ctx.db.session() as s:
        s.add(ScreeningResult(
            run_id=ctx.run_id, date=ctx.as_of, ticker=ticker, stage=stage, score=score,
            score_reasoning=out.score_reasoning,
            payload={**out.model_dump(), "last_close": last_price(fs), "persona": persona.id},
        ))
        if ctx.feature("MEMORY") and stage == "S1":
            memory.add_note(s, persona.id, out.memory_note, ticker)
    return ScreenedStock(ticker, score, out.score_reasoning, confidence,
                         float(fs.variables.get("market_cap") or 0), out)


def rank(stocks: list[ScreenedStock]) -> list[ScreenedStock]:
    """점수 내림차순, 동점이면 시가총액 내림차순 (§5 동점 처리)."""
    return sorted(stocks, key=lambda s: (-s.score, -s.market_cap))


def run_screening(ctx: PipelineContext, tickers: list[str], **variant: Any) -> tuple[list[ScreenedStock], dict[str, Any]]:
    """전 종목을 스코어링하고 순위 목록 전체를 돌려준다. 상위 N 선택은 오케스트레이터가 한다."""
    results = parallel_map(lambda t: score_one(ctx, t, **variant), tickers, ctx.rules.pipeline.max_concurrency)
    scored = [r for _, r, err in results if r is not None]
    failed = {t: str(err) for t, _, err in results if err is not None}
    return rank(scored), {"scored": len(scored), "failed": failed}
