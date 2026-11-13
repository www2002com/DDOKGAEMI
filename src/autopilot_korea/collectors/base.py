"""수집 공통 기반: 컨텍스트, 호가 제공자 프로토콜, 재시도·회로 차단기(§15), upsert 도우미."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from autopilot_korea.db.models import CollectionStatus, StockMaster
from autopilot_korea.db.session import Database
from autopilot_korea.settings import Settings

log = logging.getLogger(__name__)
JOB_ROW = "*"  # 작업 전체 상태 행의 ticker 값


class QuoteProvider(Protocol):
    """최우선 호가 제공자 (토스증권 클라이언트가 만족한다). #21~#24."""

    def best_quote(self, ticker: str) -> dict | None:
        """{"bid", "ask", "bid_size", "ask_size"} 또는 None."""


@dataclass
class CollectorContext:
    settings: Settings
    db: Database
    as_of: date
    market: Any  # sources.market.MarketDataSource
    dart: Any = None  # sources.dart.DartClient | None
    quotes: QuoteProvider | None = None


def build_context(settings: Settings, db: Database, as_of: date, quotes: QuoteProvider | None = None) -> CollectorContext:
    """설정에서 시세 원천(pykrx/FDR)과 DART 클라이언트(키가 있을 때)를 골라 컨텍스트를 만든다."""
    from autopilot_korea.collectors.sources.dart import get_dart_client
    from autopilot_korea.collectors.sources.market import get_market_source

    return CollectorContext(settings, db, as_of, get_market_source(settings), get_dart_client(settings), quotes)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite는 tz 정보를 버리므로 naive 값은 UTC로 간주한다."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def upsert(session: Session, model: type, keys: dict[str, Any], values: dict[str, Any]) -> Any:
    """keys로 찾아 values를 덮어쓰고, 없으면 새로 만든다."""
    row = session.scalars(select(model).filter_by(**keys)).first()
    if row is None:
        row = model(**keys, **values)
        session.add(row)
    else:
        for k, v in values.items():
            setattr(row, k, v)
    return row


def corp_code_for(ctx: CollectorContext, ticker: str) -> str | None:
    """stock_master의 DART 고유번호. 없으면 corpCode.xml에서 찾아 저장한다."""
    with ctx.db.session() as s:
        master = s.get(StockMaster, ticker)
        if master and master.corp_code:
            return master.corp_code
    if ctx.dart is None:
        return None
    code = ctx.dart.corp_codes().get(ticker)
    if code:
        with ctx.db.session() as s:
            master = s.get(StockMaster, ticker)
            if master:
                master.corp_code = code
    return code


# ── 재시도·회로 차단기 (§15) ──────────────────────────────────────────

def _status_row(s: Session, job: str, ticker: str) -> CollectionStatus:
    row = s.scalars(select(CollectionStatus).where(CollectionStatus.job == job, CollectionStatus.ticker == ticker)).first()
    if row is None:
        row = CollectionStatus(job=job, ticker=ticker, status="never", consecutive_failures=0)
        s.add(row)
    return row


def _circuit_open(row: CollectionStatus, threshold: int, cooldown: float, now: datetime) -> bool:
    if (row.consecutive_failures or 0) < threshold or row.last_attempt_at is None:
        return False
    return now - as_utc(row.last_attempt_at) < timedelta(seconds=cooldown)


def skip_job(ctx: CollectorContext, job: str, reason: str, tickers: Iterable[str]) -> dict[str, Any]:
    """원천이 없어(키 미설정 등) 작업을 건너뛸 때. 기존 데이터는 그대로 두고 사유만 기록한다."""
    with ctx.db.session() as s:
        row = _status_row(s, job, JOB_ROW)
        row.last_attempt_at, row.error = utcnow(), reason
    return {"job": job, "status": "skipped", "reason": reason, "ok": [], "failed": [], "stale": [], "skipped": list(tickers)}


