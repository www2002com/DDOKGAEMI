"""한국은행 ECOS 100대 통계지표 → macro_data (월 1회, 매크로 전망 에이전트 입력).

ECOS_API_KEY가 없으면 공개 테스트 키 "sample"을 쓴다(첫 10건만 조회된다).
"""

from __future__ import annotations

from typing import Any

from autopilot_korea.collectors.base import JOB_ROW, CollectorContext, run_job, upsert
from autopilot_korea.collectors.sources import ecos
from autopilot_korea.db.models import MacroData

JOB = "macro"


def _collect(ctx: CollectorContext, _: str) -> None:
    rows = ecos.key_statistics(ctx.settings.ecos_api_key or "sample")
    if not rows:
        raise RuntimeError("ECOS 통계가 비어 있습니다")
    with ctx.db.session() as s:
        for r in rows:
            if not r.get("name") or not r.get("period"):
                continue
            upsert(s, MacroData, {"series": str(r["name"])[:80], "period": str(r["period"])[:10]},
                   {"value": r.get("value"), "unit": (r.get("unit") or None) and str(r["unit"])[:40]})


def collect(ctx: CollectorContext, tickers: list[str] | None = None) -> dict[str, Any]:
    """종목과 무관한 작업이다(tickers는 무시)."""
    return run_job(ctx, JOB, _collect, [JOB_ROW])
