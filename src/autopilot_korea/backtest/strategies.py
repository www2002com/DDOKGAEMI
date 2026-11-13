"""백테스트 전략: 전체 5단계 파이프라인, 통제군(01_default·04_minimal·05_hold_llm), 강건성 스윕 (§14).

통제군은 Stage 1만 쓰는 GPT Portfolio형 전략이다: 페르소나 하나로 전 종목을 점수 매기고 상위 N개를 동일 비중으로 담는다.
강건성 스윕은 같은 구조에서 과제 문구(paraphrase×4), 프레이밍, 목표함수(순위 기준), 격자(상위 N)를 바꾼다.
LLM 호출이 많으므로 estimate_llm_calls로 비용 규모를 먼저 확인한다.
"""

from __future__ import annotations

import dataclasses
import itertools
import uuid
from datetime import date
from typing import Any, Callable

from autopilot_korea.agents import prompts
from autopilot_korea.agents.orchestrator import Orchestrator
from autopilot_korea.agents.runtime import PipelineContext
from autopilot_korea.agents.screener import run_screening
from autopilot_korea.execution.base import AccountSnapshot, Holding

# ── 강건성 스윕 차원 ────────────────────────────────────────────────────

PARAPHRASES: dict[str, str] = {
    "p0": prompts.TASK_SCREENING,
    "p1": prompts.TASK_SCREENING.replace(
        "종목 하나의 팩트시트와 최근 7일 뉴스를 받아 1~100점으로 평가합니다.",
        "아래 종목의 97개 변수와 최근 7일 뉴스를 검토해 1점에서 100점 사이의 점수를 매깁니다."),
    "p2": prompts.TASK_SCREENING.replace(
        "점수는 앞으로 3~12개월 동안 이 종목이 KOSPI200 대비 초과 수익을 낼 가능성을 나타냅니다.",
        "점수가 높을수록 향후 3~12개월 KOSPI200보다 좋은 성과를 낼 가능성이 크다는 뜻입니다."),
    "p3": prompts.TASK_SCREENING.replace(
        "점수는 유니버스 전체에서 비교되므로 근거가 약하면 극단적인 점수를 피하십시오.",
        "여러 종목의 점수를 서로 비교하므로, 근거가 충분하지 않다면 중간에 가까운 점수를 주십시오."),
}

FRAMINGS: dict[str, str] = {
    "excess": "",
    "risk_adjusted": "\n평가 기준은 단순 초과 수익이 아니라 변동성 대비 초과 수익(위험 조정 성과)입니다.",
    "downside": "\n평가할 때 하락 위험을 피하는 것을 상승 기회만큼 중요하게 보십시오.",
}

OBJECTIVES: dict[str, Callable[[Any], float]] = {
    "score": lambda s: s.score,
    "score_x_confidence": lambda s: s.score * s.confidence,
}


class PipelineStrategy:
    """전체 5단계 파이프라인. 웹검색은 현재 뉴스를 돌려주므로 백테스트에서는 끈다 (룩어헤드 차단)."""

    def __init__(self, orch: Orchestrator, name: str = "pipeline"):
        models = dict(orch.settings.models)
        models["web_search"] = dict(models.get("web_search", {}), enabled=False)
        orch.settings = dataclasses.replace(orch.settings, models=models)
        self.orch, self.name = orch, name

    def target_weights(self, signal_date: date, current: dict[str, float]) -> dict[str, float]:
        account = synthetic_account(current, self.orch.rules.portfolio.initial_capital_krw)
        ctx = self.orch.new_context(signal_date, f"backtest:{self.name}")
        return self.orch.decide(ctx, account).plan.target_weights


class ScreenTopN:
    """Stage 1 단일 페르소나 + 상위 N 동일 비중 (통제군·스윕 공용)."""

    def __init__(self, orch: Orchestrator, persona_id: str, *, top_n: int = 15, task: str | None = None,
                 objective: str = "score", name: str | None = None):
        self.orch, self.persona_id, self.top_n, self.task, self.objective = orch, persona_id, top_n, task, objective
        self.name = name or f"screen:{persona_id}:top{top_n}"

    def target_weights(self, signal_date: date, current: dict[str, float]) -> dict[str, float]:
        ctx = PipelineContext(self.orch.settings, self.orch.db, self.orch.llm, self.orch.personas,
                              f"bt-{uuid.uuid4().hex[:8]}", signal_date)
        tickers = self.orch.universe(signal_date)
        self.orch.load_inputs(ctx, tickers)
        screened, _ = run_screening(ctx, [t for t in tickers if t in ctx.factsheets],
                                    persona_id=self.persona_id, task=self.task, stage="BT")
        rank = OBJECTIVES[self.objective]
        top = sorted(screened, key=lambda s: (-rank(s), -s.market_cap))[: self.top_n]
        return {s.ticker: 1 / len(top) for s in top} if top else {}


class HoldStrategy:
    """05_hold_llm 통제군: 첫 리밸런싱에서 고른 포트폴리오를 끝까지 보유한다."""

    def __init__(self, inner: ScreenTopN, name: str = "control:05_hold_llm"):
        self.inner, self.name, self.initial = inner, name, None

    def target_weights(self, signal_date: date, current: dict[str, float]) -> dict[str, float]:
        if self.initial is None:
            self.initial = self.inner.target_weights(signal_date, current)
            return self.initial
        return current


def control_groups(orch: Orchestrator, top_n: int = 15) -> list:
    return [
        ScreenTopN(orch, "01_default", top_n=top_n, name="control:01_default"),
        ScreenTopN(orch, "04_minimal", top_n=top_n, name="control:04_minimal"),
        HoldStrategy(ScreenTopN(orch, "05_hold_llm", top_n=top_n)),
    ]


def sweep_strategies(orch: Orchestrator, *, paraphrases=("p0", "p1", "p2", "p3"), framings=("excess",),
                     objectives=("score",), grid_top_n=(15,), persona_id: str = "01_default") -> list[ScreenTopN]:
    out = []
    for p, f, o, n in itertools.product(paraphrases, framings, objectives, grid_top_n):
        out.append(ScreenTopN(orch, persona_id, top_n=n, task=PARAPHRASES[p] + FRAMINGS[f], objective=o,
                              name=f"sweep:{p}:{f}:{o}:top{n}"))
    return out


def estimate_llm_calls(n_strategies: int, n_rebalances: int, universe_size: int) -> int:
    """Stage 1 전략 기준 LLM 호출 수 = 전략 수 × 리밸런싱 횟수 × 유니버스 크기."""
    return n_strategies * n_rebalances * universe_size


def synthetic_account(weights: dict[str, float], nav: float) -> AccountSnapshot:
    """백테스트용 가상 계좌: 비중을 1원 단가의 수량으로 표현한다 (비중 비교에만 쓴다)."""
    holdings = {t: Holding(ticker=t, quantity=int(w * nav), avg_price=1.0, last_price=1.0) for t, w in weights.items() if w > 0}
    invested = sum(h.market_value for h in holdings.values())
    return AccountSnapshot(cash=max(nav - invested, 0.0), holdings=holdings)
