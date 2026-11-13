"""DART 주요사항보고 모니터링 (§3.3 이벤트 트리거, §6, §13).

1. DART 공시검색으로 유니버스·보유 종목의 새 공시를 가져온다 (주요사항보고 B + 거래소 공시 I).
2. Haiku(route: dart_triage)가 중요도를 1차 분류한다.
3. high면 Opus(route: dart_deep)가 팩트시트와 함께 재분석한다.
4. rescore가 true면 해당 종목을 즉시 다시 스코어링한다 (Stage 1). 보유 종목이 크게 부정적이면 thesis 재평가로 넘긴다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select

from autopilot_korea.agents import prompts
from autopilot_korea.agents.llm import LLMRequest
from autopilot_korea.agents.runtime import PipelineContext
from autopilot_korea.agents.schemas import DartDeepOutput, DartTriageOutput
from autopilot_korea.db.models import Disclosure, StockMaster

log = logging.getLogger(__name__)
WATCHED_TYPES = ("B", "I")  # B: 주요사항보고, I: 거래소공시(잠정실적·단일판매공급계약 등)


@dataclass
class MonitorResult:
    new: int = 0
    high: list[str] = field(default_factory=list)
    rescored: list[str] = field(default_factory=list)
    thesis_alerts: list[str] = field(default_factory=list)


def fetch_new(ctx: PipelineContext, dart: Any, watch: set[str], lookback_days: int = 1) -> list[dict]:
    bgn = (ctx.as_of - timedelta(days=lookback_days)).strftime("%Y%m%d")
    end = ctx.as_of.strftime("%Y%m%d")
    found: list[dict] = []
    for ty in WATCHED_TYPES:
        for item in dart.list_disclosures(bgn, end, pblntf_ty=ty):
            if item.get("stock_code") in watch:
                found.append(item)
    with ctx.db.session() as s:
        known = set(s.scalars(select(Disclosure.rcept_no).where(Disclosure.rcept_no.in_([f["rcept_no"] for f in found]))).all())
    return [f for f in found if f["rcept_no"] not in known]


def triage(ctx: PipelineContext, item: dict) -> DartTriageOutput:
    persona = ctx.personas.get(ctx.settings.personas["stage_personas"]["dart_monitor"])
    return ctx.llm.generate(LLMRequest(
        route="dart_triage", agent=f"dart_triage:{persona.id}", stage="DART", ticker=item.get("stock_code"),
        run_id=ctx.run_id, system_blocks=prompts.system_blocks(ctx.settings, persona, prompts.TASK_DART_TRIAGE),
        user=f"## 공시\n회사: {item.get('corp_name')} ({item.get('stock_code')})\n제목: {item.get('report_nm')}\n접수일: {item.get('rcept_dt')}",
        output_model=DartTriageOutput, persona_sha256=persona.sha256,
    ))


def deep_analysis(ctx: PipelineContext, item: dict, triage_out: DartTriageOutput) -> DartDeepOutput:
    persona = ctx.personas.get(ctx.settings.personas["stage_personas"]["dart_monitor"])
    ticker = item["stock_code"]
    fs = ctx.factsheets.get(ticker)
    return ctx.llm.generate(LLMRequest(
        route="dart_deep", agent=f"dart_deep:{persona.id}", stage="DART", ticker=ticker, run_id=ctx.run_id,
        system_blocks=prompts.system_blocks(ctx.settings, persona, prompts.TASK_DART_DEEP),
        user=prompts.join_blocks(
            f"## 공시\n{item.get('corp_name')} ({ticker}) — {item.get('report_nm')} ({item.get('rcept_dt')})",
            f"## 1차 분류\n{triage_out.model_dump_json()}",
            prompts.factsheet_block(fs) if fs else "## 팩트시트\n(없음)",
        ),
        output_model=DartDeepOutput, persona_sha256=persona.sha256,
    ))


def run_monitor(ctx: PipelineContext, dart: Any, holdings: set[str],
                rescore: Callable[[str], Any]) -> MonitorResult:
    with ctx.db.session() as s:
        universe = set(s.scalars(select(StockMaster.ticker)).all())
    watch = universe | holdings
    result = MonitorResult()
    for item in fetch_new(ctx, dart, watch):
        result.new += 1
        ticker = item["stock_code"]
        analysis: dict[str, Any] = {}
        importance = "low"
        try:
            t = triage(ctx, item)
            importance, analysis["triage"] = t.importance, t.model_dump()
            if t.importance == "high":
                result.high.append(ticker)
                d = deep_analysis(ctx, item, t)
                analysis["deep"] = d.model_dump()
                if d.rescore:
                    rescore(ticker)
                    result.rescored.append(ticker)
                if ticker in holdings and d.impact == "negative" and d.magnitude >= 4:
                    result.thesis_alerts.append(ticker)
        except Exception as exc:  # noqa: BLE001 — 공시 1건 실패가 모니터 전체를 멈추지 않게
            analysis["error"] = str(exc)
            log.warning("DART 공시 분석 실패 %s: %s", item.get("rcept_no"), exc)
        with ctx.db.session() as s:
            s.add(Disclosure(rcept_no=item["rcept_no"], corp_code=item.get("corp_code", ""), ticker=ticker,
                             report_nm=item.get("report_nm", ""), rcept_dt=_parse_date(item.get("rcept_dt"), ctx.as_of),
                             pblntf_ty=item.get("pblntf_ty"), importance=importance, analysis=analysis,
                             processed_at=datetime.now(timezone.utc)))
    return result


def _parse_date(value: str | None, default: date) -> date:
    try:
        return datetime.strptime(str(value), "%Y%m%d").date()
    except (TypeError, ValueError):
        return default
