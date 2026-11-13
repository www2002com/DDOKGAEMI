"""Stage 4 — 포트폴리오 구성 "Agent of agents" (§3.3, §3.4, §5).

제약은 프롬프트(task_optimizer)와 코드(validate) 두 곳에서 강제한다.
LLM 출력이 제약을 어기면 위반 내용을 피드백해 최대 3회 재생성한다.
그래도 어기면 §15(재시도 → 폴백 → 스킵)에 따라 결정론적 폴백 옵티마이저를 쓴다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from autopilot_korea.agents import prompts
from autopilot_korea.agents.llm import LLMError, LLMRequest
from autopilot_korea.agents.runtime import PipelineContext
from autopilot_korea.agents.schemas import OptimizerOutput
from autopilot_korea.db.models import PortfolioPosition
from autopilot_korea.settings import Constraints

log = logging.getLogger(__name__)
EPS = 1e-9
CASH_SECTOR = "현금성"
FALLBACK_CASH_WEIGHT = 0.05  # 폴백에서 평시 현금성 ETF 비중 (주식 14 + ETF 1 구성)


@dataclass
class Candidate:
    ticker: str
    name: str
    sector: str
    score: float | None
    expected_return: float | None
    confidence: float = 1.0
    market_cap: float = 0.0
    beta: float | None = None
    bull_strength: float | None = None
    bear_strength: float | None = None
    research_flag: str | None = None
    target_12m: float | None = None
    risk: str = ""
    risk_flags: list[str] = field(default_factory=list)
    current_weight: float = 0.0
    is_cash_etf: bool = False
    thesis_hint: str = ""


@dataclass
class TargetPortfolio:
    positions: list[dict[str, Any]]  # ticker, weight, thesis, falsification, edge, risk, sector, expected_return
    source: str  # llm | fallback
    attempts: int
    reasoning: str
    violations: list[str] = field(default_factory=list)  # 마지막 LLM 시도의 위반 (폴백 사유)

    def weights(self) -> dict[str, float]:
        return {p["ticker"]: p["weight"] for p in self.positions}


# ── 코드 검증 ───────────────────────────────────────────────────────────

def eligible_equities(candidates: dict[str, Candidate], rules: Constraints) -> list[Candidate]:
    blocked = set(rules.risk.blocked_flags)
    return [c for c in candidates.values()
            if not c.is_cash_etf and c.expected_return is not None and c.expected_return > 0
            and not (set(c.risk_flags) & blocked)]


def validate(weights: dict[str, float], candidates: dict[str, Candidate], rules: Constraints,
             *, drawdown_review: bool = False) -> list[str]:
    """§5 제약 위반 목록. 비어 있으면 통과."""
    p = rules.portfolio
    v: list[str] = []
    unknown = [t for t in weights if t not in candidates]
    if unknown:
        v.append(f"후보에 없는 종목: {unknown}")
    if any(w <= 0 for w in weights.values()):
        v.append("비중이 0 이하인 포지션이 있습니다")
    known = {t: w for t, w in weights.items() if t in candidates}
    equities = {t: w for t, w in known.items() if not candidates[t].is_cash_etf}
    cash = sum(w for t, w in known.items() if candidates[t].is_cash_etf)

    n_eligible = len(eligible_equities(candidates, rules))
    max_investable = min(1.0, n_eligible * p.max_position_weight + p.max_cash_etf_weight)
    total = sum(weights.values())
    if total > 1 + p.weight_tolerance or total < max_investable - p.weight_tolerance:
        v.append(f"비중 합계 {total:.4f} (허용: {max_investable:.4f}~1.0)")
    if len(weights) > p.target_positions:
        v.append(f"포지션 수 {len(weights)} > {p.target_positions}")
    need = min(p.min_equity_positions, n_eligible)
    if len(equities) < need:
        v.append(f"주식 포지션 {len(equities)}개 < 최소 {need}개")
    for t, w in equities.items():
        if w > p.max_position_weight + EPS:
            v.append(f"{t} 비중 {w:.2%} > {p.max_position_weight:.0%}")
        c = candidates[t]
        if p.require_positive_expected_return and not (c.expected_return is not None and c.expected_return > 0):
            v.append(f"{t} 기대수익 {c.expected_return} ≤ 0 또는 없음")
        if set(c.risk_flags) & set(rules.risk.blocked_flags):
            v.append(f"{t} 차단 플래그 {sorted(set(c.risk_flags) & set(rules.risk.blocked_flags))}")
    if cash > p.max_cash_etf_weight + EPS:
        v.append(f"현금성 ETF {cash:.2%} > {p.max_cash_etf_weight:.0%}")
    if drawdown_review and cash < rules.risk.drawdown_cash_target - p.weight_tolerance:
        v.append(f"드로다운 재검토 모드: 현금성 ETF {cash:.2%} < {rules.risk.drawdown_cash_target:.0%}")
    for sector, sw in sector_weights(equities, candidates).items():
        if sw > p.max_sector_weight + EPS:
            v.append(f"섹터 {sector} {sw:.2%} > {p.max_sector_weight:.0%}")
    return v


def sector_weights(weights: dict[str, float], candidates: dict[str, Candidate]) -> dict[str, float]:
    out: dict[str, float] = {}
    for t, w in weights.items():
        c = candidates[t]
        if not c.is_cash_etf:
            out[c.sector] = out.get(c.sector, 0.0) + w
    return out


# ── 결정론적 폴백 ───────────────────────────────────────────────────────

def capped_allocation(raw: dict[str, float], budget: float, pos_cap: float,
                      sector_of: dict[str, str], sector_cap: float) -> dict[str, float]:
    """raw 비례 배분 후 종목·섹터 상한을 넘는 몫을 남은 종목에 재배분한다 (워터필링)."""
    w = {t: 0.0 for t in raw}
    open_names = {t for t, r in raw.items() if r > 0}
    remaining = budget
    for _ in range(100):
        if remaining <= EPS or not open_names:
            break
        total_raw = sum(raw[t] for t in open_names)
        for t in open_names:
            w[t] += remaining * raw[t] / total_raw
        remaining = 0.0
        for t in list(open_names):
            if w[t] > pos_cap:
                remaining += w[t] - pos_cap
                w[t] = pos_cap
                open_names.discard(t)
        sectors: dict[str, float] = {}
        for t, x in w.items():
            sectors[sector_of[t]] = sectors.get(sector_of[t], 0.0) + x
        for sector, total in sectors.items():
            if total > sector_cap + EPS:
                scale = sector_cap / total
                for t in w:
                    if sector_of[t] == sector:
                        remaining += w[t] * (1 - scale)
                        w[t] *= scale
                        open_names.discard(t)
    return {t: x for t, x in w.items() if x > EPS}


def fallback_portfolio(candidates: dict[str, Candidate], rules: Constraints, *, drawdown_review: bool = False) -> dict[str, float]:
    """확신도 × 기대수익 ÷ 리스크(베타) 비례, 점수·시총 순으로 상위 종목을 고른다."""
    p = rules.portfolio
    eligible = sorted(eligible_equities(candidates, rules), key=lambda c: (-(c.score or 0), -c.market_cap))
    n_equity = p.target_positions - 1  # 마지막 한 자리는 현금성 ETF
    chosen = eligible[:n_equity]
    raw = {c.ticker: max(c.confidence, 0.05) * c.expected_return / max(c.beta or 1.0, 0.5) for c in chosen}
    cash_target = rules.risk.drawdown_cash_target if drawdown_review else FALLBACK_CASH_WEIGHT
    weights = capped_allocation(raw, 1.0 - cash_target, p.max_position_weight,
                                {c.ticker: c.sector for c in chosen}, p.max_sector_weight)
    cash = min(1.0 - sum(weights.values()), p.max_cash_etf_weight)  # 초과분은 미투자 현금으로 남는다
    cash_list = [c for c in candidates.values() if c.is_cash_etf]
    held_cash = [c for c in cash_list if c.current_weight > 0]  # 이미 보유한 현금성 ETF를 유지해 불필요한 교체를 막는다
    cash_etf = (held_cash or cash_list or [None])[0]
    cash_etf = cash_etf.ticker if cash_etf else None
    if cash_etf and cash > EPS:
        weights[cash_etf] = cash
    return weights


# ── LLM 옵티마이저 ──────────────────────────────────────────────────────

def candidate_table(candidates: dict[str, Candidate]) -> str:
    rows = []
    for c in sorted(candidates.values(), key=lambda c: (c.is_cash_etf, -(c.score or 0), -c.market_cap)):
        rows.append({
            "ticker": c.ticker, "name": c.name, "sector": CASH_SECTOR if c.is_cash_etf else c.sector,
            "s1_score": c.score, "confidence": round(c.confidence, 2),
            "bull": c.bull_strength, "bear": c.bear_strength, "s2_flag": c.research_flag,
            "expected_return": None if c.expected_return is None else round(c.expected_return, 4),
            "target_12m": c.target_12m, "beta": c.beta,
            "market_cap_억원": round(c.market_cap / 1e8) if c.market_cap else None,
            "current_weight": round(c.current_weight, 4), "risk_flags": c.risk_flags, "risk": c.risk[:200],
        })
    return "```json\n" + json.dumps(rows, ensure_ascii=False, indent=0) + "\n```"


def optimize(ctx: PipelineContext, candidates: dict[str, Candidate], holdings_summary: str) -> TargetPortfolio:
    rules = ctx.rules
    persona = ctx.personas.get(ctx.settings.personas["stage_personas"]["stage4_optimizer"])
    base_user = prompts.join_blocks(
        "## 후보 표 (Stage 1 점수 · Stage 2 불/베어 강도 · Stage 3 기대수익)", candidate_table(candidates),
        "## 현재 보유 내역", holdings_summary, prompts.macro_block(ctx.macro),
    )
    user, violations, last_out = base_user, [], None
    attempts = 0
    for attempts in range(1, rules.pipeline.optimizer_max_retries + 2):  # 최초 1회 + 재생성 최대 3회
        try:
            out: OptimizerOutput = ctx.llm.generate(LLMRequest(
                route="optimizer", agent=f"S4:{persona.id}", stage="S4", run_id=ctx.run_id,
                system_blocks=prompts.system_blocks(ctx.settings, persona,
                                                    prompts.task_optimizer(ctx.settings, drawdown_review=ctx.drawdown_review)),
                user=user, output_model=OptimizerOutput, persona_sha256=persona.sha256,
            ))
        except LLMError as exc:
            violations = [f"LLM 오류: {exc}"]
            break
        last_out = out
        weights: dict[str, float] = {}
        for pos in out.positions:
            weights[pos.ticker] = weights.get(pos.ticker, 0.0) + pos.weight
        violations = validate(weights, candidates, rules, drawdown_review=ctx.drawdown_review)
        if len(weights) != len(out.positions):
            violations.append("같은 종목이 두 번 이상 나옵니다 — 종목당 한 줄로 합치십시오")
        if not violations:
            positions = [_position(candidates, pos.ticker, pos.weight, pos.thesis, pos.falsification, pos.edge, pos.risk)
                         for pos in out.positions]
            return _persist(ctx, TargetPortfolio(positions, "llm", attempts, out.reasoning))
        user = f"{base_user}\n\n## 직전 출력의 제약 위반 (수정 필요)\n" + "\n".join(f"- {x}" for x in violations)

    log.warning("Stage 4 LLM 제약 충족 실패 → 결정론적 폴백: %s", violations)
    weights = fallback_portfolio(candidates, rules, drawdown_review=ctx.drawdown_review)
    llm_notes = {p.ticker: p for p in (last_out.positions if last_out else [])}
    positions = []
    for t, w in weights.items():
        note = llm_notes.get(t)
        c = candidates[t]
        positions.append(_position(
            candidates, t, w,
            note.thesis if note else (c.thesis_hint or "폴백 옵티마이저: 점수·기대수익 상위"),
            note.falsification if note else "12개월 기대수익이 0 이하로 재평가되거나 Stage 2 베어 강도가 불 강도를 넘으면 논지 폐기",
            note.edge if note else "", note.risk if note else c.risk,
        ))
    return _persist(ctx, TargetPortfolio(positions, "fallback", attempts,
                                         "LLM 옵티마이저가 제약을 충족하지 못해 결정론적 폴백을 사용했습니다.", violations))


def _position(candidates: dict[str, Candidate], ticker: str, weight: float, thesis: str, falsification: str,
              edge: str, risk: str) -> dict[str, Any]:
    c = candidates[ticker]
    return {"ticker": ticker, "weight": round(weight, 6), "thesis": thesis, "falsification": falsification,
            "edge": edge, "risk": risk, "sector": CASH_SECTOR if c.is_cash_etf else c.sector,
            "expected_return": c.expected_return, "name": c.name}


def _persist(ctx: PipelineContext, target: TargetPortfolio) -> TargetPortfolio:
    with ctx.db.session() as s:
        for p in target.positions:
            s.add(PortfolioPosition(run_id=ctx.run_id, date=ctx.as_of, ticker=p["ticker"], weight=p["weight"],
                                    thesis=p["thesis"], edge=p["edge"], risk=p["risk"], sector=p["sector"],
                                    expected_return=p["expected_return"]))
    return target
