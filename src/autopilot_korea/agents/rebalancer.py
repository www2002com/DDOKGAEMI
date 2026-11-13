"""Stage 5 — 상시 리밸런싱 (§3.2, §3.3, §3.4, §7).

규칙 엔진이 결정하고, 마스터 오케스트레이터 모델이 근거를 쓴다.
  1. 스왑 기준(§3.4): 보유 종목 스코어가 신규 편입 후보 최저 스코어보다 10점 이상 낮을 때만 교체한다.
     기준에 못 미치면 Stage 4가 뺀 보유 종목도 유지한다 (실측 "trades less often").
     단, 기대수익 ≤ 0, 차단 플래그, thesis exit 판정 종목은 기준과 무관하게 편출한다.
  2. 목표 비중과 현재 보유의 차이로 주문 수량을 계산한다 (작은 차이는 거래하지 않음).
  3. 30일 회전율 상한을 넘으면 비강제 거래를 비례 축소한다.
  4. 20일 평균 거래대금의 5%를 넘는 주문은 여러 거래일로 나눈다.
  5. 오케스트레이터(route: orchestrator)가 편입·교체 결정마다 근거를 쓴다. Cancel 판정 주문은 사람 검토로 넘긴다.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from autopilot_korea.agents import prompts
from autopilot_korea.agents.llm import LLMError, LLMRequest
from autopilot_korea.agents.optimizer import Candidate, TargetPortfolio, validate
from autopilot_korea.agents.runtime import PipelineContext
from autopilot_korea.agents.schemas import RebalanceReview
from autopilot_korea.execution.base import AccountSnapshot, snap_price
from autopilot_korea.execution.calendar import TradingCalendar
from autopilot_korea.risk import PlannedOrder, split_quantity, turnover_scale

log = logging.getLogger(__name__)


@dataclass
class RebalancePlan:
    target_weights: dict[str, float]
    orders: list[PlannedOrder]
    decisions: list[dict[str, Any]]
    turnover: float
    turnover_scale: float
    notes: list[str] = field(default_factory=list)
    review: RebalanceReview | None = None


def apply_swap_rule(target: dict[str, float], current: dict[str, float], scores: dict[str, float],
                    candidates: dict[str, Candidate], ctx_rules, forced_exits: set[str]) -> tuple[dict[str, float], list[dict], list[str]]:
    """Stage 4 목표에 스왑 기준(히스테리시스)을 적용한다. (조정된 목표, 결정 목록, 메모)"""
    p = ctx_rules.portfolio
    gap = ctx_rules.pipeline.swap_score_gap
    is_cash = lambda t: t in p.cash_etf_tickers  # noqa: E731
    added = sorted((t for t in target if t not in current and not is_cash(t)), key=lambda t: scores.get(t, 0))
    dropped = [t for t in current if t not in target and not is_cash(t)]
    min_new = min((scores[t] for t in added if t in scores), default=None)

    decisions: list[dict] = []
    notes: list[str] = []
    keep: dict[str, float] = {}
    for h in dropped:
        c = candidates.get(h)
        must_exit = (h in forced_exits or c is None or c.expected_return is None or c.expected_return <= 0
                     or bool(set(c.risk_flags) & set(ctx_rules.risk.blocked_flags)))
        s = scores.get(h)
        if must_exit or s is None or (min_new is not None and s <= min_new - gap):
            continue
        keep[h] = min(current[h], p.max_position_weight)
        decisions.append({"ticker_out": "", "ticker_in": h, "replace_decision": "Cancel",
                          "rule": f"보유 스코어 {s:.0f}가 신규 최저 {min_new if min_new is not None else '-'}보다 {gap:.0f}점 이상 낮지 않아 유지"})

    adjusted = dict(target)
    adjusted.update(keep)
    # 포지션 수 상한을 넘으면 스코어가 가장 낮은 신규 편입부터 보류한다.
    while len(adjusted) > p.target_positions and added:
        t = added.pop(0)
        adjusted.pop(t, None)
        decisions.append({"ticker_out": "", "ticker_in": t, "replace_decision": "Cancel",
                          "rule": "기존 보유 유지로 자리가 없어 신규 편입 보류"})
    if keep:
        kept_total = sum(keep.values())
        others = {t: w for t, w in adjusted.items() if t not in keep}
        scale = (1 - kept_total) / sum(others.values()) if others else 0
        adjusted = {**{t: w * scale for t, w in others.items()}, **keep}
        if violations := validate(adjusted, candidates, ctx_rules):
            notes.append(f"스왑 기준 적용 결과가 제약을 위반해 Stage 4 목표를 그대로 씁니다: {violations}")
            adjusted, decisions = dict(target), []

    remaining_added = [t for t in added if t in adjusted]
    exits = [t for t in dropped if t not in adjusted]
    for i, t in enumerate(remaining_added):
        out = exits[i] if i < len(exits) else ""
        decisions.append({"ticker_out": out, "ticker_in": t, "replace_decision": "Replace" if out else "Add",
                          "rule": f"스코어 {scores.get(t, 0):.0f}" + (f" vs 편출 {out} {scores.get(out, 0):.0f}" if out else "")})
    for t in exits[len(remaining_added):]:
        decisions.append({"ticker_out": t, "ticker_in": "", "replace_decision": "Replace",
                          "rule": "편출 (강제 사유 또는 스왑 기준 충족)"})
    return adjusted, decisions, notes


def one_way_turnover(deltas, nav: float) -> float:
    """회전율 = min(매수액, 매도액) ÷ NAV. 현금에서 처음 편입하는 매수는 회전으로 세지 않는다."""
    deltas = list(deltas)
    if nav <= 0:
        return 0.0
    buys = sum(d for d in deltas if d > 0)
    sells = -sum(d for d in deltas if d < 0)
    return min(buys, sells) / nav


def build_orders(target: dict[str, float], account: AccountSnapshot, prices: dict[str, float],
                 adv_value: dict[str, float], quotes: dict[str, dict], settings, calendar: TradingCalendar,
                 today, forced_exits: set[str], recent_turnover: float) -> tuple[list[PlannedOrder], float, float]:
    rules = settings.constraints
    ex = rules.execution
    nav = account.total_value
    cash_tickers = rules.portfolio.cash_etf_tickers
    deltas: dict[str, float] = {}
    for t in set(target) | set(account.holdings):
        price = prices.get(t) or (account.holdings[t].last_price if t in account.holdings else None)
        if not price:
            continue
        cur_value = account.holdings[t].market_value if t in account.holdings else 0.0
        delta = target.get(t, 0.0) * nav - cur_value
        full_exit = t not in target and t in account.holdings
        if not full_exit and (abs(delta) < ex.min_trade_value_krw or abs(delta) / nav < ex.min_weight_change):
            continue
        deltas[t] = delta

    forced = {t for t in deltas if t in forced_exits}
    planned_turnover = one_way_turnover(deltas.values(), nav)
    unforced_turnover = one_way_turnover((d for t, d in deltas.items() if t not in forced), nav)
    scale = turnover_scale(unforced_turnover, recent_turnover, rules.turnover.monthly_cap)

    # 1) 종목별 수량·지정가 결정. 매수 수량은 지정가 기준으로 계산해 체결 시 현금이 모자라지 않게 한다.
    legs: list[tuple[str, str, int, float, float | None]] = []  # (ticker, side, qty, price, limit)
    for t, delta in sorted(deltas.items(), key=lambda kv: kv[1]):  # 매도(음수) 먼저
        price = prices.get(t) or account.holdings[t].last_price
        side = "SELL" if delta < 0 else "BUY"
        held = account.holdings[t].quantity if t in account.holdings else 0
        q = quotes.get(t) or {}
        ref = (q.get("ask") if side == "BUY" else q.get("bid")) or price * (1 + ex.limit_offset_pct if side == "BUY" else 1 - ex.limit_offset_pct)
        limit = snap_price(ref, side, is_etf=t in cash_tickers) if ex.default_order_type == "LIMIT" else None
        if side == "SELL" and t not in target:
            qty = held if t in forced or scale >= 1 else math.floor(held * scale)
        else:
            value = abs(delta) * (1.0 if t in forced else scale)
            qty = math.floor(value / (limit or price)) if side == "BUY" else min(math.floor(value / price), held)
        if qty > 0:
            legs.append((t, side, qty, price, limit))

    # 2) 매수 총액이 (현금 + 매도 예상 대금)을 넘으면 매수를 비례 축소한다 (수수료·세금 반영).
    cost_in = 1 + ex.paper_commission_rate
    cost_out = 1 - ex.paper_commission_rate - ex.paper_sell_tax_rate
    available = account.cash + sum(q * (lim or p) * cost_out for _, s, q, p, lim in legs if s == "SELL")
    buy_cost = sum(q * (lim or p) * cost_in for _, s, q, p, lim in legs if s == "BUY")
    if buy_cost > available > 0:
        shrink = available / buy_cost
        legs = [(t, s, math.floor(q * shrink) if s == "BUY" else q, p, lim) for t, s, q, p, lim in legs]

    orders: list[PlannedOrder] = []
    for t, side, qty, price, limit in legs:
        if qty <= 0:
            continue
        slices = split_quantity(qty, price, adv_value.get(t), rules.risk.max_order_pct_of_adv)
        for i, sq in enumerate(slices):
            orders.append(PlannedOrder(
                ticker=t, side=side, quantity=sq, order_type=ex.default_order_type, price_limit=limit,
                reasoning=f"목표 비중 {target.get(t, 0):.2%} (현재 {account.weights().get(t, 0):.2%})"
                          + (f", 분할 {i + 1}/{len(slices)}" if len(slices) > 1 else ""),
                not_before=calendar.add_trading_days(today, i) if i else None, forced=t in forced,
            ))
    return orders, planned_turnover, scale


def review(ctx: PipelineContext, decisions: list[dict], candidates: dict[str, Candidate],
           target: TargetPortfolio) -> RebalanceReview | None:
    """마스터 오케스트레이터가 각 결정의 완전한 근거를 쓴다."""
    if not decisions:
        return None
    persona = ctx.personas.get(ctx.settings.personas["stage_personas"]["stage5_review"])
    info = {t: {"name": c.name, "sector": c.sector, "s1_score": c.score, "expected_return": c.expected_return,
                "bull": c.bull_strength, "bear": c.bear_strength, "risk": c.risk[:200]}
            for t, c in candidates.items() if any(t in (d["ticker_in"], d["ticker_out"]) for d in decisions)}
    theses = {p["ticker"]: {"thesis": p["thesis"], "falsification": p["falsification"]} for p in target.positions}
    user = prompts.join_blocks(
        "## 규칙 엔진 결정 목록", json.dumps(decisions, ensure_ascii=False),
        "## 관련 종목 정보", json.dumps(info, ensure_ascii=False),
        "## Stage 4 목표 포지션 논지", json.dumps(theses, ensure_ascii=False),
    )
    try:
        return ctx.llm.generate(LLMRequest(
            route="orchestrator", agent=f"S5:{persona.id}", stage="S5", run_id=ctx.run_id,
            system_blocks=prompts.system_blocks(ctx.settings, persona, prompts.TASK_REBALANCE_REVIEW),
            user=user, output_model=RebalanceReview, persona_sha256=persona.sha256,
        ))
    except LLMError as exc:
        log.warning("Stage 5 검토 실패 — 규칙 결정만으로 진행 (live는 어차피 사람 승인 필요): %s", exc)
        return None


def plan_rebalance(ctx: PipelineContext, target: TargetPortfolio, account: AccountSnapshot, scores: dict[str, float],
                   candidates: dict[str, Candidate], prices: dict[str, float], adv_value: dict[str, float],
                   quotes: dict[str, dict], calendar: TradingCalendar, recent_turnover: float,
                   forced_exits: set[str] | None = None) -> RebalancePlan:
    forced_exits = forced_exits or set()
    current = account.weights()
    adjusted, decisions, notes = apply_swap_rule(target.weights(), current, scores, candidates, ctx.rules, forced_exits)
    orders, turnover, scale = build_orders(adjusted, account, prices, adv_value, quotes, ctx.settings, calendar,
                                           ctx.as_of, forced_exits, recent_turnover)
    if scale < 1:
        notes.append(f"30일 회전율 상한({ctx.rules.turnover.monthly_cap:.0%}) 때문에 비강제 거래를 {scale:.0%}로 축소")
    rev = review(ctx, [d for d in decisions if d["replace_decision"] != "Cancel"], candidates, target)
    if rev is not None:
        held_back = {d.ticker_in for d in rev.decisions if d.replace_decision == "Cancel" and d.ticker_in}
        for o in orders:
            if o.side == "BUY" and o.ticker in held_back:
                o.needs_review = True
        reasons = {d.ticker_in or d.ticker_out: d.reasoning for d in rev.decisions}
        for o in orders:
            if o.ticker in reasons:
                o.reasoning = f"{o.reasoning}. {reasons[o.ticker]}"
    return RebalancePlan(adjusted, orders, decisions, turnover, scale, notes, rev)
