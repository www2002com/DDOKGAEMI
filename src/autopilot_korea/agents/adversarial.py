"""Stage 2 — 적대적 리서치 (§3.2, §3.3, §3.4).

종목마다 불 에이전트 15개와 베어 에이전트 15개가 독립적으로 논거를 만든다.
각 에이전트 = 페르소나 프롬프트 + 입장 지시 + 웹검색 도구(허용 도메인 제한) + 최근 7일 제한.
집계: 입장별 강도의 중앙값 → 불 > 베어면 후보 유지(keep), 아니면 관찰 플래그(watch).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from autopilot_korea.agents import memory, prompts
from autopilot_korea.agents.llm import LLMRequest
from autopilot_korea.agents.runtime import PipelineContext, parallel_map
from autopilot_korea.agents.schemas import AdversarialOutput, clamp
from autopilot_korea.db.models import AdversarialOpinion, ScreeningResult

CONTESTED_GAP = 10  # 불·베어 강도 차이가 이보다 작으면 "접전"으로 표시


@dataclass
class ResearchSummary:
    ticker: str
    bull_strength: float
    bear_strength: float
    bull_case: str
    bear_case: str
    bull_points: list[str] = field(default_factory=list)
    bear_points: list[str] = field(default_factory=list)
    n_bull: int = 0
    n_bear: int = 0
    risk_flags: list[str] = field(default_factory=list)

    @property
    def divergence(self) -> float:
        return self.bull_strength - self.bear_strength

    @property
    def flag(self) -> str:
        return "keep" if self.bull_strength > self.bear_strength else "watch"

    @property
    def contested(self) -> bool:
        return abs(self.divergence) < CONTESTED_GAP


def panel(ctx: PipelineContext, stance: str) -> list[str]:
    key, count = ("stage2_bull", ctx.rules.pipeline.bull_agents) if stance == "bull" else ("stage2_bear", ctx.rules.pipeline.bear_agents)
    return list(ctx.settings.personas["panels"][key])[:count]


def _recent_evidence(out: AdversarialOutput, as_of: date, days: int) -> list[dict]:
    """최근 7일 규칙을 코드로 다시 강제한다 — 기간 밖이거나 날짜를 읽을 수 없는 근거는 버린다."""
    kept = []
    for ev in out.evidence:
        try:
            d = date.fromisoformat(ev.date[:10])
        except ValueError:
            continue
        if as_of - timedelta(days=days) < d <= as_of:
            kept.append(ev.model_dump())
    return kept


def research_one(ctx: PipelineContext, ticker: str, persona_id: str, stance: str) -> AdversarialOpinion:
    persona = ctx.personas.get(persona_id)
    with ctx.db.session() as s:
        notes = memory.recent_notes(s, persona.id, ctx.settings.features.get("memory_notes_limit", 10)) if ctx.feature("MEMORY") else []
        prev = memory.last_opinion(s, ticker, persona.id, stance, ctx.run_id) if ctx.feature("LAST_REASONING") else None
    user = prompts.join_blocks(
        prompts.factsheet_block(ctx.factsheets[ticker]),
        prompts.news_block(ctx.news.get(ticker, []), ctx.rules.pipeline.news_window_days),
        prompts.sentiment_block(ctx.sentiment.get(ticker)) if ctx.feature("SOCIAL") else "",
        prompts.memory_block(notes),
        prompts.last_reasoning_block(prev),
        f"오늘은 {ctx.as_of.isoformat()}입니다. {(ctx.as_of - timedelta(days=ctx.rules.pipeline.news_window_days - 1)).isoformat()} 이후 자료만 새로운 정보입니다.",
    )
    out: AdversarialOutput = ctx.llm.generate(LLMRequest(
        route="adversarial", agent=f"S2:{stance}:{persona.id}", stage="S2", ticker=ticker, run_id=ctx.run_id,
        system_blocks=prompts.system_blocks(ctx.settings, persona, prompts.task_adversarial(stance)),
        user=user, output_model=AdversarialOutput, persona_sha256=persona.sha256, web_search=True,
    ))
    evidence = _recent_evidence(out, ctx.as_of, ctx.rules.pipeline.news_window_days)
    opinion = AdversarialOpinion(
        run_id=ctx.run_id, ticker=ticker, persona=persona.id, stance=stance,  # 입장은 지시한 값으로 고정
        strength=clamp(float(out.strength), 0, 100), summary=out.case_summary,
        payload={**out.model_dump(), "stance": stance, "evidence": evidence},
    )
    with ctx.db.session() as s:
        s.add(opinion)
        if ctx.feature("MEMORY"):
            memory.add_note(s, persona.id, out.memory_note, ticker)
    return opinion


def aggregate(ticker: str, opinions: list[AdversarialOpinion]) -> ResearchSummary:
    """입장별 중앙값 집계 (§3.4). 대표 요약은 중앙값에 가장 가까운 의견의 요약이다."""
    def side(stance: str) -> tuple[float, str, list[str], int, list[str]]:
        ops = [o for o in opinions if o.stance == stance]
        if not ops:
            return 0.0, "", [], 0, []
        med = statistics.median(o.strength for o in ops)
        rep = min(ops, key=lambda o: abs(o.strength - med))
        points: list[str] = []
        for o in sorted(ops, key=lambda o: -o.strength):
            for p in o.payload.get("key_points", []):
                if p not in points:
                    points.append(p)
        flags = sorted({f for o in ops for f in o.payload.get("risk_flags", [])})
        return float(med), rep.summary, points[:6], len(ops), flags

    bull_s, bull_case, bull_pts, n_bull, f1 = side("bull")
    bear_s, bear_case, bear_pts, n_bear, f2 = side("bear")
    return ResearchSummary(ticker, bull_s, bear_s, bull_case, bear_case, bull_pts, bear_pts, n_bull, n_bear,
                           sorted(set(f1) | set(f2)))


def run_adversarial(ctx: PipelineContext, tickers: list[str]) -> dict[str, ResearchSummary]:
    jobs = [(t, pid, stance) for t in tickers for stance in ("bull", "bear") for pid in panel(ctx, stance)]
    results = parallel_map(lambda j: research_one(ctx, *j), jobs, ctx.rules.pipeline.max_concurrency)
    by_ticker: dict[str, list[AdversarialOpinion]] = {t: [] for t in tickers}
    for (t, _, _), op, _err in results:
        if op is not None:
            by_ticker[t].append(op)

    summaries: dict[str, ResearchSummary] = {}
    with ctx.db.session() as s:
        for t, ops in by_ticker.items():
            if not ops:
                continue  # 모든 에이전트가 실패한 종목은 이번 라운드에서 제외
            summ = aggregate(t, ops)
            summaries[t] = summ
            s.add(ScreeningResult(
                run_id=ctx.run_id, date=ctx.as_of, ticker=t, stage="S2", score=summ.divergence,
                score_reasoning=f"불 {summ.bull_strength:.0f} vs 베어 {summ.bear_strength:.0f} → {summ.flag}",
                payload={"bull_strength": summ.bull_strength, "bear_strength": summ.bear_strength,
                         "bull_case": summ.bull_case, "bear_case": summ.bear_case,
                         "bull_points": summ.bull_points, "bear_points": summ.bear_points,
                         "flag": summ.flag, "contested": summ.contested,
                         "n_bull": summ.n_bull, "n_bear": summ.n_bear, "risk_flags": summ.risk_flags},
            ))
    return summaries
