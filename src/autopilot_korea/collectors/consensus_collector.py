"""I. 애널리스트 컨센서스 (#66~#72) + #56 예상 EPS — FnGuide (sources/fnguide.py 참조).

무료 FnGuide 커버리지가 없는 종목은 covered=False로 저장하고 변수는 모두 None(신선도 "missing")이 된다.
"""

from __future__ import annotations

from typing import Any

from autopilot_korea.collectors.base import CollectorContext, run_job, upsert
from autopilot_korea.collectors.sources import fnguide
from autopilot_korea.db.models import ConsensusData

JOB = "consensus"
FIELDS = ("target_high", "target_low", "target_mean", "target_median", "rec_mean", "rec_key", "n_analysts", "forward_eps")


def compute_consensus_vars(row: dict[str, Any] | None) -> dict[str, Any]:
    """row: consensus_data 행 dict (date ≤ 기준일 중 최신)."""
    r = row if row and row.get("covered", True) else {}
    return {
        "target_high_price": r.get("target_high"),
        "target_low_price": r.get("target_low"),
        "target_mean_price": r.get("target_mean"),
        "target_median_price": r.get("target_median"),
        "recommendation_mean": r.get("rec_mean"),
        "recommendation_key": r.get("rec_key"),
        "number_of_analyst_opinions": r.get("n_analysts"),
    }


def _collect_one(ctx: CollectorContext, ticker: str) -> None:
    data = fnguide.fetch_consensus(ticker, ctx.as_of)
    with ctx.db.session() as s:
        upsert(s, ConsensusData, {"ticker": ticker, "date": ctx.as_of},
               {"covered": data["covered"], **{k: data.get(k) for k in FIELDS}})


def collect(ctx: CollectorContext, tickers: list[str]) -> dict[str, Any]:
    return run_job(ctx, JOB, _collect_one, tickers)
