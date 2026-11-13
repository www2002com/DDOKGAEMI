"""마스터 오케스트레이터 (§3.3 한국형 5단계, §1 운영 케이던스).

run_pipeline(): Stage 1 스크리닝 → Stage 2 적대적 리서치 → Stage 3 시나리오 → Stage 4 포트폴리오 구성
               → Stage 5 리밸런싱 → 리스크 게이트·컴플라이언스 → 주문 제안(승인 대기) → 보고서
그 밖에 스케줄러가 부르는 작업: 데이터 수집, DART 모니터링, 감성 레이더, 매크로, NAV 스냅샷, 주문 동기화·미체결 취소.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from autopilot_korea.agents import aux_agents, memory
from autopilot_korea.agents.adversarial import ResearchSummary, run_adversarial
from autopilot_korea.agents.llm import AnthropicLLM, LLMClient
from autopilot_korea.agents.optimizer import Candidate, TargetPortfolio, optimize
from autopilot_korea.agents.personas import PersonaLibrary
from autopilot_korea.agents.rebalancer import RebalancePlan, plan_rebalance
from autopilot_korea.agents.runtime import PipelineContext, last_price
from autopilot_korea.agents.scenario import ScenarioResult, run_scenarios
from autopilot_korea.agents.screener import ScreenedStock, run_screening, score_one
from autopilot_korea.compliance import with_disclaimer
from autopilot_korea.db import Database
from autopilot_korea.db.models import NavHistory, PipelineRun, PriceData, ScreeningResult, StockMaster, utcnow
from autopilot_korea.execution import make_broker
from autopilot_korea.execution.base import AccountSnapshot, Broker
from autopilot_korea.execution.calendar import TradingCalendar
from autopilot_korea.execution.order_manager import OrderManager
from autopilot_korea.risk import max_drawdown, positions_needing_review, pre_execution_gate
from autopilot_korea.settings import Settings

log = logging.getLogger(__name__)


@dataclass
class Decision:
    summary: dict[str, Any]
    screened: list[ScreenedStock]
    research: dict[str, ResearchSummary]
    scenarios: dict[str, ScenarioResult]
    target: TargetPortfolio
    plan: RebalancePlan


class Orchestrator:
    def __init__(self, settings: Settings, db: Database, *, llm: LLMClient | None = None,
                 broker: Broker | None = None, dart: Any = None, market: Any = None):
        settings.validate()
        self.settings, self.db = settings, db
        db.create_all()
        self.personas = PersonaLibrary.from_settings(settings)
        self.personas.validate_config(settings.personas)
        self.llm = llm or AnthropicLLM(settings, db)
        self.calendar = TradingCalendar.from_settings(settings)
        self.broker = broker or make_broker(settings, db, self.price_lookup)
        self.orders = OrderManager(settings, db, self.broker, self.calendar)
        self._dart, self._market = dart, market

    # ── 공용 도우미 ─────────────────────────────────────────────────────

    @property
    def rules(self):
        return self.settings.constraints

    def price_lookup(self, ticker: str, as_of: date | None = None) -> float | None:
        with self.db.session() as s:
            q = select(PriceData.close).where(PriceData.ticker == ticker, PriceData.close.is_not(None))
            if as_of:
                q = q.where(PriceData.date <= as_of)
            return s.scalars(q.order_by(PriceData.date.desc())).first()

    def dart(self) -> Any:
        if self._dart is None and self.settings.dart_api_key:
            from autopilot_korea.collectors.sources.dart import get_dart_client

            self._dart = get_dart_client(self.settings)
        return self._dart

    def market(self) -> Any:
        if self._market is None:
            from autopilot_korea.collectors.sources.market import get_market_source

            self._market = get_market_source(self.settings)
        return self._market

    def quote_provider(self) -> Any:
        """#21~#24 호가는 토스증권 호가 API가 원천이다. live가 아니면 호가 변수는 missing으로 남는다."""
        return self.broker if self.broker.name == "toss" else None

    def new_context(self, as_of: date, trigger: str) -> PipelineContext:
        run_id = f"{as_of:%Y%m%d}-{uuid.uuid4().hex[:8]}"
        with self.db.session() as s:
            s.add(PipelineRun(run_id=run_id, trigger=trigger, mode=self.settings.mode))
        return PipelineContext(self.settings, self.db, self.llm, self.personas, run_id, as_of)

    def _finish(self, ctx: PipelineContext, status: str, summary: dict[str, Any]) -> None:
        with self.db.session() as s:
            run = s.get(PipelineRun, ctx.run_id)
            run.status, run.finished_at, run.summary = status, utcnow(), summary

    def universe(self, as_of: date) -> list[str]:
        """KOSPI200 + KOSDAQ150 중 유동성 하위 제외 (§7-4)."""
        from autopilot_korea.collectors.universe import liquid_universe

        with self.db.session() as s:
            tickers = liquid_universe(s, as_of, self.rules.risk.liquidity_exclude_bottom_pct)
            if not tickers:
                tickers = list(s.scalars(select(StockMaster.ticker).where(
                    (StockMaster.in_kospi200 | StockMaster.in_kosdaq150) & StockMaster.is_cash_etf.is_(False))).all())
        return sorted(tickers)

    def load_inputs(self, ctx: PipelineContext, tickers: list[str]) -> None:
        from autopilot_korea.collectors.factsheet import build_factsheets
        from autopilot_korea.collectors.news_collector import recent_news

        ctx.factsheets = build_factsheets(self.db, tickers, ctx.as_of, quotes=self.quote_provider(), persist=True)
        with self.db.session() as s:
            ctx.news = {t: recent_news(s, t, ctx.as_of, self.rules.pipeline.news_window_days) for t in tickers}
        ctx.macro = aux_agents.latest_macro(ctx)
        if ctx.feature("SOCIAL"):
            ctx.sentiment = self.latest_sentiment(ctx.as_of)

    def latest_sentiment(self, as_of: date) -> dict[str, dict]:
        since = as_of - timedelta(days=self.rules.pipeline.news_window_days)
        with self.db.session() as s:
            rows = s.scalars(select(ScreeningResult).where(ScreeningResult.stage == "RAD", ScreeningResult.date > since,
                                                           ScreeningResult.date <= as_of).order_by(ScreeningResult.id)).all()
            return {r.ticker: r.payload for r in rows}

    def nav_drawdown(self) -> float:
        with self.db.session() as s:
            navs = list(s.scalars(select(NavHistory.nav).order_by(NavHistory.date)).all())
        return max_drawdown(navs)

    # ── 5단계 파이프라인 ────────────────────────────────────────────────

    def run_pipeline(self, as_of: date | None = None, trigger: str = "schedule",
                     universe: list[str] | None = None) -> dict[str, Any]:
        """주 2회 전체 파이프라인: 결정(Stage 1~5) → 리스크 게이트 → 주문 제안·승인 대기 → 보고서."""
        as_of = as_of or aux_agents.today_kst()
        ctx = self.new_context(as_of, trigger)
        try:
            account = self.broker.account()
            ctx.drawdown_review = self.nav_drawdown() <= self.rules.risk.portfolio_mdd_review
            d = self.decide(ctx, account, universe)
            d.summary["stage5"] = self.propose_orders(ctx, d.plan, account, d.target)
            d.summary["report"] = str(write_report(self.settings, ctx, d.summary, d.screened, d.research,
                                                   d.scenarios, d.target, d.plan))
            self._finish(ctx, "completed", d.summary)
            return d.summary
        except Exception as exc:
            self._finish(ctx, "failed", {"run_id": ctx.run_id, "error": repr(exc)})
            raise

    def decide(self, ctx: PipelineContext, account: AccountSnapshot, universe: list[str] | None = None) -> "Decision":
        """Stage 1~5의 결정만 한다 (주문 없음). 운영 파이프라인과 백테스트(§14)가 같은 경로를 쓴다."""
        summary: dict[str, Any] = {"run_id": ctx.run_id, "as_of": ctx.as_of.isoformat(), "mode": self.settings.mode,
                                   "drawdown_review": ctx.drawdown_review}
        cash_etfs = self.rules.portfolio.cash_etf_tickers
        holdings = {t for t in account.holdings if t not in cash_etfs}
        tickers = sorted(set(universe or self.universe(ctx.as_of)) | holdings)
        self.load_inputs(ctx, tickers + sorted(cash_etfs))
        equities = [t for t in tickers if t in ctx.factsheets]

        # Stage 1 스크리닝 → 상위 50 (+ 보유 종목은 항상 다음 단계로)
        screened, s1 = run_screening(ctx, equities)
        scores = {s.ticker: s.score for s in screened}
        top = [s.ticker for s in screened[: self.rules.pipeline.screening_top_n]]
        s2_input = list(dict.fromkeys(top + [t for t in holdings if t in scores]))
        summary["stage1"] = {**s1, "top": top[:10]}

        # Stage 2 적대적 리서치 (불 15 vs 베어 15)
        research = run_adversarial(ctx, s2_input)
        survivors = [t for t in s2_input if t in research and research[t].flag == "keep"]
        s3_input = list(dict.fromkeys(survivors[: self.rules.pipeline.scenario_max_candidates]
                                      + [t for t in holdings if t in research]))
        summary["stage2"] = {"researched": len(research), "keep": len(survivors),
                             "watch": sorted(t for t, r in research.items() if r.flag == "watch")}

        # Stage 3 시나리오 모델링
        scenarios = run_scenarios(ctx, s3_input, research)
        summary["stage3"] = {"modeled": len(scenarios),
                             "positive_er": sorted(t for t, r in scenarios.items() if r.expected_return > 0)}

        # §7-2 thesis 재평가 (-25%)
        forced_exits: set[str] = set()
        reviews = {}
        for t in positions_needing_review(account, self.rules.risk.position_thesis_review):
            if t in ctx.factsheets and t not in cash_etfs:
                reviews[t] = aux_agents.review_thesis(ctx, t, account.holdings[t].pnl_pct or 0.0)
                if reviews[t]["verdict"] == "exit":
                    forced_exits.add(t)
        summary["thesis_reviews"] = reviews

        # Stage 4 포트폴리오 구성
        candidates = self.build_candidates(ctx, screened, research, scenarios, account, forced_exits)
        target = optimize(ctx, candidates, holdings_summary(account))
        summary["stage4"] = {"source": target.source, "attempts": target.attempts, "violations": target.violations,
                             "weights": target.weights()}

        # Stage 5 리밸런싱 계획 (스왑 기준·회전율·유동성 분할·오케스트레이터 검토)
        plan = self.rebalance(ctx, target, account, scores, candidates, forced_exits)
        return Decision(summary, screened, research, scenarios, target, plan)

    def build_candidates(self, ctx: PipelineContext, screened: list[ScreenedStock], research: dict[str, ResearchSummary],
                         scenarios: dict[str, ScenarioResult], account: AccountSnapshot, forced_exits: set[str]) -> dict[str, Candidate]:
        weights = account.weights()
        s1 = {s.ticker: s for s in screened}
        candidates: dict[str, Candidate] = {}
        for t, sc in scenarios.items():
            fs = ctx.factsheets[t]
            r = research.get(t)
            flags = sorted(set(sc.flags) | set(r.risk_flags if r else []) | set(getattr(fs, "extras", {}).get("risk_flags", [])))
            candidates[t] = Candidate(
                ticker=t, name=fs.name, sector=fs.sector, score=s1[t].score if t in s1 else None,
                expected_return=None if t in forced_exits else sc.expected_return,
                confidence=min(sc.confidence, s1[t].confidence) if t in s1 else sc.confidence,
                market_cap=float(fs.variables.get("market_cap") or 0), beta=fs.variables.get("beta"),
                bull_strength=r.bull_strength if r else None, bear_strength=r.bear_strength if r else None,
                research_flag=r.flag if r else None, target_12m=sc.targets.get("12m"), risk=sc.risk,
                risk_flags=flags, current_weight=weights.get(t, 0.0),
                thesis_hint=(s1[t].reasoning if t in s1 else ""),
            )
        for etf in self.rules.portfolio.cash_etfs:
            candidates[etf.ticker] = Candidate(ticker=etf.ticker, name=etf.name, sector="현금성", score=None,
                                               expected_return=None, is_cash_etf=True, current_weight=weights.get(etf.ticker, 0.0))
        # 시나리오가 없는 보유 종목도 후보에 넣는다 — 기대수익이 없으므로 편출 대상이 된다.
        for t in account.holdings:
            if t not in candidates and t in ctx.factsheets:
                fs = ctx.factsheets[t]
                candidates[t] = Candidate(ticker=t, name=fs.name, sector=fs.sector, score=s1[t].score if t in s1 else None,
                                          expected_return=None, current_weight=weights.get(t, 0.0))
        return candidates

    def rebalance(self, ctx: PipelineContext, target: TargetPortfolio, account: AccountSnapshot, scores: dict[str, float],
                  candidates: dict[str, Candidate], forced_exits: set[str]) -> RebalancePlan:
        prices, adv, quotes = {}, {}, {}
        for t in set(target.weights()) | set(account.holdings):
            fs = ctx.factsheets.get(t)
            prices[t] = (last_price(fs) if fs else None) or self.price_lookup(t, ctx.as_of)
            if fs:
                adv[t] = (getattr(fs, "extras", None) or {}).get("avg_trading_value_20d")
                v = fs.variables
                if v.get("bid") and v.get("ask"):
                    quotes[t] = {"bid": v["bid"], "ask": v["ask"]}
        recent = self.orders.recent_turnover(ctx.as_of, account.total_value)
        return plan_rebalance(ctx, target, account, scores, candidates, {t: p for t, p in prices.items() if p}, adv, quotes,
                              self.calendar, recent, forced_exits)

    def propose_orders(self, ctx: PipelineContext, plan: RebalancePlan, account: AccountSnapshot,
                       target: TargetPortfolio) -> dict[str, Any]:
        flags = {}
        for o in plan.orders:
            if o.ticker not in flags:
                try:
                    flags[o.ticker] = self.broker.regulatory_flags(o.ticker)
                except Exception as exc:  # noqa: BLE001 — 조회 실패 시 매수는 막는다(보수적)
                    flags[o.ticker] = ["규제상태_조회실패"] if o.side == "BUY" else []
                    log.warning("규제 상태 조회 실패 %s: %s", o.ticker, exc)
        pre_execution_gate(plan.orders, self.settings, flags)
        ids = self.orders.propose(ctx.run_id, plan.orders)
        approved = self.orders.auto_approve(ctx.run_id)
        submitted = self.orders.submit_approved(ctx.as_of) if self.settings.mode == "paper" else {}
        self.update_journal(ctx, plan, account, target)
        return {"orders": len(ids), "blocked": sum(1 for o in plan.orders if o.blocked_reason),
                "auto_approved": approved, "submitted": submitted, "turnover": round(plan.turnover, 4),
                "turnover_scale": round(plan.turnover_scale, 4), "notes": plan.notes,
                "awaiting_human_approval": self.settings.mode == "live" or approved < len(ids)}

    def update_journal(self, ctx: PipelineContext, plan: RebalancePlan, account: AccountSnapshot, target: TargetPortfolio) -> None:
        """반증 가능한 서면 thesis를 신규 편입 시 열고, 전량 편출 시 닫는다 (§8-1)."""
        positions = {p["ticker"]: p for p in target.positions}
        cash = self.rules.portfolio.cash_etf_tickers
        with self.db.session() as s:
            open_ = memory.open_theses(s)
            for t, w in plan.target_weights.items():
                if t in cash or t in open_ or t not in positions:
                    continue
                p = positions[t]
                fs = ctx.factsheets.get(t)
                memory.open_thesis(s, t, thesis=p["thesis"], falsification=p["falsification"],
                                   entry_price=last_price(fs) if fs else None, target_12m=None, risk=p["risk"])
            for t in account.holdings:
                if t not in plan.target_weights and t in open_:
                    memory.close_thesis(s, t, f"{ctx.run_id} 리밸런싱 편출")

    # ── 이벤트·스케줄 작업 ─────────────────────────────────────────────

    def rescore(self, ticker: str, as_of: date | None = None, trigger: str = "event") -> ScreenedStock:
        """DART 주요 공시 감지 시 해당 종목 즉시 재스코어링 (§3.3 Stage 5 이벤트 트리거)."""
        as_of = as_of or aux_agents.today_kst()
        ctx = self.new_context(as_of, trigger)
        self.load_inputs(ctx, [ticker])
        result = score_one(ctx, ticker)
        self._finish(ctx, "completed", {"rescore": ticker, "score": result.score})
        return result

    def run_dart_monitor(self, as_of: date | None = None) -> dict[str, Any]:
        from autopilot_korea.agents.dart_monitor import run_monitor

        dart = self.dart()
        if dart is None:
            return {"skipped": "DART_API_KEY 없음"}
        as_of = as_of or aux_agents.today_kst()
        ctx = self.new_context(as_of, "dart_monitor")
        holdings = set(self.broker.account().holdings)
        res = run_monitor(ctx, dart, holdings, lambda t: self.rescore(t, as_of, f"event:{ctx.run_id}"))
        summary = {"new": res.new, "high": res.high, "rescored": res.rescored, "thesis_alerts": res.thesis_alerts}
        self._finish(ctx, "completed", summary)
        return summary

    def run_sentiment_radar(self, as_of: date | None = None, tickers: list[str] | None = None) -> dict[str, Any]:
        from autopilot_korea.collectors.sources.naver import fetch_board_titles

        as_of = as_of or aux_agents.today_kst()
        ctx = self.new_context(as_of, "sentiment_radar")
        tickers = tickers or sorted(set(self.broker.account().holdings) - self.rules.portfolio.cash_etf_tickers)
        if not tickers:
            self._finish(ctx, "skipped", {"reason": "대상 종목 없음"})
            return {"skipped": "대상 종목 없음"}
        self.load_inputs(ctx, tickers)
        boards = {}
        for t in tickers:
            try:
                boards[t] = fetch_board_titles(t, pages=1)
            except Exception as exc:  # noqa: BLE001
                log.warning("종목토론방 수집 실패 %s: %s", t, exc)
        result = aux_agents.run_sentiment(ctx, tickers, boards)
        with self.db.session() as s:
            for t, payload in result.items():
                s.add(ScreeningResult(run_id=ctx.run_id, date=as_of, ticker=t, stage="RAD", score=payload["sentiment"],
                                      score_reasoning="감성 레이더 (분석 전용)", payload=payload))
        self._finish(ctx, "completed", {"tickers": len(result)})
        return result

    def run_macro(self, as_of: date | None = None) -> dict[str, Any]:
        ctx = self.new_context(as_of or aux_agents.today_kst(), "macro")
        payload = aux_agents.run_macro(ctx)
        self._finish(ctx, "completed", {"regime": payload.get("regime")})
        return payload

    def snapshot_nav(self, as_of: date | None = None, benchmark_close: float | None = None) -> dict[str, Any]:
        as_of = as_of or aux_agents.today_kst()
        account = self.broker.account()
        positions = {t: {"qty": h.quantity, "price": h.last_price, "value": h.market_value} for t, h in account.holdings.items()}
        with self.db.session() as s:
            row = s.get(NavHistory, as_of) or NavHistory(date=as_of, nav=0, cash=0, invested=0)
            row.nav, row.cash, row.invested = account.total_value, account.cash, account.total_value - account.cash
            row.positions, row.benchmark_close = positions, benchmark_close
            s.merge(row)
        return {"date": as_of.isoformat(), "nav": account.total_value, "cash": account.cash}

    def sync_orders(self, as_of: date | None = None) -> dict[str, Any]:
        as_of = as_of or aux_agents.today_kst()
        submitted = self.orders.submit_approved(as_of)
        synced = self.orders.sync()
        canceled = self.orders.cancel_stale(as_of, self.settings.schedule["monitors"]["stale_orders"]["cancel_after_trading_days"])
        return {"submitted": submitted, "synced": synced, "canceled_stale": canceled}


