"""보조 에이전트: thesis 재평가(§7-2), 매크로 전망(월 1회), 감성 레이더(분석 전용), SELF_MODIFY(연구 전용)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from typing import Any

from sqlalchemy import select

from autopilot_korea.agents import memory, prompts
from autopilot_korea.agents.llm import LLMError, LLMRequest
from autopilot_korea.agents.runtime import PipelineContext, parallel_map
from autopilot_korea.agents.schemas import MacroOutput, SelfModifyProposal, SentimentOutput, ThesisReviewOutput, clamp
from autopilot_korea.db.models import MacroData, MacroOutlook


# ── thesis 재평가 (운영 승인 패널) ───────────────────────────────────────

def review_thesis(ctx: PipelineContext, ticker: str, pnl_pct: float) -> dict[str, Any]:
    """운영 승인 패널 전원이 판정하고 다수결로 결론을 낸다. exit가 과반이면 편출."""
    with ctx.db.session() as s:
        thesis = memory.open_theses(s).get(ticker)
        thesis_info = {"thesis": thesis.thesis, "falsification": thesis.falsification, "entry_price": thesis.entry_price,
                       "opened_at": thesis.opened_at.isoformat()} if thesis else {"thesis": "(기록 없음)"}
    user = prompts.join_blocks(
        prompts.factsheet_block(ctx.factsheets[ticker]),
        prompts.news_block(ctx.news.get(ticker, []), ctx.rules.pipeline.news_window_days),
        "## 기록된 투자 논지", json.dumps(thesis_info, ensure_ascii=False),
        f"## 매입가 대비 손익\n{pnl_pct:.1%}",
    )

    def one(pid: str) -> ThesisReviewOutput:
        persona = ctx.personas.get(pid)
        return ctx.llm.generate(LLMRequest(
            route="thesis_review", agent=f"thesis:{pid}", stage="R7", ticker=ticker, run_id=ctx.run_id,
            system_blocks=prompts.system_blocks(ctx.settings, persona, prompts.TASK_THESIS_REVIEW),
            user=user, output_model=ThesisReviewOutput, persona_sha256=persona.sha256,
        ))

    panel = ctx.settings.personas["panels"]["operational"]
    outs = [o for _, o, _ in parallel_map(one, panel, ctx.rules.pipeline.max_concurrency) if o is not None]
    votes = Counter(o.verdict for o in outs)
    verdict = "exit" if votes["exit"] * 2 > len(outs) else "trim" if (votes["exit"] + votes["trim"]) * 2 > len(outs) else "keep"
    result = {"date": ctx.as_of.isoformat(), "pnl_pct": pnl_pct, "votes": dict(votes), "verdict": verdict,
              "reasons": [o.reasoning for o in outs][:7]}
    with ctx.db.session() as s:
        memory.add_review(s, ticker, result)
    return result


# ── 매크로 (월 1회, DeepSeek 화이트페이퍼 2D 매크로 프롬프트의 한국형) ─────────

def run_macro(ctx: PipelineContext) -> dict[str, Any]:
    persona = ctx.personas.get(ctx.settings.personas["stage_personas"]["macro"])
    with ctx.db.session() as s:
        rows = s.scalars(select(MacroData).order_by(MacroData.series, MacroData.period.desc())).all()
    latest: dict[str, dict] = {}
    for r in rows:
        latest.setdefault(r.series, {"period": r.period, "value": r.value, "unit": r.unit})
    out: MacroOutput = ctx.llm.generate(LLMRequest(
        route="macro", agent=f"macro:{persona.id}", stage="MACRO", run_id=ctx.run_id,
        system_blocks=prompts.system_blocks(ctx.settings, persona, prompts.TASK_MACRO),
        user=prompts.join_blocks(f"## 기준일 {ctx.as_of.isoformat()}", "## 한국은행 주요 통계 (ECOS)",
                                 json.dumps(latest, ensure_ascii=False) if latest else "(수집된 통계 없음)"),
        output_model=MacroOutput, persona_sha256=persona.sha256,
    ))
    payload = out.model_dump()
    with ctx.db.session() as s:
        s.add(MacroOutlook(as_of=ctx.as_of, payload=payload))
    return payload


def latest_macro(ctx: PipelineContext) -> dict[str, Any] | None:
    with ctx.db.session() as s:
        row = s.scalars(select(MacroOutlook).where(MacroOutlook.as_of <= ctx.as_of).order_by(MacroOutlook.as_of.desc())).first()
        return row.payload if row else None


# ── 감성 레이더 (페르소나 16·17·20, 분석 전용) ────────────────────────────

def run_sentiment(ctx: PipelineContext, tickers: list[str], board_titles: dict[str, list[str]]) -> dict[str, dict]:
    """세 감성 페르소나의 평균. 결과는 SOCIAL 토글이 켜졌을 때만 다른 에이전트에 참고로 전달된다."""
    radar = ctx.settings.personas["panels"]["sentiment_radar"]

    def one(job: tuple[str, str]) -> SentimentOutput:
        ticker, pid = job
        persona = ctx.personas.get(pid)
        user = prompts.join_blocks(
            f"## 종목 {ticker}",
            prompts.news_block(ctx.news.get(ticker, []), ctx.rules.pipeline.news_window_days),
            "## 종목토론방 최근 글 제목\n" + "\n".join(f"- {t}" for t in board_titles.get(ticker, [])[:40]),
        )
        return ctx.llm.generate(LLMRequest(
            route="sentiment", agent=f"radar:{pid}", stage="RADAR", ticker=ticker, run_id=ctx.run_id,
            system_blocks=prompts.system_blocks(ctx.settings, persona, prompts.TASK_SENTIMENT),
            user=user, output_model=SentimentOutput, persona_sha256=persona.sha256,
        ))

    jobs = [(t, pid) for t in tickers for pid in radar]
    by_ticker: dict[str, list[SentimentOutput]] = {}
    for (t, _), out, _ in parallel_map(one, jobs, ctx.rules.pipeline.max_concurrency):
        if out is not None:
            by_ticker.setdefault(t, []).append(out)
    result = {}
    for t, outs in by_ticker.items():
        result[t] = {
            "sentiment": round(sum(clamp(o.sentiment, -1, 1) for o in outs) / len(outs), 3),
            "intensity": round(sum(clamp(o.intensity, 0, 1) for o in outs) / len(outs), 3),
            "overheating": any(o.overheating for o in outs),
            "themes": sorted({th for o in outs for th in o.themes})[:8],
            "summaries": [o.summary for o in outs],
        }
    return result


# ── SELF_MODIFY (연구 모드 전용) ────────────────────────────────────────

def propose_self_modification(ctx: PipelineContext, persona_id: str, round_summary: str) -> None:
    """제안은 감사 로그에만 남는다. 페르소나 파일은 sha256으로 고정되어 있어 자동 수정이 불가능하다."""
    if not ctx.feature("SELF_MODIFY") or ctx.settings.mode in ("paper", "live"):
        return
    persona = ctx.personas.get(persona_id)
    try:
        ctx.llm.generate(LLMRequest(
            route="screening", agent=f"self_modify:{persona_id}", stage="SELF_MODIFY", run_id=ctx.run_id,
            system_blocks=prompts.system_blocks(ctx.settings, persona, prompts.TASK_SELF_MODIFY),
            user=round_summary, output_model=SelfModifyProposal, persona_sha256=persona.sha256,
        ))
    except LLMError:
        pass


def today_kst() -> date:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Seoul")).date()
