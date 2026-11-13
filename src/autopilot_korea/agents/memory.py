"""에이전트 메모리·직전 판단·thesis 저널 (§8)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from autopilot_korea.db.models import AdversarialOpinion, AgentMemory, ScreeningResult, ThesisJournal


def recent_notes(session: Session, agent: str, limit: int = 10) -> list[str]:
    rows = session.scalars(
        select(AgentMemory).where(AgentMemory.agent == agent).order_by(AgentMemory.id.desc()).limit(limit)
    ).all()
    return [f"{r.created_at:%Y-%m-%d} {r.ticker or ''} {r.note}".strip() for r in reversed(rows)]


def add_note(session: Session, agent: str, note: str, ticker: str | None = None) -> None:
    note = (note or "").strip()
    if note:
        session.add(AgentMemory(agent=agent, note=note[:500], ticker=ticker))


def last_screening(session: Session, ticker: str, before_run: str) -> dict[str, Any] | None:
    row = session.scalars(
        select(ScreeningResult)
        .where(ScreeningResult.ticker == ticker, ScreeningResult.stage == "S1", ScreeningResult.run_id != before_run)
        .order_by(ScreeningResult.id.desc())
    ).first()
    if row is None:
        return None
    return {"date": row.date.isoformat(), "score": row.score, "reasoning": row.score_reasoning,
            "price_then": row.payload.get("last_close"), "prediction_t": row.payload.get("price_prediction_t")}


def last_opinion(session: Session, ticker: str, persona: str, stance: str, before_run: str) -> dict[str, Any] | None:
    row = session.scalars(
        select(AdversarialOpinion)
        .where(AdversarialOpinion.ticker == ticker, AdversarialOpinion.persona == persona,
               AdversarialOpinion.stance == stance, AdversarialOpinion.run_id != before_run)
        .order_by(AdversarialOpinion.id.desc())
    ).first()
    if row is None:
        return None
    return {"strength": row.strength, "summary": row.summary, "run_id": row.run_id}


# ── thesis 저널 ─────────────────────────────────────────────────────────

def open_theses(session: Session) -> dict[str, ThesisJournal]:
    rows = session.scalars(select(ThesisJournal).where(ThesisJournal.status == "open")).all()
    return {r.ticker: r for r in rows}


def open_thesis(session: Session, ticker: str, *, thesis: str, falsification: str, entry_price: float | None,
                target_12m: float | None, risk: str | None) -> None:
    session.add(ThesisJournal(ticker=ticker, thesis=thesis, falsification=falsification,
                              entry_price=entry_price, target_12m=target_12m, risk=risk))


def close_thesis(session: Session, ticker: str, reason: str) -> None:
    for row in session.scalars(select(ThesisJournal).where(ThesisJournal.ticker == ticker, ThesisJournal.status == "open")):
        row.status = "closed"
        row.closed_at = datetime.now(timezone.utc)
        row.close_reason = reason


def add_review(session: Session, ticker: str, review: dict[str, Any]) -> None:
    for row in session.scalars(select(ThesisJournal).where(ThesisJournal.ticker == ticker, ThesisJournal.status == "open")):
        row.reviews = [*row.reviews, review]