def holdings_summary(account: AccountSnapshot) -> str:
    if not account.holdings:
        return f"(보유 종목 없음) 현금 {account.cash:,.0f}원"
    lines = [f"현금 {account.cash:,.0f}원 / 총평가 {account.total_value:,.0f}원"]
    weights = account.weights()
    for t, h in sorted(account.holdings.items(), key=lambda kv: -kv[1].market_value):
        pnl = f"{h.pnl_pct:+.1%}" if h.pnl_pct is not None else "-"
        lines.append(f"- {t} {h.name} {h.quantity}주, 비중 {weights.get(t, 0):.2%}, 손익 {pnl}")
    return "\n".join(lines)


def write_report(settings: Settings, ctx: PipelineContext, summary: dict, screened: list[ScreenedStock],
                 research: dict[str, ResearchSummary], scenarios: dict[str, ScenarioResult],
                 target: TargetPortfolio, plan: RebalancePlan) -> Path:
    """사람이 서명하기 전에 읽는 실행 보고서. 끝에 고지 문구가 붙는다."""
    path = settings.project_root / "data" / "reports" / f"{ctx.run_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    name = lambda t: ctx.factsheets[t].name if t in ctx.factsheets else t  # noqa: E731
    lines = [f"# 파이프라인 실행 보고서 {ctx.run_id}", "",
             f"- 기준일: {ctx.as_of} / 모드: {settings.mode} / 생성: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
             f"- 드로다운 재검토 모드: {'예' if ctx.drawdown_review else '아니오'}", "",
             "## Stage 1 스크리닝 상위 15", "", "| 순위 | 종목 | 점수 | 근거 |", "|---|---|---|---|"]
    for i, s in enumerate(screened[:15], 1):
        lines.append(f"| {i} | {name(s.ticker)} ({s.ticker}) | {s.score:.0f} | {s.reasoning} |")
    lines += ["", "## Stage 2 적대적 리서치", "", "| 종목 | 불 | 베어 | 판정 |", "|---|---|---|---|"]
    for t, r in sorted(research.items(), key=lambda kv: -kv[1].divergence)[:20]:
        lines.append(f"| {name(t)} ({t}) | {r.bull_strength:.0f} | {r.bear_strength:.0f} | {r.flag}{' (접전)' if r.contested else ''} |")
    lines += ["", "## Stage 3 시나리오", "", "| 종목 | 불/베이스/베어 확률 | 12개월 목표 | 기대수익 | 플래그 |", "|---|---|---|---|---|"]
    for t, sc in sorted(scenarios.items(), key=lambda kv: -kv[1].expected_return):
        p = sc.probs
        lines.append(f"| {name(t)} ({t}) | {p['bull']:.0%}/{p['base']:.0%}/{p['bear']:.0%} | {sc.targets.get('12m', 0):,.0f} | {sc.expected_return:+.1%} | {', '.join(sc.flags)} |")
    lines += ["", f"## Stage 4 목표 포트폴리오 ({target.source}, 시도 {target.attempts}회)", "",
              "| 종목 | 섹터 | 비중 | 논지 | 반증 조건 |", "|---|---|---|---|---|"]
    for p in sorted(target.positions, key=lambda p: -p["weight"]):
        lines.append(f"| {p.get('name', p['ticker'])} ({p['ticker']}) | {p['sector']} | {p['weight']:.1%} | {p['thesis']} | {p['falsification']} |")
    if target.violations:
        lines += ["", f"폴백 사유: {target.violations}"]
    lines += ["", "## Stage 5 리밸런싱", "", f"- 회전율(이번): {plan.turnover:.1%}, 축소 배율: {plan.turnover_scale:.0%}"]
    lines += [f"- {n}" for n in plan.notes]
    lines += ["", "| 결정 | 편출 | 편입 | 규칙 |", "|---|---|---|---|"]
    for d in plan.decisions:
        lines.append(f"| {d['replace_decision']} | {d['ticker_out']} | {d['ticker_in']} | {d['rule']} |")
    if plan.review:
        lines += ["", "### 오케스트레이터 검토", "", plan.review.reasoning]
    lines += ["", "## 주문 (승인 대기 포함)", "", "| 종목 | 방향 | 수량 | 지정가 | 상태 | 비고 |", "|---|---|---|---|---|---|"]
    for o in plan.orders:
        status = "차단" if o.blocked_reason else ("사람 검토 필요" if o.needs_review else "제안")
        lines.append(f"| {name(o.ticker)} ({o.ticker}) | {o.side} | {o.quantity} | {o.price_limit or '시장가'} | {status} | {o.blocked_reason or o.reasoning} |")
    path.write_text(with_disclaimer("\n".join(lines)) + "\n", encoding="utf-8")
    return path