def run_job(ctx: CollectorContext, job: str, fn: Callable[[CollectorContext, str], Any], tickers: Iterable[str],
            sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """종목별로 fn(ctx, ticker)을 실행한다 (§15 실패 처리).

    - 재시도: schedule.yaml retry.attempts회, 실패 사이에 backoff_seconds만큼 쉰다.
    - 끝내 실패하면 collection_status를 failed로 기록한다. 직전 데이터는 지우지 않으므로 그것이 폴백 스냅샷이 되고,
      이전 성공 이력이 있는 종목은 결과의 "stale" 목록에 올린다(팩트시트 신선도 플래그는 데이터 날짜로 계산된다).
    - 회로 차단기: 작업 단위로 연속 실패가 circuit_breaker.failure_threshold에 이르면 남은 종목을 건너뛰고,
      cooldown_seconds가 지날 때까지 다음 실행도 건너뛴다. 한 번이라도 성공하면 연속 실패 수를 0으로 되돌린다.
    - 한 종목의 실패가 작업 전체를 멈추지 않는다.
    """
    retry = ctx.settings.schedule.get("retry", {})
    attempts = int(retry.get("attempts", 3))
    backoff = list(retry.get("backoff_seconds", [2, 10, 30])) or [0]
    breaker = ctx.settings.schedule.get("circuit_breaker", {})
    threshold = int(breaker.get("failure_threshold", 5))
    cooldown = float(breaker.get("cooldown_seconds", 600))
    tickers = list(tickers)
    result: dict[str, Any] = {"job": job, "status": "ok", "ok": [], "failed": [], "stale": [], "skipped": [], "errors": {}}

    with ctx.db.session() as s:
        if _circuit_open(_status_row(s, job, JOB_ROW), threshold, cooldown, utcnow()):
            result.update(status="circuit_open", skipped=tickers)
            log.warning("[%s] 회로 차단 중 — 쿨다운이 끝날 때까지 건너뜀", job)
            return result

    for i, ticker in enumerate(tickers):
        error: Exception | None = None
        for attempt in range(attempts):
            try:
                fn(ctx, ticker)
                error = None
                break
            except Exception as exc:  # noqa: BLE001 — 종목 단위로 격리한다
                error = exc
                if attempt < attempts - 1:
                    sleep(backoff[min(attempt, len(backoff) - 1)])
        now = utcnow()
        with ctx.db.session() as s:
            row = _status_row(s, job, ticker)
            job_row = row if ticker == JOB_ROW else _status_row(s, job, JOB_ROW)
            row.last_attempt_at = job_row.last_attempt_at = now
            if error is None:
                row.status, row.last_success_at, row.consecutive_failures, row.error = "ok", now, 0, None
                job_row.status, job_row.last_success_at, job_row.consecutive_failures, job_row.error = "ok", now, 0, None
                result["ok"].append(ticker)
                continue
            had_snapshot = row.last_success_at is not None
            row.status, row.error = "failed", f"{type(error).__name__}: {error}"[:2000]
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
            if job_row is not row:
                job_row.consecutive_failures = (job_row.consecutive_failures or 0) + 1
                job_row.error = row.error
            if job_row.consecutive_failures >= threshold:
                job_row.status = "failed"
            tripped = job_row.consecutive_failures >= threshold
        log.warning("[%s] %s 수집 실패 (%d회 시도): %s", job, ticker, attempts, error)
        result["failed"].append(ticker)
        result["errors"][ticker] = str(error)[:300]
        if had_snapshot:
            result["stale"].append(ticker)
        if tripped:
            result["status"] = "circuit_open"
            result["skipped"] = tickers[i + 1:]
            log.error("[%s] 연속 실패 %d회 — 회로 차단기 작동", job, threshold)
            break
    if result["status"] == "ok" and result["failed"]:
        result["status"] = "partial" if result["ok"] else "failed"
    return result
